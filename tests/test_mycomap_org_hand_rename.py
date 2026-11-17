"""Sequences MycoMap.org refreshed but did not rename are listed for a hand rename.

Since 2026-11-12 MycoMap.org never renames sequences for an API key: the
refresh succeeds and its answer carries sequenceNamesRefused. Every HTTP call
is stubbed; nothing here reaches mycomap.org.
"""

import json
import urllib.error
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest

from app.services import mycomap_org_service as org
from app.services import mycomap_service

REFUSED = {
    "code": "API_KEY_CANNOT_RENAME_SEQUENCES",
    "error": "Sequence renames are not available with an API key: the record was "
             "refreshed, but its sequences were not renamed. Sign in to rename them.",
}
ENTOLOMA = "Entoloma sp. 'moliniophilum-HI01'"


class _JsonResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _limit):
        return self._data


def _row(seq_id, name, inat=None, mo=None, cached=ENTOLOMA):
    return {"id": seq_id, "scientificName": name, "inatObservationId": inat,
            "moObservationId": mo, "inatReportCachedName": cached}


def _fake_mycomap(rows, refresh_answers, batch_reads=None, http_error=None,
                  fail_batch_reads_from=None):
    """urlopen stub: sequences/batch answers from ``rows``, refresh from ``refresh_answers``.

    ``fail_batch_reads_from`` makes the Nth sequences/batch read (0-based) and
    every later one fail as if MycoMap.org were down.
    """
    reads = [] if batch_reads is None else batch_reads

    def urlopen(request, timeout):
        url = request.full_url
        if "/mycomap/sequences/batch" in url:
            refs = parse_qs(urlparse(url).query)["observations"][0].split(",")
            reads.append(refs)
            if fail_batch_reads_from is not None and len(reads) > fail_batch_reads_from:
                raise urllib.error.URLError("down")
            wanted = {r.split(":")[1] for r in refs}
            return _JsonResponse({"sequences": [
                row for row in rows
                if str(row.get("inatObservationId") or "") in wanted
                or str(row.get("moObservationId") or "") in wanted]})
        if url.endswith("/admin/mycomap/refresh"):
            if http_error is not None:
                raise http_error(request)
            return _JsonResponse(refresh_answers[json.loads(request.data)["observation"]])
        if url.endswith("/sync-batch"):
            return _JsonResponse({})
        raise AssertionError(f"unexpected URL {url}")

    return urlopen


def _refresh(monkeypatch, references, rows, refresh_answers, **kwargs):
    monkeypatch.setenv("MYCOMAP_ORG_API_KEY", "example-key")
    monkeypatch.setattr(mycomap_service.time, "sleep", lambda _s: None)
    with patch.object(org, "diagnostic_urlopen",
                      side_effect=_fake_mycomap(rows, refresh_answers, **kwargs)):
        return mycomap_service.refresh_mycomap_observation_records(references)


def test_refused_answer_lists_only_misnamed_sequences_with_admin_links(monkeypatch):
    rows = [
        _row(21681, "Entolomataceae", inat=50523564),
        _row(21690, ENTOLOMA, inat=50523564),  # already right: not listed
        _row(21679, "Entolomataceae", inat=50523640),
    ]
    reads = []
    result = _refresh(monkeypatch, ["inat:50523564", "inat:50523640"], rows, {
        "inat:50523564": {"sequenceNamesRefused": REFUSED},
        "inat:50523640": {"sequenceNamesRefused": REFUSED},
    }, batch_reads=reads)

    # Refreshed, so neither observation counts as a refresh failure.
    assert result["not_refreshed"] == []
    warning = result["sequence_rename_warning"]
    assert warning["observation_count"] == 2
    assert warning["message"].startswith(
        "2 records were refreshed, but MycoMap.org did not rename their sequences "
        "(API keys can't rename).")
    assert [(s["sequence_id"], s["current_name"], s["target_name"], s["url"])
            for s in warning["sequences"]] == [
        ("21681", "Entolomataceae", ENTOLOMA,
         "https://mycomap.org/admin/blast-results/50523564/21681"),
        ("21679", "Entolomataceae", ENTOLOMA,
         "https://mycomap.org/admin/blast-results/50523640/21679"),
    ]
    assert warning["unlisted_references"] == []
    assert all("mycomap.com" not in s["url"] for s in warning["sequences"])
    # The list reuses the post-refresh read: before + after, nothing extra.
    assert len(reads) == 2


