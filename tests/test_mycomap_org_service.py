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
         patch.object(org, "_ncbi_sequences", return_value=full_sequences):
        result = org.fetch_results("123", include_local=False)

    assert result["sequences"] == []
    assert result["ncbi_count"] == 0
    assert result["failed_sources"] == ["ncbi"]
    assert "full NCBI sequences" in result["errors"][0]


def test_local_metadata_preserves_observation_reference():
    status = {"local": {"status": "complete", "has_results": True}}
    metadata = {"551798": {"id": 551798, "scientificName": "Example fungus",
                            "inatObservationId": 314834254,
                            "sequence": "ACGT" * 25}}
    with patch.object(org, "_json", return_value=status), \
         patch.object(org, "_read", return_value=XML), \
         patch.object(org, "_local_metadata", return_value=metadata):
        result = org.fetch_results("123", include_ncbi=False)
    assert "iNat314834254" in result["sequences"][0]["name"]
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


def test_rerun_uses_bearer_key_and_fixed_endpoint(monkeypatch):
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
        seen["body"] = json.loads(request.data)
        return Response()

    with patch.object(org, "diagnostic_urlopen", side_effect=urlopen):
        assert org.rerun("648539", "ncbi", 100)["status"] == "queued"
    assert seen == {
        "url": "https://mycomap.org/api/mycoblast/rerun",
        "auth": "Bearer example-key",
        "body": {"id": 648539, "type": "ncbi", "limit": 100},
    }


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
