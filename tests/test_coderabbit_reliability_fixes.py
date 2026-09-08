"""Focused regressions for the eight CodeRabbit reliability findings."""

import os
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.services import alignment_service, subprocess_utils, tree_builder_service
from app.workers import events, tasks, worker_monitor


@pytest.mark.parametrize(
    ("records", "raises"),
    [
        ([('A', 'ACGT'), ('A', 'TGCA')], False),
        ([('A', 'ACGT'), ('A', 'ACGTA')], True),
        ([('A', 'ACGT'), ('B', 'TGCA')], False),
        ([('A', 'ACGT'), ('B', 'ACGTA')], True),
    ],
)
def test_already_aligned_counts_each_fasta_record(tmp_path, records, raises):
    fasta = tmp_path / "input.fasta"
    fasta.write_text("".join(f">{name}\n{sequence}\n" for name, sequence in records))

    if raises:
        with pytest.raises(RuntimeError, match="not the same length"):
            alignment_service._verify_already_aligned(fasta, Mock())
    else:
        alignment_service._verify_already_aligned(fasta, Mock())


def test_streaming_subprocess_uses_devnull_stdin():
    with patch.object(
        subprocess_utils.subprocess,
        "Popen",
        side_effect=FileNotFoundError("test executable missing"),
    ) as popen:
        exit_code, _ = subprocess_utils.run_command_streaming(["missing-tool"])

    assert exit_code == subprocess_utils.EXIT_CODE_TOOL_NOT_FOUND
    assert popen.call_args.kwargs["stdin"] is subprocess.DEVNULL


def test_streaming_internal_runner_exception_has_distinct_status():
    with patch.object(
        subprocess_utils.subprocess,
        "Popen",
        side_effect=RuntimeError("runner broke"),
    ):
        exit_code, stats = subprocess_utils.run_command_streaming(["tool"])

    error = subprocess_utils.ToolExecutionError("tool", exit_code, stats, "safe")
    assert exit_code == subprocess_utils.EXIT_CODE_RUNNER_ERROR
    assert error.failure_kind == "runner_error"
    assert error.stats["signal"] is None


def test_streaming_missing_working_directory_is_runner_error(tmp_path):
    exit_code, stats = subprocess_utils.run_command_streaming(
        ["tool"], cwd=tmp_path / "missing"
    )

    error = subprocess_utils.ToolExecutionError("tool", exit_code, stats, "safe")
    assert exit_code == subprocess_utils.EXIT_CODE_RUNNER_ERROR
    assert error.failure_kind == "runner_error"
    assert error.stats["signal"] is None


@pytest.fixture
def empty_raxml_help_cache():
    previous = tree_builder_service._RAXML_HELP_CACHE
    tree_builder_service._RAXML_HELP_CACHE = None
    yield
    tree_builder_service._RAXML_HELP_CACHE = previous


def test_successful_raxml_feature_probe_is_cached(empty_raxml_help_cache):
    config = SimpleNamespace(RAXML_BINARY="raxml-ng")
    with patch.object(
        subprocess_utils,
        "run_command",
        return_value=(0, "options: --moose --stop-rule", ""),
    ) as run:
        assert tree_builder_service._check_raxml_feature(config, "--moose")
        assert tree_builder_service._check_raxml_feature(config, "--stop-rule")
    run.assert_called_once()


def test_failed_raxml_feature_probe_does_not_poison_retry(empty_raxml_help_cache):
    config = SimpleNamespace(RAXML_BINARY="raxml-ng")
    with patch.object(
        subprocess_utils,
        "run_command",
        side_effect=[
            (1, "", "temporary loader failure"),
            (0, "", "options: --moose"),
        ],
    ) as run:
        assert not tree_builder_service._check_raxml_feature(config, "--moose")
        assert tree_builder_service._check_raxml_feature(config, "--moose")
    assert run.call_count == 2


def test_empty_raxml_feature_probe_does_not_poison_retry(empty_raxml_help_cache):
    config = SimpleNamespace(RAXML_BINARY="raxml-ng")
    with patch.object(
        subprocess_utils,
        "run_command",
        side_effect=[(0, "", ""), (0, "options: --stop-rule", "")],
    ) as run:
        assert not tree_builder_service._check_raxml_feature(config, "--stop-rule")
        assert tree_builder_service._check_raxml_feature(config, "--stop-rule")
    assert run.call_count == 2