def test_session_target_name_wins_and_target_is_not_counted_as_a_rename():
    summary = org.summarize_refresh_response({
        "sequenceNames": {
            "target": {"mycobank": 1, "name": "Amanita muscaria"},
            "renamed": [{"id": 1, "from": "Amanita", "to": "Amanita muscaria"}],
            "skipped": [{"id": 2, "reason": "same-name"}],
            "failed": [{"id": 3, "error": "boom"}],
        },
    })
    assert summary["renamed"] == 1
    assert summary["skipped"] == {"same-name": 1}
    assert summary["failed"] == ["3"]
    assert summary["target_name"] == "Amanita muscaria"
    assert summary["needs_hand_rename"] is True

    # Before this fix the target dict was read as one renamed sequence.
    quiet = org.summarize_refresh_response({
        "sequenceNames": {"target": {"name": "X y"}, "renamed": [], "skipped": []}})
    assert quiet["renamed"] == 0
    assert quiet["needs_hand_rename"] is False


def test_refused_answer_never_claims_a_rename():
    summary = org.summarize_refresh_response({
        "sequenceNamesRefused": REFUSED,
        "sequenceNames": [{"id": 1, "newName": "X y"}],
    })
    assert summary["renamed"] == 0
    assert summary["names_refused"] == "API_KEY_CANNOT_RENAME_SEQUENCES"
    assert summary["needs_hand_rename"] is True


def test_mo_observation_link_uses_the_mo_number(monkeypatch):
    rows = [_row(900, "Agaricus", mo=12345, cached="")]
    result = _refresh(monkeypatch, ["mo:12345"], rows, {
        "mo:12345": {
            "sequenceNamesRefused": REFUSED,
            "sequenceNames": {"target": {"name": "Agaricus campestris"}},
        },
    })
    (seq,) = result["sequence_rename_warning"]["sequences"]
    assert seq["url"] == "https://mycomap.org/admin/blast-results/12345/900"
    assert seq["target_name"] == "Agaricus campestris"
    assert result["sequence_rename_warning"]["message"].startswith(
        "1 record was refreshed, but MycoMap.org did not rename its sequences")


def test_admin_url_observation_number_falls_back_to_mo_digits_then_zero():
    assert org.admin_blast_url({"id": 5, "moObservationId": "MO777"}) == \
        "https://mycomap.org/admin/blast-results/777/5"
    assert org.admin_blast_url({"id": 5}) == "https://mycomap.org/admin/blast-results/0/5"
    assert org.admin_blast_url({"id": "../x"}) == ""


def test_row_with_no_observation_number_links_with_zero():
    pending = {"inat:1": {"names_refused": "API_KEY_CANNOT_RENAME_SEQUENCES",
                          "target_name": "Russula emetica"}}
    observations = org.sequences_needing_rename(
        pending, {"inat:1": [{"id": 44, "scientificName": "Russula"}]})
    assert observations[0]["sequences"][0]["url"] == \
        "https://mycomap.org/admin/blast-results/0/44"


