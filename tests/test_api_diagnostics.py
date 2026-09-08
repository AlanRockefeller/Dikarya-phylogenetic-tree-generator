import gzip
import io
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request
from unittest.mock import patch

import pytest

from app.services import api_diagnostics as diagnostics
from app.services import inaturalist_tree_service as inat


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "ARCHIVE_DIR", tmp_path)
    return tmp_path


def records(directory):
    return [json.loads(gzip.decompress(path.read_bytes())) for path in directory.glob("*/*.gz")]


def test_full_body_redaction_and_context(archive):
    req = Request("https://api.example/observations/123?api_key=topsecret", headers={"Authorization": "Bearer secretbearer"})
    with patch.object(diagnostics, "_current_context", return_value={"job": "job1", "req": "req1"}):
        diagnostic_id = diagnostics.record_api_failure(req.full_url, req=req, status=422,
            reason="missing_its", headers={"Date": "yesterday", "Set-Cookie": "cookie_secret"},
            body={"sequence": "A" * 200000, "access_token": "hidden", "nested": [{"password": "hidden"}],
                  "message": "topsecret secretbearer"})
    result = records(archive)[0]
    assert result["id"] == diagnostic_id
    assert result["context"] == {"job": "job1", "req": "req1"}
    assert len(result["body"]["sequence"]) == 200000
    assert result["headers"] == {"Date": "yesterday"}
    text = json.dumps(result)
    assert not any(secret in text for secret in ("topsecret", "secretbearer", "hidden", "cookie_secret"))


def test_http_error_body_remains_readable(archive):
    error = HTTPError("https://api.example/test", 429, "limited", {"Retry-After": "2"}, io.BytesIO(b'{"error":"retry"}'))
    with patch.object(diagnostics.request, "urlopen", side_effect=error):
        with pytest.raises(HTTPError) as caught:
            with diagnostics.diagnostic_urlopen(error.url):
                pass
    assert caught.value.read() == b'{"error":"retry"}'
    assert records(archive)[0]["body"] == {"error": "retry"}


def test_network_failure_has_no_invented_body(archive):
    with patch.object(diagnostics.request, "urlopen", side_effect=URLError("offline")):
        with pytest.raises(URLError):
            with diagnostics.diagnostic_urlopen("https://api.example/test"):
                pass
    result = records(archive)[0]
    assert result["body_available"] is False
    assert result["status"] is None


class Response(io.BytesIO):
    status = 200
    headers = {"Age": "30"}


def test_invalid_json_captures_body(archive):
    with patch.object(diagnostics.request, "urlopen", return_value=Response(b"<html>unavailable</html>")):
        with pytest.raises(json.JSONDecodeError):
            with diagnostics.diagnostic_urlopen("https://api.example/test") as response:
                json.loads(response.read())
    assert records(archive)[0]["body"] == "<html>unavailable</html>"


def test_success_does_not_write_archive(archive):
    with patch.object(diagnostics.request, "urlopen", return_value=Response(b'{"ok":true}')):
        with diagnostics.diagnostic_urlopen("https://api.example/test") as response:
            assert json.loads(response.read()) == {"ok": True}
    assert records(archive) == []


def test_semantic_observation_failure_retains_full_envelope(archive):
    payload = {"total_results": 1, "results": [{"id": 74600029, "ofvs": []}], "extra": "retained"}
    with patch.object(diagnostics.request, "urlopen", return_value=Response(json.dumps(payload).encode())), patch.object(inat, "_pace_inat_request"):
        observation = inat.fetch_observation(74600029)
    assert records(archive) == []
    with pytest.raises(inat.InatTreeError):
        inat._create_mycomap_blast_from_observation(observation, 74600029)
    result = records(archive)[0]
    assert result["body"] == payload
    assert result["headers"] == {"Age": "30"}
    assert result["reason"] == "missing_or_unusable_its"


def test_failed_archive_never_masks_original_error(archive):
    error = HTTPError("https://api.example/test", 503, "unavailable", {}, io.BytesIO(b"retry"))
    with patch.object(diagnostics.request, "urlopen", side_effect=error), patch.object(diagnostics.os, "open", side_effect=PermissionError):
        with pytest.raises(HTTPError) as caught:
            with diagnostics.diagnostic_urlopen(error.url):
                pass
    assert caught.value.read() == b"retry"


def test_recovered_retry_still_retains_failure(archive):
    error = HTTPError("https://api.example/test", 429, "limited", {}, io.BytesIO(b'{"error":"rate limit"}'))
    with patch.object(diagnostics.request, "urlopen", side_effect=[error, Response(b'{"results":[]}')]), patch.object(inat, "_pace_inat_request"), patch.object(inat.time, "sleep"):
        assert inat._http_request(error.url) == {"results": []}
    assert len(records(archive)) == 1
    assert records(archive)[0]["status"] == 429


def test_malformed_json_credentials_redacted(archive):
    diagnostics.record_api_failure("https://api.example/test", reason="invalid_json",
        body='{"access_token":"new-secret", "password": "another-secret", BROKEN')
    text = json.dumps(records(archive))
    assert "new-secret" not in text
    assert "another-secret" not in text


def test_binary_response_not_lossily_decoded(archive):
    raw = b"\xff\xfe\x00\x80"
    diagnostics.record_api_failure("https://api.example/test", reason="invalid_zip", body=raw)
    result = records(archive)[0]
    assert result["body"].encode(result["body_encoding"]) == raw


def test_digest_groups_diagnostics_without_discarding_event_identity():
    from scripts.dikarya_log_digest import meaningful_error_key
    first = "[WARNING] event=api.response_failed diagnostic=" + "a" * 32 + " status=429 reason=http_error"
    second = first.replace("a" * 32, "b" * 32)
    assert meaningful_error_key(first) == meaningful_error_key(second)
