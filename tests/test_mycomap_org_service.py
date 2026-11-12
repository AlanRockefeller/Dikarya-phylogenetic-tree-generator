"""Provider routing and public MycoMap.org BLAST response contracts."""

import json
from unittest.mock import patch

import pytest

from app.services import mycomap_org_service as org
from app.services.mycomap_service import (
    parse_mycomap_result_reference, resolve_mycomap_result_reference,
)


@pytest.mark.parametrize("url,kind", [
    ("https://mycomap.org/mycoblast/648539", "mycoblast"),
    ("https://www.mycomap.org/blast-results/332221330/761935", "sequence"),
    ("https://mycomap.org/admin/blast-results/332221330/761935", "sequence"),
])
def test_org_result_paths(url, kind):
    assert parse_mycomap_result_reference(url)["kind"] == kind


@pytest.mark.parametrize("url", [
    "http://mycomap.org/mycoblast/1",
    "https://mycomap.org.evil.test/mycoblast/1",
    "https://evil@mycomap.org/mycoblast/1",
    "https://mycomap.org:8443/mycoblast/1",
    "https://mycomap.org/mycoblast/nope",
    "https://mycomap.org/other/1",
    "https://mycomap.org/mycoblast/1?redirect=evil",
])
def test_org_rejects_unsupported_urls(url):
    assert parse_mycomap_result_reference(url) is None


def test_observation_link_resolves_through_sequence_metadata():
    metadata = {
        "inatObservationId": 332221330,
        "blastMetadata": {"MM_Blast_Link": "https://mycomap.com/path/view/627080"},
    }
    with patch.object(org, "_json", return_value=metadata) as read:
        result = resolve_mycomap_result_reference(
            "https://mycomap.org/admin/blast-results/332221330/761935"
        )
    assert result["result_id"] == "627080"
    read.assert_called_once_with("sequence-metadata/761935")


def test_observation_link_must_match_metadata():
    with patch.object(org, "_json", return_value={"inatObservationId": 42}):
        with pytest.raises(org.OrgResultError) as caught:
            resolve_mycomap_result_reference(
                "https://mycomap.org/blast-results/332221330/761935"
            )
    assert caught.value.status == 404


XML = b"""<?xml version="1.0"?>
<BlastOutput><BlastOutput_query-len>100</BlastOutput_query-len>
<BlastOutput_iterations><Iteration><Iteration_hits><Hit>
<Hit_id>551798</Hit_id><Hit_def>iNat314834254 record</Hit_def>
<Hit_accession>551798</Hit_accession><Hit_len>100</Hit_len>
<Hit_hsps><Hsp><Hsp_score>50</Hsp_score><Hsp_identity>90</Hsp_identity>
<Hsp_align-len>100</Hsp_align-len><Hsp_query-from>1</Hsp_query-from>
<Hsp_query-to>100</Hsp_query-to><Hsp_hit-from>100</Hsp_hit-from>
<Hsp_hit-to>1</Hsp_hit-to><Hsp_hseq>ACGT--ACGT</Hsp_hseq></Hsp></Hit_hsps>
</Hit></Iteration_hits></Iteration></BlastOutput_iterations></BlastOutput>"""


def test_xml_metrics_and_local_metadata_fallback():
    assert org._parse_xml(XML, "local")[0]["identity"] == 90
    status = {"local": {"status": "complete", "has_results": True},
              "ncbi": {"status": "queued", "has_results": False,
                       "queue_position": 12}}
    with patch.object(org, "status", return_value=status), \
         patch.object(org, "_json", return_value=status), \
         patch.object(org, "_read", return_value=XML), \
         patch.object(org, "_local_metadata", return_value={}):
        result = org.fetch_results("123")
    assert result["pending_sources"] == ["ncbi"]
    assert result["local_count"] == 1
    assert result["sequences"][0]["sequence"] == "ACGTACGT"
    assert result["sequences"][0]["subject_cover"] == 100


