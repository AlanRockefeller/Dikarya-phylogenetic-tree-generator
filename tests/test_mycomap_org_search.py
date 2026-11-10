"""Raw-query MycoMap.org searches use the public MycoBLAST contract."""

import io
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from app.services import mycomap_org_service as org


QUERY = "ACGT" * 25


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture
def search(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(org, "time", clock)
    submit = Mock(return_value=b'{"job_id":1234}')
    statuses = Mock(return_value={
        "status": "complete", "local": {"status": "complete", "has_results": True},
    })
    result = {
        "sequences": [{
            "name": "MycoMap#42 iNat123 Example fungus", "sequence": QUERY,
            "identity": 98.5, "query_cover": 100, "subject_cover": 95,
            "source": "mycomap", "hit_source": "local", "mycomap_sequence_id": "42",
            "location": "USA Arizona", "blast_metrics_available": True,
        }],
        "ncbi_count": 0, "local_count": 1, "errors": [],
        "pending_sources": [], "failed_sources": [],
        "ncbi_queue_position": None, "metrics_by_key": {},
    }
    fetch = Mock(return_value=result)
    monkeypatch.setattr(org, "_read", submit)
    monkeypatch.setattr(org, "_json", statuses)
    monkeypatch.setattr(org, "fetch_results", fetch)
    return SimpleNamespace(clock=clock, submit=submit, statuses=statuses,
                           fetch=fetch, result=result)


def test_local_search_submits_single_query_and_preserves_full_import_metadata(search):
    search.statuses.side_effect = [
        {"status": "processing", "local": {"status": "queued", "has_results": False}},
        {"status": "complete", "local": {"status": "complete", "has_results": True}},
    ]
    result = org.search_from_query(
        ">private sample header\n; private comment\n1 acgu acgt\n" + "ACGT" * 23,
        min_identity=95, max_sequences=101, time_budget=40,
    )
    search.submit.assert_called_once_with(
        "submit", method="POST", authenticated=True, deadline=140,
        body={"type": "sequence", "input": QUERY, "limit": 250, "localOnly": True},
    )
    assert search.clock.waits == [2]
    search.fetch.assert_called_once_with(
        "1234", include_ncbi=False, include_local=True, time_budget=38,
    )
    assert result["sequences"][0]["mycomap_sequence_id"] == "42"
    assert result["sequences"][0]["query_cover"] == 100
    assert result["sequences"][0]["location"] == "USA Arizona"
    assert result["result_id"] == "1234"
    assert result["result_url"] == "https://mycomap.org/mycoblast/1234"
    assert result["metrics_by_key"] == {}
    assert result["ncbi_count"] == 0


def test_accession_uses_genbank_query_without_resolving_or_mutating_sequence_record(search):
    org.search_from_query("  or807397.1  ", max_sequences=5)
    assert search.submit.call_args.kwargs["body"] == {
        "type": "genbank", "input": "OR807397.1", "limit": 50, "localOnly": True,
    }


def test_identity_filter_and_exact_hit_limit_are_applied_after_upstream_rounding(search):
    search.result["sequences"] = [
        {"name": "below cutoff", "identity": 89},
        {"name": "at cutoff", "identity": 90},
        {"name": "higher", "identity": 100},
        {"name": "unknown", "identity": None},
    ]
    result = org.search_from_query(QUERY, min_identity=90, max_sequences=1)
    assert result["sequences"] == [{"name": "at cutoff", "identity": 90}]
    assert result["local_count"] == 1
    assert search.submit.call_args.kwargs["body"]["limit"] == 50


def test_completed_search_with_zero_hits_succeeds(search):
    search.result["sequences"] = []
    search.result["local_count"] = 0
    result = org.search_from_query(QUERY)
    assert result["sequences"] == []
    assert result["local_count"] == 0


@pytest.mark.parametrize("query", [
    "", "garbage !", ">first\n" + QUERY + "\n>second\n" + QUERY,
    "A" * 49, "A" * 20_001, "N" * 100, "AAAA01000000", "ACGT-" * 20,
])
def test_invalid_queries_never_create_upstream_search(search, query):
    with pytest.raises(ValueError):
        org.search_from_query(query)
    search.submit.assert_not_called()


@pytest.mark.parametrize("reply", [b"not JSON", b"[]", b"{}", b'{"job_id":"../1"}',
                                    b'{"job_id":0}', b'{"job_id":true}'])
def test_invalid_submission_response_does_not_poll(search, reply):
    search.submit.return_value = reply
    with pytest.raises(org.OrgResultError):
        org.search_from_query(QUERY)
    search.statuses.assert_not_called()


@pytest.mark.parametrize("status", [
    {"status": "error", "local": {}},
    {"status": "complete", "local": {"status": "failed", "has_results": False}},
    {"status": "complete", "local": {"status": "complete", "has_results": False}},
    {"status": "processing", "local": ["invalid"]},
])
def test_failed_or_unpublished_results_are_not_empty_success(search, status):
    search.statuses.return_value = status
    with pytest.raises(org.OrgResultError):
        org.search_from_query(QUERY)
    search.fetch.assert_not_called()


@pytest.mark.parametrize("field", ["errors", "pending_sources", "failed_sources"])
def test_failed_final_result_fetch_never_becomes_empty_success(search, field):
    search.result[field] = ["local"]
    with pytest.raises(org.OrgResultError):
        org.search_from_query(QUERY)


def test_polling_stops_at_shared_deadline(search):
    search.statuses.return_value = {
        "status": "processing", "local": {"status": "running", "has_results": False},
    }
    with pytest.raises(TimeoutError, match="did not finish"):
        org.search_from_query(QUERY, time_budget=3)
    assert search.clock.waits == [2, 1]
    assert search.statuses.call_count == 2
    search.fetch.assert_not_called()


def test_submission_and_result_fetch_share_poll_budget(search):
    def submit(*args, **kwargs):
        search.clock.now += 5
        return b'{"job_id":1234}'

    search.submit.side_effect = submit
    org.search_from_query(QUERY, time_budget=20)
    search.fetch.assert_called_once_with(
        "1234", include_ncbi=False, include_local=True, time_budget=15,
    )


def test_auth_refusal_is_propagated_without_polling_or_logging_query(search, caplog):
    refusal = org.OrgResultError("MycoMap.org did not accept Dikarya's access key.",
                                 retryable=False)
    search.submit.side_effect = refusal
    with pytest.raises(org.OrgResultError) as caught:
        org.search_from_query(">private identifier\n" + QUERY)
    assert caught.value is refusal
    search.statuses.assert_not_called()
    assert QUERY not in caplog.text
    assert "private identifier" not in caplog.text


def test_success_log_omits_query_and_fasta_header(search, caplog):
    with caplog.at_level("INFO", logger=org.__name__):
        org.search_from_query(">private identifier\n" + QUERY)
    assert "result_id=1234" in caplog.text
    assert QUERY not in caplog.text
    assert "private identifier" not in caplog.text


def test_submission_auth_refusal_suggests_available_ncbi_search(monkeypatch):
    monkeypatch.setenv("MYCOMAP_ORG_API_KEY", "test-key")
    refusal = HTTPError(org.ORG_API + "/submit", 401, "Unauthorized", {}, io.BytesIO())
    monkeypatch.setattr(org, "diagnostic_urlopen", Mock(side_effect=refusal))
    with pytest.raises(org.OrgResultError, match="use NCBI BLAST") as caught:
        org._read("submit", method="POST", authenticated=True, body={})
    assert caught.value.retryable is False
