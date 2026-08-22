"""Regression coverage for Dikarya's logging, telemetry and log-digest behaviour.

These tests exist because the observability layer is exactly the code that has no
user-visible symptom when it breaks: the worker kept running perfectly while its
log went silent, and the digest kept printing while its numbers were wrong.
"""

import gzip
import importlib.util
import logging
import os
import sys
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# create_app() refuses the unconfigured SQLite fallback so a production script
# cannot silently query a stale local app.db; opt in explicitly for these tests.
os.environ.setdefault("ALLOW_SQLITE_FALLBACK", "1")

REPO_ROOT = Path(__file__).resolve().parents[1]


def _digest_module():
    path = REPO_ROOT / "scripts" / "dikarya_log_digest.py"
    spec = importlib.util.spec_from_file_location("dikarya_log_digest_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_logging():
    """Give a test the logging state a fresh CLI process starts with, then restore.

    Every installer under test mutates the process-wide root logger, so without
    this a passing test could be an artefact of an earlier one's handlers.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_levels = {
        name: logging.getLogger(name).level
        for name in ("rq", "rq.worker", "rq.scheduler", "app.workers.tasks")
    }
    root.handlers = []
    root.setLevel(logging.WARNING)  # the default a bare `flask ...` process has
    try:
        yield root
    finally:
        for handler in root.handlers:
            if handler not in saved_handlers:
                try:
                    handler.close()
                except Exception:
                    pass
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)


class _Capture(logging.Handler):
    """Collect records (and their formatted text) emitted anywhere in the process."""

    def __init__(self, level=logging.NOTSET):
        super().__init__(level)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    @property
    def text(self):
        return "\n".join(record.getMessage() for record in self.records)


def _make_app(tmp_path, config_name="development"):
    """Build the real application with its errors mirror pointed at a temp file.

    var/logs is dikarya-owned, so the real path is unwritable from the test user;
    overriding the config class is what lets the mirror actually be exercised.
    """
    from app.config import config as config_map

    config_class = config_map[config_name]
    original_path = config_class.ERROR_LOG_PATH
    had_limit = hasattr(config_class, "RATELIMIT_ENABLED")
    original_limit = getattr(config_class, "RATELIMIT_ENABLED", None)
    config_class.ERROR_LOG_PATH = tmp_path / "errors.log"
    # The limiter's storage is Redis, which is not reachable from the test
    # sandbox; rate limiting is not what these tests are about.
    config_class.RATELIMIT_ENABLED = False
    try:
        from app import create_app

        app = create_app(config_name)
    finally:
        config_class.ERROR_LOG_PATH = original_path
        if had_limit:
            config_class.RATELIMIT_ENABLED = original_limit
        else:
            delattr(config_class, "RATELIMIT_ENABLED")
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return app


@pytest.fixture(autouse=True)
def restore_rate_limiter():
    """flask-limiter is a module-level singleton; do not leak a disabled one."""
    from app.extensions import limiter

    saved = limiter.enabled
    yield
    limiter.enabled = saved


@pytest.fixture
def no_telemetry_dedup():
    """Make the endpoint's cross-process dedup a no-op.

    /api/log/client suppresses a repeat of the same fingerprint for 120s via
    Redis. On a host where Redis is actually reachable that makes these tests
    order- and rerun-dependent, so the dedup is stubbed to "not seen before".
    """
    connection = Mock()
    connection.set.return_value = True
    with patch("app.workers.queue.get_redis_connection", return_value=connection):
        yield connection


# ---------------------------------------------------------------------------
# Bounded job parameter summaries
# ---------------------------------------------------------------------------

def test_job_parameter_summary_is_bounded_and_excludes_payloads():
    from app.workers.tasks import summarize_job_params

    secret_sequence = "ACGT" * 60_000
    params = {
        "input_type": "fasta",
        "sequence": f">private specimen note\n{secret_sequence}\n",
        "notes": "private free text",
        "authorization": "Bearer secret-token",
        "sequence_metadata": [
            {"name": "private specimen", "source": "mycomap", "sequence": secret_sequence}
        ],
        "alignment_method": "mafft",
        "tree_method": "iqtree",
    }
    summary = summarize_job_params(params)
    rendered = str(summary)

    assert summary["sequence_count"] == 1
    assert summary["total_bases"] == len(secret_sequence)
    assert summary["imported_record_count"] == 1
    assert summary["options"]["alignment_method"] == "mafft"
    assert len(summary["parameter_fingerprint"]) == 16
    assert len(rendered) < 1000
    assert "ACGTACGT" not in rendered
    assert "private specimen" not in rendered
    assert "secret-token" not in rendered
    assert "private free text" not in rendered


def test_job_parameter_summary_normalizes_unknown_source_labels():
    """`source` is user-controlled metadata, so it cannot be echoed verbatim."""
    from app.workers.tasks import summarize_job_params

    summary = summarize_job_params({
        "sequence_metadata": [
            {"source": "MycoMap"},
            {"source": "collected by Jane Doe at 44.1N, notes: Bearer abc123"},
            {"hit_source": "ncbi"},
        ],
    })

    assert summary["sources"] == {"mycomap": 1, "ncbi": 1, "other": 1}
    assert "Jane Doe" not in str(summary)


# ---------------------------------------------------------------------------
# Context formatting and request correlation
# ---------------------------------------------------------------------------

def test_context_formatter_keeps_context_on_exception_first_line():
    from app.services.log_context import ContextFormatter

    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "failure", (), None)
    record.req = "req123"
    record.job = "job123"
    record.release = "release123"
    rendered = ContextFormatter("%(levelname)s %(message)s").format(record)
    assert rendered.splitlines()[0].endswith("[req=req123 job=job123 release=release123]")


def test_request_id_in_response_header_matches_the_log_record(tmp_path, clean_logging, no_telemetry_dedup):
    """The X-Request-Id a user can quote must be the one grep finds in the log."""
    app = _make_app(tmp_path)
    capture = _Capture()
    logging.getLogger().addHandler(capture)

    response = app.test_client().post(
        "/api/log/client",
        json={"event": "window_error", "message": "boom", "pathname": "/tree"},
    )

    assert response.status_code == 200
    request_id = response.headers["X-Request-Id"]
    assert request_id and request_id != "-"
    telemetry = [r for r in capture.records if "event=client.window_error" in r.getMessage()]
    assert telemetry, "the telemetry endpoint logged nothing"
    assert getattr(telemetry[0], "req", None) == request_id


# ---------------------------------------------------------------------------
# Worker / RQ logging initialization
# ---------------------------------------------------------------------------

def test_worker_logging_initialization_enables_info_and_rq_console(tmp_path, clean_logging):
    """Reproduce the real worker startup order and assert nothing is muted.

    Order: a default CLI logging state, then create_app() installing the errors
    mirror, then RQ's own logging bootstrap, then the task module import. The
    regression this guards is subtle -- everything still "works", the log just
    stops containing anything below WARNING.
    """
    from rq.logutils import _has_effective_handler, setup_loghandlers

    from app.services.log_context import CONSOLE_MARKER, ERROR_MIRROR_MARKER

    root = clean_logging
    # The state a bare `flask run-worker` starts in. (pytest attaches its own
    # capture handlers to the root logger, so only ours are checked here.)
    assert not [
        handler for handler in root.handlers
        if getattr(handler, CONSOLE_MARKER, False) or getattr(handler, ERROR_MIRROR_MARKER, False)
    ]

    app = _make_app(tmp_path)

    # 1. The WARNING mirror is still there, still WARNING-only.
    mirrors = [h for h in root.handlers if getattr(h, ERROR_MIRROR_MARKER, False)]
    assert len(mirrors) == 1
    assert mirrors[0].level == logging.WARNING

    # 2. Application INFO is enabled again (this is what basicConfig used to do
    #    and stopped doing once the mirror occupied the root logger).
    assert logging.getLogger("app.workers.tasks").isEnabledFor(logging.INFO)
    assert root.level <= logging.INFO

    # 3. RQ's bootstrap runs next. It skips installing its own handlers when a
    #    handler is already effective, so the console path has to be ours.
    setup_loghandlers(level="INFO", name="rq.worker")
    rq_logger = logging.getLogger("rq.worker")
    assert _has_effective_handler(rq_logger)
    assert rq_logger.isEnabledFor(logging.INFO)

    console = [h for h in root.handlers if getattr(h, CONSOLE_MARKER, False)]
    assert {h.stream for h in console} == {sys.stdout, sys.stderr}

    # 4. Importing the task module must not reconfigure anything.
    import app.workers.tasks as tasks  # noqa: F401

    assert logging.getLogger("app.workers.tasks").isEnabledFor(logging.INFO)
    assert root.level <= logging.INFO

    # 5. An INFO record reaches the console but never the WARNING mirror.
    capture = _Capture()
    root.addHandler(capture)
    logging.getLogger("app.workers.tasks").info("event=job.started probe")
    assert any("event=job.started probe" in r.getMessage() for r in capture.records)
    assert app.config["ERROR_LOG_PATH"].read_text().count("event=job.started probe") == 0


def test_gunicorn_processes_get_no_duplicate_console_handler(tmp_path, clean_logging):
    """Under Gunicorn, stdout is already captured; a console handler would double it."""
    from app.services.log_context import CONSOLE_MARKER

    with patch.dict(os.environ, {}, clear=False):
        gunicorn_logger = logging.getLogger("gunicorn.error")
        marker_handler = logging.NullHandler()
        gunicorn_logger.addHandler(marker_handler)
        try:
            _make_app(tmp_path)
        finally:
            gunicorn_logger.removeHandler(marker_handler)

    assert not [h for h in clean_logging.handlers if getattr(h, CONSOLE_MARKER, False)]


def test_repeated_app_creation_does_not_duplicate_handlers_or_leak_fds(tmp_path, clean_logging):
    """The worker and several CLI commands call create_app() more than once."""
    from app.services.log_context import CONSOLE_MARKER, ERROR_MIRROR_MARKER

    def open_error_log_fds():
        fd_dir = Path("/proc/self/fd")
        if not fd_dir.is_dir():
            return None
        found = 0
        for entry in fd_dir.iterdir():
            try:
                if entry.resolve() == (tmp_path / "errors.log").resolve():
                    found += 1
            except OSError:
                continue
        return found

    _make_app(tmp_path)
    after_first = open_error_log_fds()
    for _ in range(3):
        _make_app(tmp_path)

    root = clean_logging
    assert len([h for h in root.handlers if getattr(h, ERROR_MIRROR_MARKER, False)]) == 1
    assert len([h for h in root.handlers if getattr(h, CONSOLE_MARKER, False)]) == 2
    if after_first is not None:
        assert open_error_log_fds() == after_first

    # And a single record is written once, not four times.
    logging.getLogger("app.probe").warning("duplicate check")
    assert (tmp_path / "errors.log").read_text().count("duplicate check") == 1


def test_per_job_handler_only_receives_its_own_job(tmp_path, clean_logging):
    from app.services.log_context import JobContextFilter, background_context

    from app.services.log_context import ensure_root_level, install_console_logging

    install_console_logging()
    ensure_root_level(logging.INFO)
    log_path = tmp_path / "pipeline.log"
    handler = logging.FileHandler(log_path)
    handler.setLevel(logging.INFO)
    handler.addFilter(JobContextFilter("job-a"))
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    try:
        with background_context(job="job-a"):
            logging.getLogger("app.services.probe").info("mine at INFO")
        with background_context(job="job-b"):
            logging.getLogger("app.services.probe").info("not mine")
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()

    written = log_path.read_text()
    assert "mine at INFO" in written
    assert "not mine" not in written


def test_release_is_resolved_once_per_process(clean_logging):
    """configured_release() is called for every log record; it must not stat .git."""
    from app.services import log_context

    log_context.reset_release_cache()
    calls = []
    real = log_context._read_git_head

    def counting(git_dir):
        calls.append(git_dir)
        return real(git_dir)

    with patch.dict(os.environ, {}, clear=False):
        for key in ("DIKARYA_RELEASE", "RELEASE_VERSION", "GIT_COMMIT"):
            os.environ.pop(key, None)
        with patch.object(log_context, "_read_git_head", counting):
            log_context.install_record_factory()
            first = log_context.configured_release()
            for index in range(50):
                logging.getLogger("app.probe").debug("record %s", index)
            second = log_context.configured_release()

    assert first == second
    assert len(calls) <= 1, f".git was read {len(calls)} times"
    log_context.reset_release_cache()


# ---------------------------------------------------------------------------
# Telemetry privacy
# ---------------------------------------------------------------------------

def test_telemetry_sanitizer_removes_credentials_queries_and_sequences():
    from app.services.log_context import sanitize_telemetry_text

    sequence = "ACGTTTGGATCATTAGGAAGCACGTNNNTTGGCA" * 4
    raw = (
        "TypeError: cannot read 'length'\n"
        f"    at parseFasta (https://dikarya.us/static/js/x.js?code=4/0AY0e&state=xyz:12:3)\n"
        "    Authorization: Bearer abcdef0123456789abcdef\n"
        "    cookie: session=deadbeefdeadbeefdeadbeef\n"
        "    token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVP\n"
        f"    query={sequence}\n"
        "    api_key=AKIAIOSFODNN7EXAMPLEKEYVALUE12345\n"
    )
    cleaned = sanitize_telemetry_text(raw)

    # Nothing sensitive survives.
    for secret in (
        "abcdef0123456789abcdef", "deadbeefdeadbeef", "code=4/0AY0e", "state=xyz",
        "AKIAIOSFODNN7EXAMPLEKEYVALUE12345", "eyJhbGciOiJIUzI1NiJ9",
    ):
        assert secret not in cleaned, secret
    assert sequence not in cleaned
    assert "ACGTTTGGATCATTAGG" not in cleaned

    # Everything needed to debug does.
    assert "TypeError" in cleaned
    assert "parseFasta" in cleaned
    assert "/static/js/x.js" in cleaned
    assert f"<sequence:{len(sequence)}>" in cleaned
    assert "\n" not in cleaned and "\r" not in cleaned


def test_telemetry_sanitizer_bounds_length_and_strips_control_characters():
    from app.services.log_context import sanitize_telemetry_text

    long_message = "failure " * 1000
    assert len(sanitize_telemetry_text(long_message, 100)) == 100
    assert sanitize_telemetry_text(long_message, 100).startswith("failure failure")
    # A long run of nucleotide letters is treated as sequence wherever it appears,
    # and any other long opaque blob is treated as a possible key.
    assert sanitize_telemetry_text("A" * 60) == "<sequence:60>"
    assert sanitize_telemetry_text("z" * 60) == "<redacted-token>"
    # Paths and dotted identifiers keep their shape: the lookarounds exclude
    # anything adjacent to . / - so stack frames stay readable.
    assert sanitize_telemetry_text("at /static/js/tree_viewer_controller.js:1420:15") == (
        "at /static/js/tree_viewer_controller.js:1420:15"
    )
    assert sanitize_telemetry_text("of\x00fe\x07nd\x1bing") == "offending"
    assert sanitize_telemetry_text(None) == ""


def test_telemetry_endpoint_never_logs_raw_sequences_or_tokens(tmp_path, clean_logging, no_telemetry_dedup):
    app = _make_app(tmp_path)
    capture = _Capture()
    logging.getLogger().addHandler(capture)

    fasta = ">Amanita muscaria voucher AR-1234\n" + "ACGTACGTTTGGCATTAGGCACGTAAGC" * 20
    response = app.test_client().post("/api/log/client", json={
        "event": "ui_action_failed",
        "action": "tree_builder.submit_job",
        "message": f"failed to submit {fasta}",
        "pathname": "/tree?code=4/0AY0e-secretcode&state=abc",
        "stack": (
            "Error: boom\n at submit (https://dikarya.us/tree?token=supersecrettokenvalue)\n"
            " Authorization: Bearer sk-live-0123456789abcdefghij\n"
        ),
    })

    assert response.status_code == 200
    logged = capture.text
    assert "ACGTACGTTTGGCA" not in logged
    assert "supersecrettokenvalue" not in logged
    assert "sk-live-0123456789abcdefghij" not in logged
    assert "0AY0e-secretcode" not in logged
    # Still diagnosable.
    assert "tree_builder.submit_job" in logged
    assert "pathname=/tree" in logged


def test_telemetry_endpoint_ignores_unknown_events(tmp_path, clean_logging, no_telemetry_dedup):
    app = _make_app(tmp_path)
    capture = _Capture()
    logging.getLogger().addHandler(capture)

    response = app.test_client().post(
        "/api/log/client", json={"event": "made_up", "message": "hello"}
    )

    assert response.get_json() == {"status": "ignored"}
    assert "hello" not in capture.text


# ---------------------------------------------------------------------------
# Safe RQ descriptions
# ---------------------------------------------------------------------------

def _sensitive_params():
    return {
        "input_type": "fasta",
        "sequence": ">Amanita voucher AR-9 private note\n" + "ACGTACGTACGTACGTACGTACGT" * 50,
        "notes": "collector private note",
        "tree_method": "raxml",
        "mycomap_api_key": "0123456789abcdef0123456789abcdef",
        "sequence_metadata": [{"name": "AR-9 private", "source": "leaked-source-label"}],
    }


def test_safe_job_description_contains_no_payload():
    from app.workers.queue import safe_job_description

    description = safe_job_description("phylo pipeline", _sensitive_params(), "job-1")

    assert "phylo pipeline" in description
    assert "tree=raxml" in description
    assert len(description) <= 200
    for secret in ("ACGTACGT", "AR-9", "private note", "0123456789abcdef", "leaked-source-label"):
        assert secret not in description


def test_every_enqueue_path_sets_an_explicit_description():
    """RQ prints the description at job start; unset, it renders the raw args."""
    from app.workers import queue as queue_module

    params = _sensitive_params()
    fake_job = Mock(id="11111111-1111-4111-8111-111111111111")
    fake_queue = Mock()
    fake_queue.enqueue.return_value = fake_job
    fake_queue.enqueue_call.return_value = fake_job
    fake_queue.enqueue_in.return_value = fake_job
    fake_queue.connection.lock.return_value = nullcontext()
    fake_queue.fetch_job.return_value = None

    with patch.object(queue_module, "get_queue", return_value=fake_queue):
        queue_module.enqueue_job(dict(params))
        queue_module.enqueue_mycomap_blast_refresh_job({"blast_id": "r123"})
        queue_module.enqueue_recompute_job("22222222-2222-4222-8222-222222222222", dict(params))

    from app.services import inaturalist_tree_service

    with patch("app.workers.queue.get_queue", return_value=fake_queue):
        inaturalist_tree_service._schedule_ncbi_recheck("33333333-3333-4333-8333-333333333333")

    calls = (
        fake_queue.enqueue.call_args_list
        + fake_queue.enqueue_call.call_args_list
        + fake_queue.enqueue_in.call_args_list
    )
    assert len(calls) == 4
    for call in calls:
        description = call.kwargs.get("description")
        assert description, f"enqueue without a description: {call}"
        for secret in ("ACGTACGT", "AR-9", "private note", "leaked-source-label"):
            assert secret not in description


def test_recompute_enqueue_reuses_an_active_job():
    from app.workers import queue as queue_module

    existing = Mock(id=JOB_A)
    existing.get_status.return_value = "started"
    fake_queue = Mock()
    fake_queue.connection.lock.return_value = nullcontext()
    fake_queue.fetch_job.return_value = existing

    with patch.object(queue_module, "get_queue", return_value=fake_queue):
        result = queue_module.enqueue_recompute_job(
            JOB_A, _sensitive_params(), return_created=True,
        )

    assert result == (JOB_A, False)
    fake_queue.enqueue_call.assert_not_called()


def test_enqueue_job_preserves_queue_selection_ids_and_timeouts():
    """The description change must not disturb anything else about enqueueing."""
    from app.workers import queue as queue_module

    fake_job = Mock(id="job-id")
    fake_queue = Mock()
    fake_queue.enqueue.return_value = fake_job

    with patch.object(queue_module, "get_queue", return_value=fake_queue) as get_queue:
        queue_module.enqueue_job(
            {"tree_method": "raxml"}, queue_name=queue_module.QUEUE_BULK,
            meta={"steps": {}}, job_id="abc",
        )

    get_queue.assert_called_once_with(queue_module.QUEUE_BULK)
    kwargs = fake_queue.enqueue.call_args.kwargs
    assert kwargs["job_id"] == "abc"
    assert kwargs["meta"] == {"steps": {}}
    assert kwargs["job_timeout"].endswith("s")


# ---------------------------------------------------------------------------
# Health transitions
# ---------------------------------------------------------------------------

class HealthTransitionTests(unittest.TestCase):
    def setUp(self):
        from app.monitoring import services

        self.services = services
        services._HEALTH_STATES.clear()
        services._HEALTH_LAST_EMIT.clear()
        self.capture = _Capture()
        self.logger = logging.getLogger("app.monitoring.services")
        self.logger.addHandler(self.capture)
        self.previous_level = self.logger.level
        self.logger.setLevel(logging.WARNING)

    def tearDown(self):
        self.logger.removeHandler(self.capture)
        self.logger.setLevel(self.previous_level)
        self.services._HEALTH_STATES.clear()
        self.services._HEALTH_LAST_EMIT.clear()

    def test_transition_logs_changes_once_and_repeats_only_after_cooldown(self):
        transition = self.services._transition
        cooldown = self.services.HEALTH_COOLDOWN_SECONDS

        transition("disk", True, "percent=95", now=0)
        transition("disk", True, "percent=96", now=60)          # unchanged: silent
        transition("disk", True, "percent=97", now=cooldown + 1)  # cooldown reminder
        transition("disk", False, "percent=40", now=cooldown + 2)  # recovery
        transition("disk", False, "percent=41", now=cooldown + 3)  # silent

        messages = [record.getMessage() for record in self.capture.records]
        assert sum("unhealthy" in message for message in messages) == 2
        assert sum("recovered" in message for message in messages) == 1

    def test_disk_uses_hysteresis_so_a_flapping_value_does_not_spam(self):
        with patch.object(self.services, "get_worker_status", return_value={"workers": [
            {"status": "healthy"}
        ]}), patch.object(self.services, "db", Mock()), patch.object(self.services, "Job", Mock()):
            self.services.emit_health_transitions({"disk_usage": 91, "memory_percent": 10})
            # 87 is below the 90 entry threshold but above the 85 exit threshold:
            # still unhealthy, and therefore not a new transition.
            self.services.emit_health_transitions({"disk_usage": 87, "memory_percent": 10})
            assert self.services._HEALTH_STATES["disk"] is True
            self.services.emit_health_transitions({"disk_usage": 80, "memory_percent": 10})
            assert self.services._HEALTH_STATES["disk"] is False

        disk_messages = [
            record.getMessage() for record in self.capture.records
            if "health.disk" in record.getMessage()
        ]
        assert len(disk_messages) == 2  # one unhealthy, one recovered


# ---------------------------------------------------------------------------
# Pipeline invariants
# ---------------------------------------------------------------------------

def test_pipeline_invariants_log_success_and_failure(tmp_path, clean_logging):
    from app.workers.tasks import validate_pipeline_outputs

    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "alignment").mkdir()
    (job_dir / "tree").mkdir()
    fasta = ">A\nACGTACGT\n>B\nACGTACGA\n"
    (job_dir / "input" / "input_raw.fasta").write_text(fasta)
    (job_dir / "alignment" / "alignment_raw.fasta").write_text(fasta)
    (job_dir / "alignment" / "alignment_trimmed.fasta").write_text(fasta)
    (job_dir / "tree" / "tree_original.newick").write_text("(A:0.1,B:0.1);\n")
    (job_dir / "tree" / "tree_original.nexus").write_text("#NEXUS\n")

    capture = _Capture()
    probe = logging.getLogger("app.probe.invariants")
    probe.addHandler(capture)
    probe.setLevel(logging.INFO)

    ok = validate_pipeline_outputs(job_dir, {}, logger_obj=probe)
    assert ok["ok"] is True
    assert "event=pipeline.invariants_ok" in capture.text

    (job_dir / "tree" / "tree_original.newick").write_text("")
    bad = validate_pipeline_outputs(job_dir, {}, logger_obj=probe)
    assert bad["ok"] is False
    assert "event=pipeline.invariant_failed" in capture.text
    probe.removeHandler(capture)


# ---------------------------------------------------------------------------
# Log digest: error grouping
# ---------------------------------------------------------------------------

def test_digest_reads_rotations_deduplicates_mirrors_and_groups_final_cause(tmp_path):
    digest = _digest_module()
    digest.LOG_DIR = tmp_path
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record = (
        f"[{timestamp}] [ERROR] [gunicorn.error] Error handling request "
        "[req=abc user=user@example.com job=123]\n"
        "Traceback (most recent call last):\n"
        "  File \"worker.py\", line 10, in run\n"
        "ValueError: final meaningful cause 123\n"
    )
    (tmp_path / "error.log-20260814.gz").write_bytes(gzip.compress(record.encode()))
    (tmp_path / "errors.log").write_text(record)

    result = digest.analyze_errors(datetime.now() - timedelta(hours=1))

    assert sum(result["exceptions"].values()) == 1
    key = next(iter(result["exceptions"]))
    assert key == "ValueError: final meaningful cause <n>"
    assert sorted(result["coverage"]["files"]) == ["error.log-20260814.gz", "errors.log"]


def test_digest_separates_5xx_noise_and_streams_and_normalizes_thumb(tmp_path):
    digest = _digest_module()
    digest.LOG_DIR = tmp_path
    timestamp = datetime.now().strftime("%d/%b/%Y:%H:%M:%S +0000")
    lines = [
        f'1.1.1.1 - - [{timestamp}] "GET /api/job/abc HTTP/1.0" 500 10 "-" "browser" 100000 req=r1\n',
        f'2.2.2.2 - - [{timestamp}] "GET /thumb/123456.jpg HTTP/1.0" 404 10 "-" "scannerbot" 1000 req=r2\n',
        f'3.3.3.3 - - [{timestamp}] "GET /api/job/123/events HTTP/1.0" 200 10 "-" "browser" 900000000 req=r3\n',
        f'3.3.3.3 - - [{timestamp}] "POST /api/job HTTP/1.0" 422 10 "-" "browser" 2000 req=r4\n',
    ]
    (tmp_path / "access.log-20260814").write_text("".join(lines))

    result = digest.analyze_access(datetime.now() - timedelta(hours=1))

    assert result["server_errors"][(500, "GET /api/job/abc")] == 1
    assert result["product_4xx"][(422, "POST /api/job")] == 1
    assert result["noise_4xx"][(404, "GET /thumb/<id>.jpg")] == 1
    assert "GET /api/job/<id>/events" in result["streams"]
    assert "GET /api/job/<id>/events" not in result["durations"]


def test_digest_classifies_query_suffixed_and_credential_probing_paths_as_noise(tmp_path):
    digest = _digest_module()
    digest.LOG_DIR = tmp_path
    timestamp = datetime.now().strftime("%d/%b/%Y:%H:%M:%S +0000")
    probes = [
        "/index.php?s=/Index/think/app/invokefunction",
        "/.env?full=1",
        "/wp-login.php?redirect_to=/wp-admin",
        "/.aws/credentials",
        "/actuator/env",
        "/manager/html",
    ]
    lines = [
        f'9.9.9.9 - - [{timestamp}] "GET {path} HTTP/1.0" 404 10 "-" "python-requests/2" 900 req=r\n'
        for path in probes
    ]
    # A real product 404 must stay in the product bucket.
    lines.append(
        f'8.8.8.8 - - [{timestamp}] "GET /api/job/missing HTTP/1.0" 404 10 "-" "Mozilla/5.0" 900 req=r\n'
    )
    (tmp_path / "access.log").write_text("".join(lines))

    result = digest.analyze_access(datetime.now() - timedelta(hours=1))

    assert sum(result["noise_4xx"].values()) == len(probes)
    assert sum(result["product_4xx"].values()) == 1
    # High-volume noise must never crowd out the separate 5xx section.
    assert result["server_errors"] == {}


def test_digest_window_selects_only_overlapping_rotations(tmp_path):
    digest = _digest_module()
    digest.LOG_DIR = tmp_path
    now = datetime.now()
    cutoff = now - timedelta(hours=24)

    def write(name, age_hours, record_age_hours):
        path = tmp_path / name
        stamp = (now - timedelta(hours=record_age_hours)).strftime("%d/%b/%Y:%H:%M:%S +0000")
        path.write_text(
            f'1.1.1.1 - - [{stamp}] "GET /api/job/x HTTP/1.0" 500 10 "-" "b" 100 req=r\n'
        )
        when = (now - timedelta(hours=age_hours)).timestamp()
        os.utime(path, (when, when))
        return path

    write("access.log-20260701", age_hours=800, record_age_hours=800)   # ancient
    write("access.log-20260801", age_hours=300, record_age_hours=300)   # old
    write("access.log-20260814", age_hours=30, record_age_hours=30)     # boundary
    write("access.log-20260815", age_hours=5, record_age_hours=5)       # in window
    write("access.log", age_hours=0, record_age_hours=1)                # live

    result = digest.analyze_access(cutoff)
    inspected = result["coverage"]["files"]

    assert "access.log" in inspected
    assert "access.log-20260815" in inspected
    # The one rotation immediately before the cutoff is kept to cover the boundary.
    assert "access.log-20260814" in inspected
    # Everything older is never opened at all.
    assert "access.log-20260801" not in inspected
    assert "access.log-20260701" not in inspected

    # Coverage separates what was scanned from what was inside the window.
    assert result["coverage"]["lines"] == 3
    assert result["coverage"]["observed"] == 2
    assert result["total"] == 2


# ---------------------------------------------------------------------------
# Log digest: worker lifecycle
# ---------------------------------------------------------------------------

JOB_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
JOB_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
JOB_C = "cccccccc-3333-4333-8333-cccccccccccc"
JOB_D = "dddddddd-4444-4444-8444-dddddddddddd"
JOB_E = "eeeeeeee-5555-4555-8555-eeeeeeeeeeee"


def _worker_log(tmp_path, lines):
    (tmp_path / "worker.log").write_text("".join(lines))


def _stamp(minutes_ago):
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


def test_worker_lifecycle_matches_rq_starts_completions_failures_and_retries(tmp_path):
    digest = _digest_module()
    digest.LOG_DIR = tmp_path
    _worker_log(tmp_path, [
        # A: an entirely ordinary RQ job -- start, then Job OK.
        f"[{_stamp(90)}] [INFO] [rq.worker] phylo_high: phylo pipeline job={JOB_A} sequences=12 ({JOB_A}) [release=git:abc]\n",
        # The app emits its own start for that same attempt; it is not a retry.
        f"[{_stamp(90)}] [INFO] [app.workers.tasks] event=job.started Starting job summary={{}} [job={JOB_A} rq={JOB_A} release=git:abc]\n",
        f"[{_stamp(88)}] [INFO] [rq.worker] phylo_high: Job OK ({JOB_A}) [release=git:abc]\n",
        # Likewise, a second terminal vocabulary must not double-count A.
        f"[{_stamp(88)}] [INFO] [app.workers.tasks] event=job.completed Job completed successfully [job={JOB_A} rq={JOB_A} release=git:abc]\n",
        # B: Dikarya's own stable events.
        f"[{_stamp(80)}] [INFO] [app.workers.tasks] event=job.started Starting job summary={{}} [job={JOB_B} rq={JOB_B} release=git:abc]\n",
        f"[{_stamp(75)}] [INFO] [app.workers.tasks] event=job.completed Job completed successfully [job={JOB_B} rq={JOB_B} release=git:abc]\n",
        # C: a genuine failure.
        f"[{_stamp(70)}] [INFO] [rq.worker] phylo_bulk: phylo pipeline job={JOB_C} ({JOB_C})\n",
        f"[{_stamp(69)}] [ERROR] [rq.worker] Worker w1: moving job {JOB_C} to FailedJobRegistry (boom)\n",
        # D: retried, then completed. A retry is not an outcome.
        f"[{_stamp(65)}] [INFO] [rq.worker] phylo_high: phylo pipeline job={JOB_D} ({JOB_D})\n",
        f"[{_stamp(64)}] [INFO] [rq.worker] Worker w1: job {JOB_D} scheduled for retry\n",
        f"[{_stamp(60)}] [INFO] [rq.worker] Successfully completed job {JOB_D} in 4.2s on worker w1\n",
    ])

    counts, stale, coverage = digest.analyze_worker(
        datetime.now() - timedelta(hours=6), grace=timedelta(minutes=30)
    )

    assert counts["started"] == 4
    assert counts["completed"] == 3
    assert counts["failed"] == 1
    assert counts["retried"] == 1
    assert stale == [], "an ordinary start followed by Job OK must not be reported"
    assert coverage["files"] == ["worker.log"]


def test_worker_lifecycle_counts_repeated_stable_start_as_retry(tmp_path):
    digest = _digest_module()
    digest.LOG_DIR = tmp_path
    _worker_log(tmp_path, [
        f"[{_stamp(20)}] [INFO] [app.workers.tasks] event=job.started first [job={JOB_A}]\n",
        f"[{_stamp(18)}] [INFO] [app.workers.tasks] event=job.started resumed [job={JOB_A}]\n",
        f"[{_stamp(17)}] [INFO] [app.workers.tasks] event=job.completed done [job={JOB_A}]\n",
    ])

    counts, stale, _ = digest.analyze_worker(datetime.now() - timedelta(hours=1))

    assert counts["started"] == 1
    assert counts["retried"] == 1
    assert counts["completed"] == 1
    assert stale == []


def test_worker_lifecycle_reused_job_id_starts_a_new_run(tmp_path):
    digest = _digest_module()
    digest.LOG_DIR = tmp_path
    _worker_log(tmp_path, [
        f"[{_stamp(30)}] [INFO] [app.workers.tasks] event=job.started original [job={JOB_A}]\n",
        f"[{_stamp(25)}] [INFO] [app.workers.tasks] event=job.completed original [job={JOB_A}]\n",
        f"[{_stamp(20)}] [INFO] [app.workers.tasks] event=job.started recompute [job={JOB_A}]\n",
        f"[{_stamp(15)}] [INFO] [app.workers.tasks] event=job.completed recompute [job={JOB_A}]\n",
    ])

    counts, stale, _ = digest.analyze_worker(datetime.now() - timedelta(hours=1))

    assert counts["started"] == 2
    assert counts["completed"] == 2
    assert counts["retried"] == 0
    assert stale == []


def test_worker_lifecycle_separates_active_jobs_from_genuinely_stale_ones(tmp_path):
    digest = _digest_module()
    digest.LOG_DIR = tmp_path
    _worker_log(tmp_path, [
        # Started five minutes ago: a long RAxML run, not an orphan.
        f"[{_stamp(5)}] [INFO] [rq.worker] phylo_high: phylo pipeline job={JOB_A} ({JOB_A})\n",
        # Started nine hours ago and never heard from again.
        f"[{_stamp(540)}] [INFO] [rq.worker] phylo_high: phylo pipeline job={JOB_E} ({JOB_E})\n",
    ])

    counts, stale, _ = digest.analyze_worker(
        datetime.now() - timedelta(hours=24), grace=timedelta(minutes=60)
    )

    assert counts["started"] == 2
    assert counts["active"] == 1
    assert [job for job, _ in stale] == [JOB_E]
    age = stale[0][1]
    assert digest.format_age(datetime.now() - age).endswith("m")


def test_worker_lifecycle_ignores_stale_starts_outside_the_window(tmp_path):
    digest = _digest_module()
    digest.LOG_DIR = tmp_path
    _worker_log(tmp_path, [
        f"[{_stamp(60 * 40)}] [INFO] [rq.worker] phylo_high: phylo pipeline ({JOB_A})\n",
    ])

    counts, stale, coverage = digest.analyze_worker(datetime.now() - timedelta(hours=24))

    assert counts["started"] == 0
    assert stale == []
    assert coverage["observed"] == 0
    assert coverage["lines"] == 1