def test_event_redis_client_has_short_explicit_timeouts():
    previous = events._redis_client
    events._redis_client = None
    try:
        with patch.object(events.redis, "from_url", return_value=Mock()) as from_url:
            events._get_redis()
        assert from_url.call_args.kwargs == {
            "socket_connect_timeout": 2.0,
            "socket_timeout": 2.0,
            "retry_on_timeout": False,
        }
    finally:
        events._redis_client = previous


class _Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


def test_rate_limiter_active_behavior_and_per_job_cleanup():
    clock = _Clock()
    limiter = events.RateLimiter(idle_ttl=30, cleanup_interval=1, clock=clock)

    assert limiter.try_consume("one", "align", "stderr")
    assert limiter.try_consume("two", "tree", "stdout")
    assert limiter.should_warn_overflow("one", "align", "stderr")
    one_key = limiter._bucket_key("one", "align", "stderr")
    two_key = limiter._bucket_key("two", "tree", "stdout")

    limiter.forget_job("one")

    assert one_key not in limiter._buckets
    assert one_key not in limiter._last_overflow_warning
    assert two_key in limiter._buckets
    assert limiter._buckets[two_key]["tokens"] == events.MAX_LINES_PER_SECOND - 1


def test_rate_limiter_evicts_idle_bucket_and_warning_state():
    clock = _Clock()
    limiter = events.RateLimiter(idle_ttl=5, cleanup_interval=1, clock=clock)
    assert limiter.try_consume("stale", "align", "stderr")
    assert limiter.should_warn_overflow("stale", "align", "stderr")
    stale_key = limiter._bucket_key("stale", "align", "stderr")

    clock.value += 6
    assert limiter.try_consume("active", "tree", "stdout")

    assert stale_key not in limiter._buckets
    assert stale_key not in limiter._last_overflow_warning
    assert "stale" not in limiter._job_keys


def test_terminal_event_forgets_rate_limiter_state():
    limiter = events.RateLimiter()
    limiter.try_consume("done", "align", "stderr")
    with patch.object(events, "_rate_limiter", limiter), patch.object(events, "publish_event"):
        events.publish_job_completed("done", "/job/done/view")
    assert "done" not in limiter._job_keys


def test_failed_tool_diagnostics_flow_from_exact_invocation(tmp_path):
    failed_stats = {
        "duration_seconds": 3.25,
        "stdout_lines": 7,
        "stderr_lines": 2,
        "stdout_tail": [">private", "ACGT" * 20, "allocation failed"],
        "stderr_tail": ["fatal: controlled failure"],
    }
    config = SimpleNamespace(
        MUSCLE_BINARY="muscle",
        SUBPROCESS_MEMORY_LIMIT_MB=0,
        SUBPROCESS_CPU_LIMIT_SECONDS=0,
        MUSCLE_TIME_LIMIT_HOURS=8,
    )
    params = SimpleNamespace()
    with (
        patch.object(alignment_service, "run_command_streaming", return_value=(-24, failed_stats)),
        patch.object(events, "publish_command"),
        pytest.raises(subprocess_utils.ToolExecutionError) as caught,
    ):
        alignment_service._run_muscle(
            tmp_path / "input.fasta",
            tmp_path / "output.fasta",
            params,
            config,
            Mock(),
            job_id="job-id",
        )

    diagnostic = tasks.failure_diagnostics(caught.value, fallback_tool="old-success")
    assert diagnostic["tool"] == "MUSCLE"
    assert diagnostic["exit_code"] == -24
    assert diagnostic["failure_kind"] == "cpu_limit"
    assert diagnostic["stats"]["duration_seconds"] == 3.25
    assert diagnostic["stats"]["stderr_tail"] == ["fatal: controlled failure"]
    assert diagnostic["stats"]["stdout_tail"][:2] == [
        "[sequence output omitted]",
        "[sequence output omitted]",
    ]
    persisted = tasks.failure_metric_updates(
        str(caught.value), "align", "Alignment (MUSCLE)", diagnostic
    )
    assert persisted["failed_step"] == "align"
    assert persisted["failed_tool"] == "MUSCLE"
    assert persisted["exit_code"] == -24
    assert persisted["failure_kind"] == "cpu_limit"
    assert persisted["failed_tool_signal"] == 24
    assert persisted["failed_tool_duration_seconds"] == 3.25

    unrelated = tasks.failure_diagnostics(RuntimeError("later failure"), "tree")
    assert unrelated["tool"] == "tree"
    assert unrelated["exit_code"] is None
    assert unrelated["stats"] == {}
    unrelated_persisted = tasks.failure_metric_updates(
        "later failure", "tree", "Tree Building", unrelated
    )
    assert unrelated_persisted["exit_code"] is None
    assert unrelated_persisted["failed_tool_duration_seconds"] is None