def test_ncbi_import_fetches_full_sequence_and_positive_reverse_coverage():
    xml = XML.replace(b"551798", b"PX215422").replace(
        b"<Hit_len>100</Hit_len>", b"<Hit_len>12</Hit_len>"
    ).replace(b"<Hsp_hit-from>100</Hsp_hit-from>",
              b"<Hsp_hit-from>10</Hsp_hit-from>").replace(
        b"<Hsp_hit-to>1</Hsp_hit-to>", b"<Hsp_hit-to>3</Hsp_hit-to>"
    )
    status = {"ncbi": {"status": "complete", "has_results": True}}
    seen = {}

    class Response:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b">PX215422.1 Example fungus\nTTACGTACGTGG\n"

        def close(self):
            pass

    def ncbi_request(method, url, **kwargs):
        seen.update(method=method, url=url, data=kwargs["data"])
        return Response()

    with patch.object(org, "_json", return_value=status), \
         patch.object(org, "_read", return_value=xml), \
         patch("app.services.blast_service._ncbi_request", side_effect=ncbi_request):
        result = org.fetch_results("123", include_local=False, time_budget=25)

    assert seen["method"] == "POST"
    assert seen["url"].endswith("/efetch.fcgi")
    assert seen["data"]["id"] == "PX215422"
    assert result["ncbi_count"] == 1
    assert result["sequences"][0]["sequence"] == "TTACGTACGTGG"
    assert result["sequences"][0]["subject_cover"] == pytest.approx(66.67)


@pytest.mark.parametrize("full_sequences", [{}, {"PX215422": "ACGTACGT"}])
def test_ncbi_import_omits_hits_without_matching_full_records(full_sequences):
    xml = XML.replace(b"551798", b"PX215422")
    status = {"ncbi": {"status": "complete", "has_results": True}}
    with patch.object(org, "_json", return_value=status), \
         patch.object(org, "_read", return_value=xml), \
         patch.object(org, "_ncbi_sequences", return_value=(full_sequences, set())):
        result = org.fetch_results("123", include_local=False)

    assert result["sequences"] == []
    assert result["ncbi_count"] == 0
    assert result["failed_sources"] == ["ncbi"]
    assert "full NCBI sequences" in result["errors"][0]


_HIT = (XML.split(b"<Iteration_hits>")[1].split(b"</Iteration_hits>")[0]
        .replace(b"551798", b"{accession}"))
TWO_HIT_XML = XML.replace(
    _HIT.replace(b"{accession}", b"551798"),
    _HIT.replace(b"{accession}", b"PX215422") + _HIT.replace(b"{accession}", b"PX215423"),
)


@pytest.mark.parametrize("unchecked_second", [True, False])
def test_partial_ncbi_miss_always_flags_incomplete_source(unchecked_second):
    status = {"ncbi": {"status": "complete", "has_results": True}}

    def fake_fetch(accessions, *, deadline=None):
        assert sorted(accessions) == ["PX215422", "PX215423"]
        # PX215423 either sat in a batch that errored or timed out, or NCBI
        # answered without a record for it.
        unchecked = {"PX215423"} if unchecked_second else set()
        return {"PX215422": "ACGT" * 25}, unchecked

    with patch.object(org, "_json", return_value=status), \
         patch.object(org, "_read", return_value=TWO_HIT_XML), \
         patch.object(org, "_ncbi_sequences", side_effect=fake_fetch):
        result = org.fetch_results("123", include_local=False)

    assert result["ncbi_count"] == 1
    assert result["failed_sources"] == ["ncbi"]
    assert "full NCBI sequences for 1" in result["errors"][0]


class _FakeResponse:
    def __init__(self, status=200, body=b""):
        self.status_code = status
        self.body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        pass


def _retry_exhausted(*args, **kwargs):
    import requests
    raise requests.exceptions.RetryError("NCBI did not answer")


@pytest.mark.parametrize("outcome, transient", [
    (_retry_exhausted, True),
    (lambda *a, **k: _FakeResponse(503), True),
    (lambda *a, **k: _FakeResponse(400), False),
    (lambda *a, **k: _FakeResponse(body=b"\xff\xfe"), False),
])
def test_only_unanswered_ncbi_batches_count_as_unchecked(outcome, transient):
    with patch("app.services.blast_service._ncbi_request", side_effect=outcome):
        sequences, unchecked = org._ncbi_sequences(["PX215422"])
    assert sequences == {}
    assert unchecked == ({"PX215422"} if transient else set())


