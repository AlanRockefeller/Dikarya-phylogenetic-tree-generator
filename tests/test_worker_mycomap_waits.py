"""Structural guards for the MycoMap waits in run_phylo_job (app/workers/tasks.py).

run_phylo_job needs Redis, RQ and a job row to execute, so these read the source,
as the other worker guards in this suite do.
"""

from pathlib import Path

TASKS = (Path(__file__).resolve().parents[1] / "app" / "workers" / "tasks.py").read_text()


def _inat_rate_limit_handler():
    start = TASKS.index("deferral = _defer_for_inat_rate_limit(job, job_id, exc)")
    return TASKS[start:TASKS.index("return deferral", start)]


def test_a_rate_limited_resume_keeps_its_ncbi_wait():
    # Every pass marks the preparation "running" on entry, so a pass that was
    # resuming an NCBI wait has to put "waiting_for_ncbi" back before deferring,
    # or the retry redoes the MycoMap refresh it already made.
    handler = _inat_rate_limit_handler()
    assert '= "waiting_for_ncbi"' in handler


def test_a_rate_limited_first_pass_is_not_marked_as_waiting():
    # A first pass has no wait to resume; marking it waiting would make the retry
    # skip a MycoMap refresh that never happened.
    handler = _inat_rate_limit_handler()
    guard = handler.index("if tree_resuming_after_ncbi:")
    assert guard < handler.index('= "waiting_for_ncbi"')


def test_waiting_for_a_creation_slot_has_its_own_allowance():
    block = TASKS[TASKS.index('throttle_wait = bool(rerun_details.get("creation_throttled"))'):]
    block = block[:block.index("wait_attempts = (")]
    assert '"mycomap_throttle_waits"' in block
    assert "get_mycomap_bulk_throttle_max_wait_attempts()" in block
    polling = block[block.index("else:"):]
    assert "get_mycomap_bulk_throttle_max_wait_attempts" not in polling
    assert "job.meta[wait_counter_key] = wait_attempts" in TASKS


def test_an_unreachable_mycomap_defers_instead_of_failing():
    # A MycoMap blip while resolving a legacy link must wait, not fail the job
    # (inaturalist_tree_service raises it with details={"mycomap_unavailable": True}).
    helper = TASKS[TASKS.index("def _defer_for_inat_rate_limit("):]
    helper = helper[:helper.index("\ndef ", 1)]
    assert 'details.get("mycomap_unavailable")' in helper
    assert '"mycomap_unavailable_deferrals"' in helper
