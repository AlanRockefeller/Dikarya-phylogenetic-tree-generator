"""A web-request BLAST keeps one deadline across every NCBI call."""

from unittest.mock import MagicMock, patch

import pytest

from app.services import blast_service as bs


def _response(status, headers=None):
    response = MagicMock(status_code=status, headers=headers or {})
    return response


def test_retry_wait_past_the_budget_raises_instead_of_sleeping():
    token = bs._request_deadline.set(bs.time.monotonic() + 30)
    try:
        with patch.object(bs.requests, "request",
                          return_value=_response(429, {"Retry-After": "120"})), \
             patch.object(bs, "record_requests_failure"), \
             patch.object(bs.time, "sleep") as sleep:
            with pytest.raises(TimeoutError):
                bs._ncbi_request("GET", bs.NCBI_BLAST_URL, timeout=(5, 30))
        assert all(call.args[0] < 1 for call in sleep.call_args_list)
    finally:
        bs._request_deadline.reset(token)


def test_request_timeout_is_clamped_to_remaining_budget():
    token = bs._request_deadline.set(bs.time.monotonic() + 10)
    try:
        with patch.object(bs.requests, "request", return_value=_response(200)) as request:
            bs._ncbi_request("GET", bs.NCBI_BLAST_URL, timeout=(15, 120))
        connect, read = request.call_args.kwargs["timeout"]
        assert connect <= 10 and read <= 10
    finally:
        bs._request_deadline.reset(token)


def test_spent_budget_is_not_cached_as_a_partial_result(tmp_path):
    config = MagicMock(BLAST_CACHE_DIR=str(tmp_path))
    now = [0.0]

    def slow_fasta(_accessions):
        # The FASTA fetch degrades instead of raising; the budget runs out inside it.
        now[0] = 500.0
        return ""

    with patch.object(bs, "_check_cache", return_value=None), \
         patch.object(bs, "_submit_blast_request", return_value=("RID", 0)), \
         patch.object(bs, "_poll_blast"), \
         patch.object(bs, "_fetch_blast_results",
                      return_value={"accessions": ["AB123456"], "hit_details": []}), \
         patch.object(bs, "fetch_fasta_for_accessions", side_effect=slow_fasta), \
         patch.object(bs, "_save_cache") as save, \
         patch.object(bs.time, "monotonic", side_effect=lambda: now[0]):
        with pytest.raises(TimeoutError):
            bs.blast_from_sequence("ACGT", config, max_wait=240)
    save.assert_not_called()
    assert bs._request_deadline.get() is None


def test_worker_call_has_no_request_deadline():
    with patch.object(bs, "_check_cache", return_value=None), \
         patch.object(bs, "_submit_blast_request", return_value=("RID", 0)), \
         patch.object(bs, "_poll_blast") as poll, \
         patch.object(bs, "_fetch_blast_results",
                      return_value={"accessions": [], "hit_details": []}), \
         patch.object(bs, "fetch_fasta_for_accessions", return_value=""), \
         patch.object(bs, "_save_cache", return_value={}):
        bs.blast_from_sequence("ACGT", MagicMock())
    assert poll.call_args.kwargs["max_wait"] == bs.BLAST_MAX_WAIT_SECONDS


def test_rate_limit_near_deadline_in_fasta_fetch_is_not_cached():
    # 5s left when NCBI answers the GenBank fetch with 429 Retry-After: 120.
    # The fetch helpers degrade on ordinary failures; this one must propagate.
    now = [0.0]

    def fetch_results(_rid, _limit):
        now[0] = 235.0
        return {"accessions": ["AB123456"], "hit_details": []}

    with patch.object(bs, "_check_cache", return_value=None), \
         patch.object(bs, "_submit_blast_request", return_value=("RID", 0)), \
         patch.object(bs, "_poll_blast"), \
         patch.object(bs, "_fetch_blast_results", side_effect=fetch_results), \
         patch.object(bs.requests, "request",
                      return_value=_response(429, {"Retry-After": "120"})), \
         patch.object(bs, "record_requests_failure"), \
         patch.object(bs, "_report_unresolved_accessions"), \
         patch.object(bs, "_save_cache") as save, \
         patch.object(bs.time, "sleep"), \
         patch.object(bs.time, "monotonic", side_effect=lambda: now[0]):
        with pytest.raises(TimeoutError):
            bs.blast_from_sequence("ACGT", MagicMock(), max_wait=240)
    save.assert_not_called()