def test_oversized_ncbi_batch_is_not_unchecked(monkeypatch):
    monkeypatch.setattr(org, "MAX_XML_BYTES", 4)
    with patch("app.services.blast_service._ncbi_request",
               return_value=_FakeResponse(body=b">PX215422\nACGTACGT\n")):
        assert org._ncbi_sequences(["PX215422"]) == ({}, set())


def test_local_metadata_preserves_observation_reference():
    status = {"local": {"status": "complete", "has_results": True}}
    metadata = {"551798": {"id": 551798, "scientificName": "Example fungus",
                            "inatObservationId": 314834254,
                            "sequence": "ACGT" * 25}}
    with patch.object(org, "_json", return_value=status), \
         patch.object(org, "_read", return_value=XML), \
         patch.object(org, "_local_metadata", return_value=metadata):
        result = org.fetch_results("123", include_ncbi=False)
    assert result["sequences"][0]["name"].startswith(
        "MycoMap#551798 iNat314834254"
    )
    assert result["sequences"][0]["mycomap_sequence_id"] == "551798"
    assert len(result["sequences"][0]["sequence"]) == 100


def test_direct_org_import_uses_shared_queue_rules():
    from app.api.routes import gather_mycomap_sequences_for_queue
    normalized = {
        "sequences": [{"name": "551798 iNat314834254 Example fungus",
                       "sequence": "ACGT" * 35, "source": "mycomap",
                       "hit_source": "local", "taxon": "Example fungus",
                       "identity": 98.5, "query_cover": 100,
                       "subject_cover": 99, "location": "California US"}],
        "ncbi_count": 0, "local_count": 1, "errors": [],
        "failed_sources": [], "pending_sources": [],
        "ncbi_queue_position": None,
    }
    with patch.object(org, "fetch_results", return_value=normalized), \
         patch("app.services.mycomap_service.fetch_mycomap_fasta",
               side_effect=AssertionError("must not call .com FASTA")):
        payload, error = gather_mycomap_sequences_for_queue(
            "https://mycomap.org/mycoblast/648539",
            include_ncbi=False, fetch_time_budget=25,
        )
    assert error is None
    assert payload["blast_metrics_count"] == 1
    assert payload["sequences"][0]["identity"] == 98.5


def test_rerun_uses_api_key_header_and_fixed_endpoint(monkeypatch):
    monkeypatch.setenv("MYCOMAP_ORG_API_KEY", "example-key")
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            return json.dumps({"status": "queued"}).encode()

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["key"] = request.get_header("X-api-key")
        seen["body"] = json.loads(request.data)
        return Response()

    with patch.object(org, "diagnostic_urlopen", side_effect=urlopen):
        assert org.rerun("648539", "ncbi", 120)["status"] == "queued"
    # MycoMap.org reads a raw key from X-API-Key; a Bearer header there is a
    # mobile-app token and would make the request anonymous.
    assert seen == {
        "url": "https://mycomap.org/api/mycoblast/rerun",
        "auth": None,
        "key": "example-key",
        "body": {"sequenceId": 648539, "rerunType": "ncbi", "maxHits": 250},
    }


def test_rerun_max_hits_rounds_up_to_an_accepted_value():
    assert [org.rerun_max_hits(n) for n in (1, 50, 51, 100, 101, 250, 251, 500, 9999, None)] == [
        50, 50, 100, 100, 250, 250, 500, 500, 500, 50,
    ]


def test_rerun_rate_limit_is_retryable_but_a_refusal_is_not(monkeypatch):
    import io
    import urllib.error

    monkeypatch.setenv("MYCOMAP_ORG_API_KEY", "example-key")

    def answer(code):
        def urlopen(request, timeout):
            raise urllib.error.HTTPError(request.full_url, code, "x", {"Retry-After": "300"},
                                         io.BytesIO(b"{}"))
        return urlopen

    for code, retryable in ((429, True), (401, False), (403, False)):
        with patch.object(org, "diagnostic_urlopen", side_effect=answer(code)):
            try:
                org.rerun("648539", "local", 50)
            except org.OrgResultError as exc:
                assert exc.retryable is retryable, code
                assert (org.deferral_details(exc) is not None) is retryable, code
            else:
                raise AssertionError(f"HTTP {code} did not raise")