@pytest.mark.parametrize(
    ("exit_code", "kind", "signal_number"),
    [
        (subprocess_utils.EXIT_CODE_RUNNER_ERROR, "runner_error", None),
        (subprocess_utils.EXIT_CODE_JOB_TIMEOUT, "timeout", None),
        (subprocess_utils.EXIT_CODE_TOOL_NOT_FOUND, "launch_failure", None),
        (-24, "cpu_limit", 24),
        (-15, "interrupted", 15),
        (-9, "forced_kill", 9),
        (-1, "signal", 1),
        (7, "nonzero_exit", None),
    ],
)
def test_tool_failure_status_classification(exit_code, kind, signal_number):
    error = subprocess_utils.ToolExecutionError("tool", exit_code, {}, "safe")
    assert error.failure_kind == kind
    assert error.stats["signal"] == signal_number


def test_diagnostic_tail_redacts_long_embedded_iupac_run():
    sequence = "ACGT" * 20
    [redacted] = subprocess_utils._bounded_diagnostic_tail([
        f"Invalid sequence {sequence} at record foo"
    ])

    assert redacted == "Invalid sequence [sequence output omitted] at record foo"
    assert sequence not in redacted


def test_diagnostic_tail_preserves_short_sequence_and_ordinary_text():
    lines = [
        "Invalid motif ACGT at record foo",
        "Background worker could not validate arguments",
    ]
    assert subprocess_utils._bounded_diagnostic_tail(lines) == lines


def test_heartbeat_worker_forwards_rq_options_by_keyword(tmp_path):
    job_class = object()
    queue_class = object()
    handler = object()
    connection = object()
    with (
        patch.object(worker_monitor.Worker, "__init__", return_value=None) as parent_init,
        patch.object(worker_monitor.atexit, "register"),
    ):
        worker_monitor.HeartbeatWorker(
            ["high"],
            name="worker-name",
            default_result_ttl=321,
            connection=connection,
            exc_handler=handler,
            default_worker_ttl=654,
            job_class=job_class,
            queue_class=queue_class,
            worker_dir=tmp_path / "workers",
        )

    parent_init.assert_called_once_with(
        ["high"],
        name="worker-name",
        default_result_ttl=321,
        connection=connection,
        exc_handler=handler,
        default_worker_ttl=654,
        job_class=job_class,
        queue_class=queue_class,
    )


def _bare_heartbeat_worker(worker_dir):
    worker = object.__new__(worker_monitor.HeartbeatWorker)
    worker.worker_dir = worker_dir
    worker.name = "test-worker"
    return worker


def test_heartbeat_touch_updates_existing_file(tmp_path):
    worker = _bare_heartbeat_worker(tmp_path)
    heartbeat = tmp_path / "test-worker.heartbeat"
    heartbeat.touch()
    # Backdate explicitly. Two touches inside one filesystem timestamp tick are
    # equal, so ">=" passed even when touch_heartbeat_file() did nothing at all
    # -- the staleness check this file exists to protect would then never fire.
    stale = time.time() - 3600
    os.utime(heartbeat, (stale, stale))
    before = heartbeat.stat().st_mtime_ns
    worker.touch_heartbeat_file()
    assert heartbeat.stat().st_mtime_ns > before


def test_heartbeat_touch_recreates_missing_directory(tmp_path):
    worker_dir = tmp_path / "missing" / "workers"
    worker = _bare_heartbeat_worker(worker_dir)
    worker.touch_heartbeat_file()
    assert (worker_dir / "test-worker.heartbeat").is_file()


def test_heartbeat_touch_failure_propagates_and_next_iteration_recovers(tmp_path):
    worker_dir = tmp_path / "workers"
    worker = _bare_heartbeat_worker(worker_dir)
    with patch.object(worker_dir.__class__, "mkdir", side_effect=OSError("read only")):
        with pytest.raises(OSError, match="read only"):
            worker.touch_heartbeat_file()

    worker.touch_heartbeat_file()
    assert (worker_dir / "test-worker.heartbeat").is_file()