def test_lookup_failure_still_warns_without_a_list(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.URLError("down")

    pending = {"inat:50523564": {"names_refused": "API_KEY_CANNOT_RENAME_SEQUENCES",
                                 "target_name": ""}}
    with patch.object(org, "diagnostic_urlopen", side_effect=urlopen):
        warning = org.hand_rename_warning(org.sequences_needing_rename(pending))
    assert warning["observation_count"] == 1
    assert warning["sequences"] == []
    assert warning["unlisted_references"] == ["inat:50523564"]
    assert "API keys can't rename" in warning["message"]


def test_refused_key_still_raises_and_reports_nothing_to_rename(monkeypatch):
    def forbidden(request):
        import io
        return urllib.error.HTTPError(
            request.full_url, 403, "x", {},
            io.BytesIO(json.dumps({"code": "API_KEY_REFRESH_NOT_GRANTED"}).encode()))

    with pytest.raises(mycomap_service.MycoMapRefreshError) as raised:
        _refresh(monkeypatch, ["inat:1"], [], {}, http_error=forbidden)
    assert "admin" in str(raised.value)


def test_rate_limit_is_unchanged(monkeypatch):
    from app.services import ncbi_pacing

    class FakeRedis:
        def set(self, *_a, **_k):
            pass

        def ttl(self, _key):
            return -2

    monkeypatch.setattr(ncbi_pacing, "connection", lambda: FakeRedis())

    def too_many(request):
        import io
        return urllib.error.HTTPError(request.full_url, 429, "x", {"Retry-After": "60"},
                                      io.BytesIO(b"{}"))

    with pytest.raises(mycomap_service.MycoMapRefreshRateLimited) as raised:
        _refresh(monkeypatch, ["inat:1"], [], {}, http_error=too_many)
    assert raised.value.retry_after == 60


def test_answer_without_refusal_adds_no_warning(monkeypatch):
    rows = [_row(21681, "Entolomataceae", inat=50523564)]
    result = _refresh(monkeypatch, ["inat:50523564"], rows, {
        "inat:50523564": {"sequenceNames": [{"id": 21681, "newName": ENTOLOMA}]},
    })
    assert result["sequence_rename_warning"] is None
    assert result["warnings"] == []


def test_rename_pending_is_filled_only_for_refused_refreshes(monkeypatch):
    monkeypatch.setenv("MYCOMAP_ORG_API_KEY", "example-key")
    answers = {"inat:1": {"sequenceNamesRefused": REFUSED}, "inat:2": {}}
    pending, not_refreshed = {}, []
    with patch.object(org, "diagnostic_urlopen", side_effect=_fake_mycomap([], answers)):
        org.refresh_observations(["inat:1", "inat:2"], not_refreshed=not_refreshed,
                                 rename_pending=pending)
    assert list(pending) == ["inat:1"]
    assert not_refreshed == []


def test_failed_reread_after_a_refused_refresh_keeps_the_refresh_and_the_warning(monkeypatch):
    """Orchestration: refresh succeeds (refused renames), the read after it fails."""
    rows = [_row(21681, "Entolomataceae", inat=50523564)]
    reads = []
    result = _refresh(monkeypatch, ["inat:50523564"], rows, {
        "inat:50523564": {"sequenceNamesRefused": REFUSED},
    }, batch_reads=reads, fail_batch_reads_from=1)

    assert result["not_refreshed"] == []
    assert result["after"] == {}
    assert any("could not read them back" in w for w in result["warnings"])
    warning = result["sequence_rename_warning"]
    assert warning["sequences"] == []
    assert warning["unlisted_references"] == ["inat:50523564"]
    assert warning["refused_references"] == ["inat:50523564"]
    # Before + the failed after-read; no third attempt for the list.
    assert len(reads) == 2


def test_sequence_returned_in_two_batches_is_listed_once(monkeypatch):
    """A sequence linked to an iNat and an MO observation in different batches."""
    shared = _row(77, "Agaricales", inat=1, mo=2)
    responses = iter([[shared], [shared]])
    monkeypatch.setattr(org, "OBSERVATION_BATCH_SIZE", 1)
    monkeypatch.setattr(org, "_json", lambda *_a, **_k: {"sequences": next(responses)})
    rows = org.observation_rows(["inat:1", "mo:2"])
    assert [r["id"] for r in rows["inat:1"]] == [77]
    assert [r["id"] for r in rows["mo:2"]] == [77]

    pending = {"inat:1": {"names_refused": "X", "target_name": "Agaricus"}}
    warning = org.hand_rename_warning(org.sequences_needing_rename(pending, rows))
    assert warning["sequence_count"] == 1