def test_rerun_waits_for_new_xml_date():
    details = {
        "result_id": "648539", "org_wait_sources": ["local"],
        "org_before_dates": {"local": "2026-08-31T19:53:24Z"},
    }
    old = {"local": {"status": "complete", "has_results": True,
                     "xml_date": "2026-08-31T19:53:24Z"}}
    new = {"local": {"status": "complete", "has_results": True,
                     "xml_date": "2026-09-24T15:00:00Z"}}
    with patch.object(org, "status", side_effect=[old, new]):
        assert org.rerun_pending(details)
        assert not org.rerun_pending(details)
    assert details["local_status"] == "completed"
    assert details["org_wait_sources"] == []


def test_late_org_hit_keeps_metrics_for_job_metadata(tmp_path):
    from app.services.inaturalist_tree_service import (
        _append_fasta_to_job_input, _build_sequence_metadata,
    )
    hit = {"name": "PX215422 Example fungus", "sequence": "ACGT" * 30,
           "source": "mycomap", "hit_source": "ncbi", "identity": 98.5,
           "query_cover": 100, "subject_cover": 99,
           "blast_metrics_available": True}
    added = []
    count = _append_fasta_to_job_input(
        tmp_path, "", sequence_records=[hit], added_records=added,
    )
    assert count == 1
    assert _build_sequence_metadata(added)[0]["subject_cover"] == 99
    assert _append_fasta_to_job_input(
        tmp_path, "", sequence_records=[hit]
    ) == 0


class _JsonResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _limit):
        return self._data


def _http_error(request, code, payload, headers=None):
    import io
    import urllib.error
    return urllib.error.HTTPError(request.full_url, code, "x", headers or {},
                                  io.BytesIO(json.dumps(payload).encode()))


def test_refresh_asks_mycomap_to_rename_sequences(monkeypatch):
    monkeypatch.setenv("MYCOMAP_ORG_API_KEY", "example-key")
    seen = []

    def urlopen(request, timeout):
        seen.append({
            "url": request.full_url,
            "auth": request.get_header("Authorization"),
            "key": request.get_header("X-api-key"),
            "type": request.get_header("Content-type"),
            "body": json.loads(request.data),
        })
        if request.full_url.endswith("/admin/mycomap/refresh"):
            return _JsonResponse({
                "sequenceNames": [
                    {"id": 1, "oldName": "Amanita", "newName": "Amanita muscaria"},
                    {"id": 2, "oldName": "Amanita muscaria", "reason": "same-name"},
                    {"id": 3, "oldName": "Amanita sp.", "reason": "no-access"},
                ],
                "localSync": {"oldName": "Amanita", "newName": "Amanita muscaria"},
                "nameRepair": {"warning": "Name repaired from a typo."},
            })
        return _JsonResponse({"ok": True})

    with patch.object(org, "diagnostic_urlopen", side_effect=urlopen):
        warnings = org.refresh_observations(["inat:123"])

    refresh, sync = seen
    assert refresh == {
        "url": "https://mycomap.org/api/admin/mycomap/refresh",
        "auth": None,
        "key": "example-key",
        "type": "application/json",
        "body": {"observation": "inat:123", "renameSequences": True},
    }
    assert sync["url"] == "https://mycomap.org/api/mycoblast/sync-batch"
    assert sync["body"] == {"action": "sync", "observations": ["inat:123"]}
    # no-access is reported plainly, not as a failed refresh; same-name is quiet.
    assert len(warnings) == 2
    assert "no-access" in warnings[0] and "project" in warnings[0]
    assert "same-name" not in " ".join(warnings)
    assert "Name repaired from a typo." in warnings[1]


def test_refresh_summary_accepts_sequence_names_keyed_by_id():
    summary = org.summarize_refresh_response({
        "sequenceNames": {"7": {"newName": "X y"}, "8": {"reason": "would-regress"}},
    })
    assert summary["renamed"] == 1
    assert summary["skipped"] == {"would-regress": 1}


def test_refresh_sends_one_observation_per_request_and_syncs_in_hundreds(monkeypatch):
    monkeypatch.setenv("MYCOMAP_ORG_API_KEY", "example-key")
    refreshed, synced = [], []

    def urlopen(request, timeout):
        body = json.loads(request.data)
        if request.full_url.endswith("/sync-batch"):
            synced.append(len(body["observations"]))
        else:
            refreshed.append(body["observation"])
        return _JsonResponse({})

    refs = [f"inat:{n}" for n in range(1, 151)]
    with patch.object(org, "diagnostic_urlopen", side_effect=urlopen):
        org.refresh_observations(refs)
    assert sorted(refreshed, key=lambda r: int(r[5:])) == refs
    assert synced == [100, 50]


def test_refresh_not_granted_names_the_admin_page_and_logs_the_body(monkeypatch, caplog):
    monkeypatch.setenv("MYCOMAP_ORG_API_KEY", "secret-key-value")

    def urlopen(request, timeout):
        raise _http_error(request, 403, {"code": "API_KEY_REFRESH_NOT_GRANTED",
                                         "echo": "secret-key-value"})

    with patch.object(org, "diagnostic_urlopen", side_effect=urlopen), \
            pytest.raises(org.OrgResultError) as raised:
        org._refresh_observation("inat:1", None)
    assert raised.value.retryable is False
    assert "https://mycomap.org/admin/api-keys" in str(raised.value)
    assert "API_KEY_REFRESH_NOT_GRANTED" in caplog.text
    assert "secret-key-value" not in caplog.text


def test_refresh_rate_limit_honours_retry_after_and_stops_calling(monkeypatch):
    from app.services import ncbi_pacing

    class FakeRedis:
        def __init__(self):
            self.ttls = {}

        def set(self, key, _value, ex=None):
            self.ttls[key] = ex

        def ttl(self, key):
            return self.ttls.get(key, -2)

    fake = FakeRedis()
    monkeypatch.setattr(ncbi_pacing, "connection", lambda: fake)
    monkeypatch.setenv("MYCOMAP_ORG_API_KEY", "example-key")
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        raise _http_error(request, 429, {"error": "Too many"}, {"Retry-After": "240"})

    with patch.object(org, "diagnostic_urlopen", side_effect=urlopen), \
            pytest.raises(org.OrgResultError) as raised:
        org._refresh_observation("inat:1", None)
    assert raised.value.retry_after == 240
    assert "4 minutes" in str(raised.value)
    assert org.refresh_paused_for(org.REFRESH_PATH) == 240
    assert org.refresh_paused_for(org.SYNC_PATH) == 0
    # While paused, a new refresh is refused without contacting MycoMap.org.
    with patch.object(org, "diagnostic_urlopen", side_effect=urlopen), \
            pytest.raises(org.OrgResultError):
        org.refresh_observations(["inat:2"])
    assert len(calls) == 1


def test_refresh_bad_reference_explains_the_accepted_forms(monkeypatch):
    monkeypatch.setenv("MYCOMAP_ORG_API_KEY", "example-key")

    def urlopen(request, timeout):
        raise _http_error(request, 400, {"error": "Invalid observation"})

    with patch.object(org, "diagnostic_urlopen", side_effect=urlopen), \
            pytest.raises(org.OrgResultError) as raised:
        org._refresh_observation("inat:1", None)
    assert raised.value.status == 400
    assert "Invalid observation" in str(raised.value)
    assert "mo:<id>" in str(raised.value)
    assert "mp:<id>" not in str(raised.value)


def test_tip_refresh_is_quiet_when_a_hand_edited_label_already_has_the_new_name():
    from app.services.tree_edit_service import refresh_mycomap_tip_labels

    original = "iNat154644100 Podaxis inyoensis Death Valley California US RiC 62"
    state = {
        "tree_structure": {"children": [{"name": original}, {"name": "other"}]},
        "renames": {original: "iNat154644100 Podaxis pistillaris Inyo Co. CA US"},
    }
    refresh = {
        "before": {"inat:154644100": {"found": True, "scientific_name": "Podaxis inyoensis",
                                      "location": ""}},
        "after": {"inat:154644100": {"found": True, "scientific_name": "Podaxis pistillaris",
                                     "location": ""}},
    }
    result = refresh_mycomap_tip_labels(state, [original], refresh)
    assert result["changes"] == []
    assert result["warnings"] == []


def _refresh_fixture(before, after):
    ref = "inat:154644100"
    return {
        "before": {ref: {"found": True, "scientific_name": before, "location": ""}},
        "after": {ref: {"found": True, "scientific_name": after, "location": ""}},
    }


_ORIGINAL_TIP = "iNat154644100 Podaxis inyoensis Death Valley California US RiC 62"


def _state(rename=None):
    return {
        "tree_structure": {"children": [{"name": _ORIGINAL_TIP}, {"name": "other"}]},
        "renames": {_ORIGINAL_TIP: rename} if rename else {},
    }


def test_tip_refresh_uses_the_built_name_when_mycomap_already_had_the_new_one():
    from app.services.tree_edit_service import refresh_mycomap_tip_labels

    metadata = [{"name": _ORIGINAL_TIP, "taxon": "Podaxis inyoensis"}]
    result = refresh_mycomap_tip_labels(
        _state(), [_ORIGINAL_TIP],
        _refresh_fixture("Podaxis pistillaris", "Podaxis pistillaris"), metadata)
    assert [c["new_name"] for c in result["changes"]] == [
        "iNat154644100 Podaxis pistillaris Death Valley California US RiC 62"]
    assert result["warnings"] == []


def test_tip_refresh_swaps_only_the_name_inside_a_hand_edited_label():
    from app.services.tree_edit_service import refresh_mycomap_tip_labels

    result = refresh_mycomap_tip_labels(
        _state("iNat154644100 Podaxis inyoensis Inyo Co. (my note)"), [_ORIGINAL_TIP],
        _refresh_fixture("Podaxis inyoensis", "Podaxis pistillaris"))
    assert result["changes"][0]["new_name"] == "iNat154644100 Podaxis pistillaris Inyo Co. (my note)"


def test_tip_refresh_leaves_a_label_without_the_old_name_and_says_so():
    from app.services.tree_edit_service import refresh_mycomap_tip_labels

    result = refresh_mycomap_tip_labels(
        _state("iNat154644100 my own label"), [_ORIGINAL_TIP],
        _refresh_fixture("Podaxis inyoensis", "Podaxis pistillaris"))
    assert result["changes"] == []
    assert len(result["warnings"]) == 1
    assert "Podaxis pistillaris" in result["warnings"][0]
    assert "left as it is" in result["warnings"][0]


def test_refresh_records_the_new_name_in_sequence_metadata(tmp_path):
    import os
    from app.services.tree_edit_service import read_input_info, refresh_sequence_metadata_names

    info = tmp_path / "input_info.json"
    info.write_text(json.dumps({"sequence": ">x\nACGT\n", "sequence_metadata": [
        {"name": _ORIGINAL_TIP, "taxon": "Podaxis inyoensis"},
        {"name": "other", "taxon": "Other thing"},
    ]}))
    os.chmod(info, 0o664)
    changes = refresh_sequence_metadata_names(
        tmp_path, read_input_info(tmp_path), [_ORIGINAL_TIP],
        _refresh_fixture("Podaxis pistillaris", "Podaxis pistillaris"))
    assert changes == [{"reference": "inat:154644100", "old_taxon": "Podaxis inyoensis",
                        "new_taxon": "Podaxis pistillaris"}]
    stored = json.loads(info.read_text())
    entry = stored["sequence_metadata"][0]
    assert entry["taxon"] == "Podaxis pistillaris"
    assert entry["taxon_before_refresh"] == "Podaxis inyoensis"
    assert entry["name"] == _ORIGINAL_TIP  # the tip's identity is untouched
    assert stored["sequence_metadata"][1]["taxon"] == "Other thing"
    assert stored["sequence"] == ">x\nACGT\n"
    assert info.stat().st_mode & 0o777 == 0o664
    # A second refresh with the same answer changes nothing.
    assert refresh_sequence_metadata_names(
        tmp_path, read_input_info(tmp_path), [_ORIGINAL_TIP],
        _refresh_fixture("Podaxis pistillaris", "Podaxis pistillaris")) == []


def test_tip_refresh_does_not_insert_the_new_name_inside_itself():
    from app.services.tree_edit_service import refresh_mycomap_tip_labels

    # The metadata taxon is a prefix of the new name, which the label already shows.
    metadata = [{"name": _ORIGINAL_TIP, "taxon": "Amanita"}]
    result = refresh_mycomap_tip_labels(
        _state("iNat154644100 Amanita muscaria Inyo Co."), [_ORIGINAL_TIP],
        _refresh_fixture("Amanita muscaria", "Amanita muscaria"), metadata)
    assert result["changes"] == []
    assert result["warnings"] == []
    assert result["settled_tips"] == [_ORIGINAL_TIP]


def test_tip_refresh_prefers_the_longer_old_name():
    from app.services.tree_edit_service import refresh_mycomap_tip_labels

    metadata = [{"name": _ORIGINAL_TIP, "taxon": "Amanita sp."}]
    result = refresh_mycomap_tip_labels(
        _state("iNat154644100 Amanita sp. Inyo Co."), [_ORIGINAL_TIP],
        _refresh_fixture("Amanita", "Amanita muscaria"), metadata)
    assert result["changes"][0]["new_name"] == "iNat154644100 Amanita muscaria Inyo Co."


def test_tip_metadata_lookup_tolerates_mafft_reversal_and_requires_a_unique_row():
    from app.services.tree_edit_service import _metadata_for_tip

    row = {"name": _ORIGINAL_TIP, "taxon": "Podaxis inyoensis"}
    assert _metadata_for_tip([row], "_R_" + _ORIGINAL_TIP) is row
    assert _metadata_for_tip([row, dict(row)], _ORIGINAL_TIP) is None


def test_unchanged_label_is_not_settled_so_metadata_keeps_the_old_name():
    from app.services.tree_edit_service import refresh_mycomap_tip_labels

    result = refresh_mycomap_tip_labels(
        _state("iNat154644100 my own label"), [_ORIGINAL_TIP],
        _refresh_fixture("Podaxis inyoensis", "Podaxis pistillaris"))
    assert result["settled_tips"] == []


def test_refresh_keeps_going_after_a_404_on_the_first_reference(monkeypatch):
    monkeypatch.setenv("MYCOMAP_ORG_API_KEY", "example-key")
    refreshed = []

    def urlopen(request, timeout):
        body = json.loads(request.data)
        if request.full_url.endswith("/sync-batch"):
            return _JsonResponse({})
        if body["observation"] == "inat:1":
            raise _http_error(request, 404, {"error": "Not found"})
        refreshed.append(body["observation"])
        return _JsonResponse({})

    not_refreshed = []
    with patch.object(org, "diagnostic_urlopen", side_effect=urlopen):
        warnings = org.refresh_observations(["inat:1", "inat:2", "inat:3"],
                                            not_refreshed=not_refreshed)
    assert sorted(refreshed) == ["inat:2", "inat:3"]
    assert not_refreshed == ["inat:1"]
    assert "inat:1" in warnings[0]


def test_refresh_429_on_the_first_call_reports_every_reference(monkeypatch):
    from app.services import ncbi_pacing

    class FakeRedis:
        def set(self, *_args, **_kwargs):
            pass

        def ttl(self, _key):
            return -2

    monkeypatch.setattr(ncbi_pacing, "connection", lambda: FakeRedis())
    monkeypatch.setenv("MYCOMAP_ORG_API_KEY", "example-key")

    def urlopen(request, timeout):
        if request.full_url.endswith("/sync-batch"):
            return _JsonResponse({})
        raise _http_error(request, 429, {"error": "Too many"}, {"Retry-After": "60"})

    refs = [f"inat:{n}" for n in range(1, 13)]
    not_refreshed = []
    with patch.object(org, "diagnostic_urlopen", side_effect=urlopen), \
            pytest.raises(org.OrgResultError) as raised:
        org.refresh_observations(refs, not_refreshed=not_refreshed)
    # Nothing refreshed: the 429 itself is raised (the route answers 429 with
    # Retry-After), every reference is reported, and sync-batch is not sent.
    assert raised.value.status == 429
    assert raised.value.retry_after == 60
    assert not_refreshed == refs


def test_refused_key_on_the_first_reference_raises_its_message_without_syncing(monkeypatch):
    monkeypatch.setenv("MYCOMAP_ORG_API_KEY", "example-key")
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        raise _http_error(request, 403, {"code": "API_KEY_REFRESH_NOT_GRANTED"})

    with patch.object(org, "diagnostic_urlopen", side_effect=urlopen), \
            pytest.raises(org.OrgResultError) as raised:
        org.refresh_observations(["inat:1", "inat:2"])
    assert "admin" in str(raised.value)
    assert not any(url.endswith("/sync-batch") for url in calls)
    assert len(calls) == 1


def test_tip_refresh_leaves_a_historical_note_beside_the_new_name():
    from app.services.tree_edit_service import refresh_mycomap_tip_labels

    label = "iNat154644100 Podaxis pistillaris (was Podaxis inyoensis)"
    result = refresh_mycomap_tip_labels(
        _state(label), [_ORIGINAL_TIP],
        _refresh_fixture("Podaxis inyoensis", "Podaxis pistillaris"))
    assert result["changes"] == []
    assert result["settled_tips"] == [_ORIGINAL_TIP]


def test_tip_refresh_skips_references_that_were_not_refreshed():
    from app.services.tree_edit_service import refresh_mycomap_tip_labels

    refresh = _refresh_fixture("Podaxis inyoensis", "Podaxis pistillaris")
    refresh["not_refreshed"] = ["inat:154644100"]
    result = refresh_mycomap_tip_labels(_state(), [_ORIGINAL_TIP], refresh)
    assert result["changes"] == []
    assert result["settled_tips"] == []


def test_versioned_or_genbank_compact_mo_is_not_an_observation():
    from app.services.tree_edit_service import tip_observation_reference

    assert tip_observation_reference("MO123456.1 Amanita muscaria") is None
    assert tip_observation_reference(
        "MO123456 Amanita muscaria", "", {"source": "genbank"}) is None
    # Explicit and trusted forms still count.
    assert tip_observation_reference("MO123456 Amanita muscaria") == "mo:123456"
    assert tip_observation_reference("mo:123456 Amanita", "", {"source": "genbank"}) == "mo:123456"


def test_mafft_reversed_tips_still_name_their_observation():
    from app.services.mycomap_service import (
        extract_mycomap_observation_reference,
        extract_mycomap_observation_references,
    )
    from app.services.sequence_dedup_service import record_accession, record_provenance
    from app.services.tree_edit_service import tip_observation_reference

    reversed_tip = "_R_" + _ORIGINAL_TIP
    assert extract_mycomap_observation_reference(reversed_tip) == "inat:154644100"
    assert extract_mycomap_observation_references(reversed_tip) == ["inat:154644100"]
    assert tip_observation_reference(reversed_tip) == "inat:154644100"
    # Only MAFFT's leading marker is stripped, not "_R_" glued on mid-label.
    assert extract_mycomap_observation_reference("note_R_iNat154644100") is None
    # A reversed GenBank record keeps its provenance and accession.
    assert record_provenance("_R_MO123456.1 Amanita") == "genbank"
    assert record_accession("_R_MO123456.1 Amanita", {}) == "MO123456.1"
    assert tip_observation_reference("_R_MO123456.1 Amanita") is None


def test_tip_refresh_updates_a_mafft_reversed_tip():
    from app.services.tree_edit_service import refresh_mycomap_tip_labels

    reversed_tip = "_R_" + _ORIGINAL_TIP
    state = {"tree_structure": {"children": [{"name": reversed_tip}, {"name": "other"}]},
             "renames": {}}
    result = refresh_mycomap_tip_labels(
        state, [reversed_tip], _refresh_fixture("Podaxis inyoensis", "Podaxis pistillaris"))
    assert result["changes"][0]["new_name"] == (
        "_R_iNat154644100 Podaxis pistillaris Death Valley California US RiC 62")


def test_metadata_update_uses_the_reference_read_from_the_saved_label(tmp_path):
    from app.services.tree_edit_service import (
        read_input_info,
        refresh_mycomap_tip_labels,
        refresh_sequence_metadata_names,
    )

    # The original name carries no observation id; only the saved label does.
    original = "sample_1"
    (tmp_path / "input_info.json").write_text(json.dumps({"sequence_metadata": [
        {"name": original, "taxon": "Podaxis inyoensis"}]}))
    state = {"tree_structure": {"children": [{"name": original}, {"name": "other"}]},
             "renames": {original: "inat:154644100 Podaxis inyoensis"}}
    refresh = _refresh_fixture("Podaxis inyoensis", "Podaxis pistillaris")
    labels = refresh_mycomap_tip_labels(state, [original], refresh,
                                        read_input_info(tmp_path)["sequence_metadata"])
    assert labels["settled_references"] == {original: "inat:154644100"}
    changes = refresh_sequence_metadata_names(
        tmp_path, read_input_info(tmp_path), labels["settled_tips"], refresh,
        labels["settled_references"])
    assert [c["new_taxon"] for c in changes] == ["Podaxis pistillaris"]
