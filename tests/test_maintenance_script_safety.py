"""Failure-path coverage for the scripts that mutate production job data.

These run as cron jobs against var/jobs and the live database with nobody
watching, so the cases that matter are the ones where something goes wrong
partway: a compression that dies on a full disk, a staging directory that will
not delete, a backfill row that carries no observation id. Every test here works
in a tmp_path or against a stub, never against real job data.
"""

import gzip
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name):
    """Import a scripts/*.py module under its own name, without installing it."""
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def reclaim():
    return _load_script("dikarya_reclaim_job_space")


@pytest.fixture(scope="module")
def digest():
    return _load_script("dikarya_log_digest")


# --------------------------------------------------------------------------
# dikarya_reclaim_job_space.py
# --------------------------------------------------------------------------

def test_failed_compression_leaves_no_partial_archive(reclaim, tmp_path, monkeypatch):
    """A gzip that dies partway must not leave a .gz.tmp nothing will ever reclaim.

    No pass in the script globs *.gz.tmp, so a residue here is permanent and
    grows with every run -- consuming the space the script exists to free.
    """
    source = tmp_path / "alignment_raw.fasta"
    source.write_bytes(b"ACGT" * 100_000)

    def explode(src, dst, length=None):
        dst.write(b"partial")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(reclaim.shutil, "copyfileobj", explode)

    with pytest.raises(OSError):
        reclaim.gzip_in_place(source, apply=True)

    assert not (tmp_path / "alignment_raw.fasta.gz.tmp").exists()
    # The original is untouched, so the artifact is still readable.
    assert source.read_bytes() == b"ACGT" * 100_000


def test_successful_compression_replaces_the_original(reclaim, tmp_path):
    source = tmp_path / "alignment_raw.fasta"
    payload = b">seq\n" + b"ACGT" * 100_000
    source.write_bytes(payload)

    saved = reclaim.gzip_in_place(source, apply=True)

    assert saved > 0
    assert not source.exists()
    with gzip.open(tmp_path / "alignment_raw.fasta.gz", "rb") as handle:
        assert handle.read() == payload


def test_interrupted_final_unlink_leaves_a_resolver_safe_pair(
    reclaim, tmp_path, monkeypatch
):
    from app.services.artifact_storage import resolve_artifact

    source = tmp_path / "alignment_raw.fasta"
    payload = b">seq\n" + b"ACGT" * 100_000
    source.write_bytes(payload)
    original_unlink = Path.unlink

    def fail_source_unlink(self, *args, **kwargs):
        if self == source:
            raise OSError("simulated interruption after archive rename")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_source_unlink)

    with pytest.raises(OSError, match="simulated interruption"):
        reclaim.gzip_in_place(source, apply=True)

    assert source.exists()
    assert source.with_name(source.name + ".gz").exists()
    assert resolve_artifact(source) == source, "the readable plain artifact must win"


def test_undeletable_staging_is_counted_as_an_error_not_as_reclaimed(
    reclaim, tmp_path, monkeypatch
):
    """rmtree(ignore_errors=True) hides failures; the byte tally must not.

    Tallying the pre-delete size regardless meant a run that removed nothing
    still reported the full amount as reclaimed, and exited 0.
    """
    job_dir = tmp_path / "job"
    staging = job_dir / ".recompute-abcd"
    staging.mkdir(parents=True)
    (staging / "alignment.fasta").write_bytes(b"x" * 5000)

    monkeypatch.setattr(reclaim.shutil, "rmtree", lambda *args, **kwargs: None)

    tally = reclaim.Tally()
    reclaim.pass_scratch(job_dir, apply=True, tally=tally)

    assert tally.errors == 1
    assert tally.by_pass["scratch"][1] == 0, "bytes that are still on disk were counted"


def test_removable_staging_is_counted_and_clears_no_error(reclaim, tmp_path):
    job_dir = tmp_path / "job"
    staging = job_dir / ".recompute-abcd"
    staging.mkdir(parents=True)
    (staging / "alignment.fasta").write_bytes(b"x" * 5000)

    tally = reclaim.Tally()
    reclaim.pass_scratch(job_dir, apply=True, tally=tally)

    assert tally.errors == 0
    assert tally.by_pass["scratch"][1] == 5000
    assert not staging.exists()


def test_partially_removed_staging_counts_only_bytes_actually_removed(
    reclaim, tmp_path, monkeypatch
):
    job_dir = tmp_path / "job"
    staging = job_dir / ".recompute-abcd"
    staging.mkdir(parents=True)
    removed = staging / "removed.fasta"
    surviving = staging / "surviving.fasta"
    removed.write_bytes(b"x" * 3000)
    surviving.write_bytes(b"y" * 2000)

    monkeypatch.setattr(
        reclaim.shutil, "rmtree", lambda *args, **kwargs: removed.unlink()
    )

    tally = reclaim.Tally()
    reclaim.pass_scratch(job_dir, apply=True, tally=tally)

    assert tally.errors == 1
    assert tally.by_pass["scratch"][1] == 3000
    assert surviving.exists()


def test_log_filtering_streams_and_matches_the_buffered_result(reclaim, tmp_path):
    """The streaming filter must produce exactly the bytes the old version did."""
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "alignment.log"
    lines = []
    for index in range(20_000):
        lines.append(f"STEP {index} progress nnnnnnnn")
        lines.append(f"done {index}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    original = path.stat().st_size

    kept = [line for line in path.read_text().splitlines() if reclaim._keep_mafft_log_line(line)]
    expected = ("\n".join(kept) + "\n").encode("utf-8") if kept else b""

    tally = reclaim.Tally()
    reclaim.pass_logs(tmp_path, apply=True, tally=tally)

    assert path.read_bytes() == expected
    assert tally.by_pass["logs"][1] == original - len(expected)


def test_streaming_log_filter_matches_splitlines_for_crlf_input(
    reclaim, tmp_path
):
    path = tmp_path / "alignment.log"
    lines = ["STEP progress nnnnnnnn", "done"] * 400
    raw = ("\r\n".join(lines) + "\r\n").encode("utf-8")
    path.write_bytes(raw)

    text = raw.decode("utf-8")
    kept = [line for line in text.splitlines() if reclaim._keep_mafft_log_line(line)]
    expected = ("\n".join(kept) + "\n").encode("utf-8") if kept else b""

    assert b"".join(reclaim._kept_log_chunks(path)) == expected


def test_replace_atomically_removes_temp_file_when_streaming_raises(
    reclaim, tmp_path
):
    path = tmp_path / "tree_state.json"
    original = b'{"old":true}\n'
    path.write_bytes(original)

    def broken_chunks():
        yield b'{"new":'
        raise OSError("simulated read failure")

    with pytest.raises(OSError, match="simulated read failure"):
        reclaim.replace_atomically(path, broken_chunks())

    assert path.read_bytes() == original
    assert list(tmp_path.glob(".reclaim.*.tmp")) == []


def test_reclaim_dry_run_does_not_mutate_artifacts(reclaim, tmp_path):
    job_dir = tmp_path / "job"
    alignment = job_dir / "alignment" / "alignment_raw.fasta"
    alignment.parent.mkdir(parents=True)
    payload = b">seq\n" + b"ACGT" * 2000
    alignment.write_bytes(payload)

    tally = reclaim.Tally()
    reclaim.pass_alignments(job_dir, apply=False, tally=tally)

    assert alignment.read_bytes() == payload
    assert not alignment.with_name(alignment.name + ".gz").exists()
    assert not alignment.with_name(alignment.name + ".gz.tmp").exists()


def test_main_exits_non_zero_when_a_pass_failed(reclaim, tmp_path, monkeypatch, capsys):
    """Cron cannot see a partial run that exits 0."""
    jobs_root = tmp_path / "jobs"
    job_dir = jobs_root / "0123abcd-0123-0123-0123-0123456789ab"
    (job_dir / "logs").mkdir(parents=True)
    # Backdate everything so --min-age-hours does not skip the job.
    old = 0
    for path in (job_dir / "logs", job_dir, jobs_root):
        os.utime(path, (old, old))

    def failing_pass(job_dir, apply, tally):
        raise OSError("simulated failure")

    monkeypatch.setitem(reclaim.PASS_FUNCS, "scratch", failing_pass)
    monkeypatch.setattr(
        sys, "argv",
        ["dikarya_reclaim_job_space.py", "--jobs-dir", str(jobs_root), "--passes", "scratch"],
    )

    assert reclaim.main() == 1
    assert "errors: 1" in capsys.readouterr().out


def test_main_exits_zero_on_a_clean_run(reclaim, tmp_path, monkeypatch):
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    monkeypatch.setattr(
        sys, "argv", ["dikarya_reclaim_job_space.py", "--jobs-dir", str(jobs_root)]
    )

    assert reclaim.main() == 0


# --------------------------------------------------------------------------
# dikarya_log_digest.py
# --------------------------------------------------------------------------

def test_access_timestamps_are_normalized_to_utc(digest):
    """Gunicorn's %(t)s offset must be applied, not discarded.

    The digest's window boundaries are naive UTC. Dropping the offset was
    correct only while the host stayed on UTC; anywhere else it slid the whole
    window by the offset.
    """
    utc = digest.parse_access_ts("[24/Aug/2026:08:01:02 +0000]")
    plus_two = digest.parse_access_ts("[24/Aug/2026:10:01:02 +0200]")
    minus_seven = digest.parse_access_ts("[24/Aug/2026:01:01:02 -0700]")

    assert utc == datetime(2026, 8, 24, 8, 1, 2)
    assert plus_two == utc, "a +0200 record is the same instant as the +0000 one"
    assert minus_seven == utc
    # Still naive, so it compares against the module's naive-UTC cutoffs.
    assert utc.tzinfo is None


def test_access_timestamp_without_an_offset_is_still_parsed(digest):
    assert digest.parse_access_ts("24/Aug/2026:08:01:02") == datetime(2026, 8, 24, 8, 1, 2)


def test_unparseable_access_timestamps_return_none(digest):
    assert digest.parse_access_ts("") is None
    assert digest.parse_access_ts("not a timestamp") is None


def _write_log(path, records):
    path.write_text("".join(records), encoding="utf-8")


WINDOW = (datetime(2026, 8, 24), datetime(2026, 8, 25))

# One second, one message, no request id: the hardest case for incident
# identity, because timestamp, normalized key and raw text are all identical.
REDIS_DOWN = "[2026-08-24 08:01:02] [ERROR] [app.services.queue] Redis unavailable\n"


def _count(digest, **_unused):
    result = digest.analyze_errors(WINDOW[0], until=WINDOW[1])
    return sum(result["exceptions"].values())


def test_differently_worded_failures_in_the_same_second_count_separately(
    digest, tmp_path, monkeypatch
):
    """The original bug: (second, normalized key) collapsed a whole burst.

    meaningful_error_key() rewrites every integer to <n>, so these three
    genuinely different messages also normalize to one key.
    """
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    stamp = "2026-08-24 08:01:02"
    _write_log(tmp_path / "errors.log", [
        f"[{stamp}] [ERROR] [app.api.routes] Tree build failed for job alpha\n",
        f"[{stamp}] [ERROR] [app.api.routes] Tree build failed for job beta\n",
        f"[{stamp}] [ERROR] [app.api.routes] Tree build failed for job gamma\n",
    ])

    assert _count(digest) == 3


def test_byte_identical_failures_in_the_same_second_count_separately(
    digest, tmp_path, monkeypatch
):
    """Three identical no-request-id failures in one second are three incidents.

    This is the case that survived the first fix. Adding the raw record text to
    the key separated differently-worded messages but not identical ones: three
    copies of the same line in the same second still produced one key. A worker
    that loses Redis logs exactly this shape, and the digest is a volume signal.
    """
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    _write_log(tmp_path / "errors.log", [REDIS_DOWN, REDIS_DOWN, REDIS_DOWN])

    assert _count(digest) == 3


def test_a_byte_identical_burst_mirrored_into_both_logs_is_not_doubled(
    digest, tmp_path, monkeypatch
):
    """Same burst, written to both streams: still three, not six.

    errors.log is a level-filtered mirror of error.log, so every one of these
    records exists in both files. The Nth occurrence in one stream must land on
    the same incident as the Nth in the other.
    """
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    _write_log(tmp_path / "errors.log", [REDIS_DOWN, REDIS_DOWN, REDIS_DOWN])
    _write_log(tmp_path / "error.log", [REDIS_DOWN, REDIS_DOWN, REDIS_DOWN])

    assert _count(digest) == 3


def test_a_mirror_carrying_fewer_copies_does_not_inflate_the_count(
    digest, tmp_path, monkeypatch
):
    """The larger stream sets the count; the mirror cannot add to it."""
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    # error.log is the superset (it also carries INFO, which is filtered out
    # here by LEVEL_RE), so it may legitimately hold copies errors.log lacks.
    _write_log(tmp_path / "error.log", [
        REDIS_DOWN,
        "[2026-08-24 08:01:02] [INFO] [app.services.queue] Reconnecting\n",
        REDIS_DOWN,
    ])
    _write_log(tmp_path / "errors.log", [REDIS_DOWN])

    assert _count(digest) == 2


def test_a_mirrored_record_is_still_counted_once(digest, tmp_path, monkeypatch):
    """error.log mirrors errors.log; the same record in both is one incident."""
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    record = "[2026-08-24 08:01:02] [ERROR] [app.api.routes] Tree build failed for job alpha\n"
    _write_log(tmp_path / "errors.log", [record])
    _write_log(tmp_path / "error.log", [record])

    assert _count(digest) == 1


# ---------------------------------------------------------------------------
# The two streams are NOT byte-identical.
#
# error.log is written by the handler Gunicorn installed, re-wrapped by
# ContextFormatter in app/__init__.py: Gunicorn's datefmt carries the UTC offset
# and no logger name. errors.log is written by install_error_mirror()'s own
# handler in app/services/log_context.py, whose format is
# "[%(asctime)s] [%(process)d] [%(levelname)s] [%(name)s] %(message)s" -- default
# asctime, so milliseconds, plus the logger name. One incident, two spellings.
# ---------------------------------------------------------------------------

def _gunicorn_style(message, context="", pid=12345):
    """A record as error.log spells it."""
    suffix = f" [{context}]" if context else ""
    return f"[2026-08-24 08:01:02 +0000] [{pid}] [ERROR] {message}{suffix}\n"


def _mirror_style(message, context="", pid=12345, logger="app.services.queue"):
    """The same record as errors.log spells it."""
    suffix = f" [{context}]" if context else ""
    return f"[2026-08-24 08:01:02,417] [{pid}] [ERROR] [{logger}] {message}{suffix}\n"


def test_the_two_stream_formats_really_do_differ(digest):
    """Guard the premise: if these ever match byte-for-byte the tests below are vacuous."""
    assert _gunicorn_style("Redis unavailable") != _mirror_style("Redis unavailable")
    # ...but they must resolve to the same incident identity.
    assert (digest.record_identity(_gunicorn_style("Redis unavailable"))
            == digest.record_identity(_mirror_style("Redis unavailable")))
    # And a different message must not.
    assert (digest.record_identity(_mirror_style("Redis unavailable"))
            != digest.record_identity(_mirror_style("Postgres unavailable")))


def test_one_failure_in_both_stream_formats_is_one_incident(
    digest, tmp_path, monkeypatch
):
    """The realistic mirror case: same incident, different formatted text."""
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    _write_log(tmp_path / "error.log", [_gunicorn_style("Redis unavailable")])
    _write_log(tmp_path / "errors.log", [_mirror_style("Redis unavailable")])

    assert _count(digest) == 1


@pytest.mark.parametrize("copies", [1, 2, 3])
def test_bracket_prefixed_mirror_bursts_count_each_incident_once(
    digest, tmp_path, monkeypatch, copies
):
    """Application tags are message data, not the mirror formatter's logger."""
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    message = "[ALIGN] Redis unavailable"
    _write_log(tmp_path / "error.log", [_gunicorn_style(message)] * copies)
    _write_log(tmp_path / "errors.log", [_mirror_style(message)] * copies)

    assert _count(digest) == copies


@pytest.mark.parametrize("message", ["[TREE] failure", "[worker-1] failure"])
def test_other_bracket_prefixed_messages_deduplicate_without_losing_the_tag(
    digest, tmp_path, monkeypatch, message
):
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    gunicorn = _gunicorn_style(message)
    mirror = _mirror_style(message)
    _write_log(tmp_path / "error.log", [gunicorn])
    _write_log(tmp_path / "errors.log", [mirror])

    assert digest.record_identity(gunicorn) == digest.record_identity(mirror)
    assert digest.record_identity(gunicorn)[0].startswith(message.split()[0])
    assert _count(digest) == 1


def test_two_separate_same_second_failures_are_not_collapsed(
    digest, tmp_path, monkeypatch
):
    """Two genuinely distinct failures in one second stay two, in one stream."""
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    _write_log(tmp_path / "errors.log", [
        _mirror_style("Redis unavailable"),
        _mirror_style("Redis unavailable"),
    ])

    assert _count(digest) == 2


def test_a_same_second_burst_in_both_stream_formats_counts_once_each(
    digest, tmp_path, monkeypatch
):
    """Three failures, mirrored in the two real formats: three, not six, not one."""
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    _write_log(tmp_path / "error.log", [_gunicorn_style("Redis unavailable")] * 3)
    _write_log(tmp_path / "errors.log", [_mirror_style("Redis unavailable")] * 3)

    assert _count(digest) == 3


def test_request_id_records_still_use_request_identity_across_the_formats(
    digest, tmp_path, monkeypatch
):
    """A request id identifies the incident regardless of which stream spelt it."""
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    context = "req=2abb1286 user=someone@example.com job=5db685aa-1111-2222-3333-444455556666"
    _write_log(tmp_path / "error.log", [
        _gunicorn_style("Tree build failed", context),
        _gunicorn_style("Tree build failed", context),
    ])
    _write_log(tmp_path / "errors.log", [
        _mirror_style("Tree build failed", context, logger="app.api.routes"),
    ])

    assert _count(digest) == 1


def test_two_different_requests_in_the_same_second_stay_separate_across_formats(
    digest, tmp_path, monkeypatch
):
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    _write_log(tmp_path / "error.log", [
        _gunicorn_style("Tree build failed", "req=aaaa1111 user=a@example.com"),
        _gunicorn_style("Tree build failed", "req=bbbb2222 user=b@example.com"),
    ])
    _write_log(tmp_path / "errors.log", [
        _mirror_style("Tree build failed", "req=aaaa1111 user=a@example.com",
                      logger="app.api.routes"),
        _mirror_style("Tree build failed", "req=bbbb2222 user=b@example.com",
                      logger="app.api.routes"),
    ])

    assert _count(digest) == 2


def test_multiline_tracebacks_still_deduplicate_across_the_two_formats(
    digest, tmp_path, monkeypatch
):
    """The traceback body is byte-identical in both streams; only the head differs."""
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    traceback = (
        "Traceback (most recent call last):\n"
        '  File "/var/www/dikarya/app/services/queue.py", line 42, in enqueue\n'
        "    conn.ping()\n"
        "ConnectionError: Error 111 connecting to localhost:6379\n"
    )
    _write_log(tmp_path / "error.log", [_gunicorn_style("Enqueue failed") + traceback])
    _write_log(tmp_path / "errors.log", [_mirror_style("Enqueue failed") + traceback])

    result = digest.analyze_errors(WINDOW[0], until=WINDOW[1])

    assert sum(result["exceptions"].values()) == 1
    assert "ConnectionError" in next(iter(result["exceptions"]))


def test_two_different_tracebacks_in_the_same_second_stay_separate(
    digest, tmp_path, monkeypatch
):
    """Same first line, different cause: two incidents, mirrored into both formats."""
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    head = "Traceback (most recent call last):\n"
    one = head + "ConnectionError: Error 111 connecting to localhost:6379\n"
    two = head + "TimeoutError: read timed out\n"
    _write_log(tmp_path / "error.log", [
        _gunicorn_style("Enqueue failed") + one,
        _gunicorn_style("Enqueue failed") + two,
    ])
    _write_log(tmp_path / "errors.log", [
        _mirror_style("Enqueue failed") + one,
        _mirror_style("Enqueue failed") + two,
    ])

    result = digest.analyze_errors(WINDOW[0], until=WINDOW[1])

    assert sum(result["exceptions"].values()) == 2


def test_identical_records_across_a_rotation_boundary_count_separately(
    digest, tmp_path, monkeypatch
):
    """A rotation is not a mirror.

    The occurrence counter runs across a stream's rotations rather than
    resetting per file, so the same line either side of a rotation boundary is
    two occurrences -- while the same line in the *other* stream is still one.
    """
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    _write_log(tmp_path / "errors.log.1", [REDIS_DOWN])
    _write_log(tmp_path / "errors.log", [REDIS_DOWN])
    # Make the rotation look older than the live file so log_files() orders them.
    os.utime(tmp_path / "errors.log.1", (1_000_000, 1_000_000))

    assert _count(digest) == 2


def test_repeats_inside_one_request_remain_a_single_incident(
    digest, tmp_path, monkeypatch
):
    """Request-id deduplication must survive the occurrence-index change."""
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    record = (
        "[2026-08-24 08:01:02] [ERROR] [app.api.routes] Tree build failed "
        "[req=2abb1286 user=someone@example.com job=5db685aa-1111-2222-3333-444455556666]\n"
    )
    _write_log(tmp_path / "errors.log", [record, record, record])
    _write_log(tmp_path / "error.log", [record])

    assert _count(digest) == 1


def test_the_same_message_in_two_different_requests_counts_twice(
    digest, tmp_path, monkeypatch
):
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    template = (
        "[2026-08-24 08:01:02] [ERROR] [app.api.routes] Tree build failed "
        "[req={req} user=someone@example.com]\n"
    )
    _write_log(tmp_path / "errors.log", [
        template.format(req="2abb1286"),
        template.format(req="9ffe4410"),
    ])

    assert _count(digest) == 2


def test_a_job_started_long_ago_is_stale_even_if_the_log_stopped(digest, tmp_path, monkeypatch):
    """The age of an unterminated job is measured against the window end.

    Measuring it against the newest line in the log meant a worker that died
    right after logging job.started pinned its own reference and reported the
    stranded job "active" in every later digest -- the exact case the section
    exists to surface.
    """
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    job = "5db685aa-1111-2222-3333-444455556666"
    _write_log(tmp_path / "worker.log", [
        f"[2026-08-24 00:05:00] [INFO] [app.workers.tasks] event=job.started "
        f"[req=2abb1286 user=someone@example.com job={job}]\n",
    ])

    counts, stale, _coverage = digest.analyze_worker(
        datetime(2026, 8, 24), until=datetime(2026, 8, 25)
    )

    assert counts["active"] == 0
    assert [item[0] for item in stale] == [job]


# --------------------------------------------------------------------------
# backfill_inaturalist_job_titles.py
# --------------------------------------------------------------------------

def _backfill_module():
    """Import the backfill script without letting it re-exec or touch the DB."""
    path = REPO_ROOT / "scripts" / "backfill_inaturalist_job_titles.py"
    spec = importlib.util.spec_from_file_location("backfill_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def backfill():
    return _backfill_module()


@pytest.mark.parametrize(
    "metrics, expected",
    [
        ({"inat_observation_id": 12345}, 12345),
        ({"inat_observation_id": "12345"}, 12345),
        ({"inat_observation_id": 0}, 0),
        ({"inat_observation_id": -5}, 0),
        ({"inat_observation_id": "not-a-number"}, 0),
        ({"inat_observation_id": float("inf")}, 0),
        ({"inat_observation_id": float("nan")}, 0),
        ({"inat_observation_id": 123.5}, 0),
        ({"inat_observation_id": True}, 0),
        ({"inat_observation_id": None}, 0),
        ({}, 0),
        ({"notes": "iNaturalist obs 987 → Phylogenetic Tree"}, 987),
        ({"via": "inat_phylogenetic_tree"}, 0),
    ],
)
def test_unusable_observation_ids_all_resolve_to_zero(backfill, metrics, expected):
    """One skip condition, not a skip for absent and a crash for malformed.

    metrics is a free-form JSON column. A job whose id was 0 or unparseable
    never entered `genera`, and the apply loops then raised KeyError on it --
    the second one after db.session.commit() had already renamed the database
    rows, leaving their input_info.json files behind.
    """
    assert backfill._observation_id(SimpleNamespace(metrics=metrics)) == expected


def test_preview_limit_is_a_real_constant(backfill):
    """The dry-run preview samples the resolved set instead of fixed ids."""
    assert isinstance(backfill.PREVIEW_LIMIT, int)
    assert backfill.PREVIEW_LIMIT > 0
    source = (REPO_ROOT / "scripts" / "backfill_inaturalist_job_titles.py").read_text()
    assert "110793649" not in source, "the hardcoded preview ids are back"


# --- backfill orchestration -------------------------------------------------
#
# main() is the thing with the interesting contract: which jobs form the working
# population, whether a dry run really touches nothing, and what the exit code
# says when the two stores end up disagreeing. It is exercised here against fake
# jobs, a patched observation fetch and a tmp_path job root -- never the
# production database and never the network.

class _FakeJob:
    """The parts of app.models.Job the backfill actually reads and writes."""

    def __init__(self, job_id, metrics, job_dir):
        self.id = job_id
        self.metrics = metrics
        self.job_dir = str(job_dir)


def _observation(observation_id, genus="Amanita", species="muscaria"):
    """An iNaturalist observation in the shape _extract_inat_genus() reads."""
    return {
        "id": observation_id,
        "taxon": {"name": f"{genus} {species}", "rank": "species"},
    }


def _make_job(tmp_path, job_id, metrics, *, with_input_info=True):
    job_dir = tmp_path / "jobs" / job_id
    job_dir.mkdir(parents=True)
    if with_input_info:
        (job_dir / "input_info.json").write_text(
            json.dumps({"notes": "old title", "sequence": ">A\nACGT\n"}, indent=2),
            encoding="utf-8",
        )
    return _FakeJob(job_id, metrics, job_dir)


@pytest.fixture
def run_backfill(backfill, tmp_path, monkeypatch):
    """Run backfill.main() over a fake job population, capturing its summary."""
    from flask import Flask

    from app.config import Config
    from app.extensions import db

    def _run(jobs, observations, argv, commit=None):
        app = Flask(__name__)
        commits = []

        monkeypatch.setattr(backfill, "_build_app", lambda: app)
        monkeypatch.setattr(backfill, "_matching_jobs", lambda: list(jobs))
        monkeypatch.setattr(
            backfill, "_fetch_observations",
            lambda observation_ids: {
                key: value for key, value in observations.items() if key in set(observation_ids)
            },
        )
        monkeypatch.setattr(Config, "JOB_DIR", tmp_path / "jobs")
        def _commit():
            commits.append(True)
            if commit is not None:
                commit()

        monkeypatch.setattr(
            db, "session", SimpleNamespace(commit=_commit, rollback=lambda: None)
        )

        printed = []
        monkeypatch.setattr(
            "builtins.print", lambda *args, **kwargs: printed.append(" ".join(map(str, args)))
        )
        try:
            code = backfill.main(argv)
        finally:
            # Restore print before anything else runs, so a failure inside
            # main() reports normally instead of swallowing pytest's output.
            monkeypatch.undo()

        summary = {}
        for chunk in printed:
            try:
                summary = json.loads(chunk)
            except ValueError:
                continue
        return code, summary, commits

    return _run


def test_dry_run_reports_success_and_mutates_nothing(run_backfill, tmp_path):
    """A dry run must not commit the database or rewrite input_info.json."""
    job = _make_job(tmp_path, "0123abcd-0123-0123-0123-000000000001", {
        "via": "inat_phylogenetic_tree", "inat_observation_id": 111, "notes": "old title",
    })
    before = (tmp_path / "jobs" / job.id / "input_info.json").read_text()

    code, summary, commits = run_backfill([job], {111: _observation(111)}, [])

    assert code == 0
    assert summary["mode"] == "dry-run"
    assert commits == [], "a dry run committed the database"
    # Neither store changed.
    assert job.metrics["notes"] == "old title"
    assert "inat_genus" not in job.metrics
    assert (tmp_path / "jobs" / job.id / "input_info.json").read_text() == before


def test_dry_run_preview_comes_from_the_resolved_observations(run_backfill, tmp_path):
    """The preview must reflect this dataset, not a fixed author-specific list.

    A hardcoded id list produced an empty preview on every dataset but one, so
    the operator had nothing to inspect before running --apply.
    """
    jobs, observations = [], {}
    for index, observation_id in enumerate((555, 666, 777), start=1):
        jobs.append(_make_job(tmp_path, f"0123abcd-0123-0123-0123-00000000000{index}", {
            "via": "inat_phylogenetic_tree", "inat_observation_id": observation_id,
        }))
        observations[observation_id] = _observation(observation_id, genus="Russula")

    code, summary, _commits = run_backfill(jobs, observations, [])

    assert code == 0
    assert set(summary["preview"]) == {"555", "666", "777"}
    assert all("Russula" in title for title in summary["preview"].values())


@pytest.mark.parametrize(
    "bad_value",
    ["not-a-number", 0, -1, None, "", 0.0, [], {"nested": 1}],
)
def test_a_job_with_an_unusable_observation_id_is_skipped_not_crashed(
    run_backfill, tmp_path, bad_value
):
    """via=inat_phylogenetic_tree with no usable id must leave the population.

    Such a job used to reach genera[0] and raise KeyError -- the second time
    after db.session.commit() had already renamed the good rows, leaving their
    input_info.json files behind.
    """
    good = _make_job(tmp_path, "0123abcd-0123-0123-0123-00000000000a", {
        "via": "inat_phylogenetic_tree", "inat_observation_id": 111,
    })
    bad = _make_job(tmp_path, "0123abcd-0123-0123-0123-00000000000b", {
        "via": "inat_phylogenetic_tree", "inat_observation_id": bad_value,
    })

    code, summary, commits = run_backfill([good, bad], {111: _observation(111)}, ["--apply"])

    assert code == 0
    assert summary["skipped_without_observation"] == 1
    assert summary["job_count"] == 1
    assert summary["database_jobs_updated"] == 1
    assert commits == [True]

    # The good job was renamed in both stores.
    assert good.metrics["notes"] == "iNat # 111 - Amanita → Phylogenetic Tree"
    assert good.metrics["inat_genus"] == "Amanita"
    assert json.loads(
        (tmp_path / "jobs" / good.id / "input_info.json").read_text()
    )["notes"] == good.metrics["notes"]

    # The skipped one was touched in neither.
    assert "inat_genus" not in bad.metrics
    assert bad.metrics.get("notes") is None
    assert json.loads(
        (tmp_path / "jobs" / bad.id / "input_info.json").read_text()
    )["notes"] == "old title"


def test_a_job_with_no_metrics_at_all_is_skipped(run_backfill, tmp_path):
    job = _make_job(tmp_path, "0123abcd-0123-0123-0123-00000000000c", {
        "via": "inat_phylogenetic_tree",
    })

    code, summary, commits = run_backfill([job], {}, [])

    assert code == 0
    assert summary["skipped_without_observation"] == 1
    assert summary["job_count"] == 0
    assert commits == []


def test_a_legacy_title_still_supplies_the_observation_id(run_backfill, tmp_path):
    """Jobs predating inat_observation_id carry the id in their notes."""
    job = _make_job(tmp_path, "0123abcd-0123-0123-0123-00000000000d", {
        "notes": "iNaturalist obs 987 → Phylogenetic Tree",
    })

    code, summary, _commits = run_backfill([job], {987: _observation(987)}, [])

    assert code == 0
    assert summary["skipped_without_observation"] == 0
    assert set(summary["preview"]) == {"987"}


def test_apply_returns_nonzero_when_an_input_info_write_fails(run_backfill, tmp_path):
    """A partial run must not report success -- and must not half-apply either.

    The artifact is written first and the database row only follows for a job
    whose file actually changed. A job whose input_info.json could not be
    rewritten therefore keeps its old title in *both* stores rather than being
    renamed in the database alone, which used to leave the two permanently
    disagreeing with nothing to undo it with. Cron still sees the shortfall
    through the exit code.
    """
    good = _make_job(tmp_path, "0123abcd-0123-0123-0123-00000000000e", {
        "via": "inat_phylogenetic_tree", "inat_observation_id": 111,
    })
    # No input_info.json on disk, so _update_input_info() reports "missing".
    broken = _make_job(
        tmp_path, "0123abcd-0123-0123-0123-00000000000f",
        {"via": "inat_phylogenetic_tree", "inat_observation_id": 222},
        with_input_info=False,
    )

    code, summary, commits = run_backfill(
        [good, broken],
        {111: _observation(111), 222: _observation(222, genus="Russula")},
        ["--apply"],
    )

    assert code == 1, "a partial apply reported success"
    assert summary["input_info"] == {"missing": 1, "updated": 1}
    # Only the job whose artifact followed was renamed in the database.
    assert summary["database_jobs_updated"] == 1
    assert commits == [True]
    assert "notes" not in (broken.metrics or {})
    assert good.metrics["notes"] == "iNat # 111 - Amanita → Phylogenetic Tree"


def test_input_info_failure_removes_its_temporary_file(
    backfill, tmp_path, monkeypatch
):
    from app.config import Config

    job = _make_job(tmp_path, "0123abcd-0123-0123-0123-000000000012", {
        "via": "inat_phylogenetic_tree", "inat_observation_id": 111,
    })
    monkeypatch.setattr(Config, "JOB_DIR", tmp_path / "jobs")
    original_replace = Path.replace

    def fail_replace(self, target):
        if self.name == "input_info.json.tmp":
            raise OSError("simulated replace failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)

    assert backfill._update_input_info(job, "new title") == "write_failed"
    assert not (Path(job.job_dir) / "input_info.json.tmp").exists()


def test_apply_returns_zero_when_every_store_agrees(run_backfill, tmp_path):
    job = _make_job(tmp_path, "0123abcd-0123-0123-0123-000000000010", {
        "via": "inat_phylogenetic_tree", "inat_observation_id": 111,
    })

    code, summary, commits = run_backfill([job], {111: _observation(111)}, ["--apply"])

    assert code == 0
    assert summary["input_info"] == {"updated": 1}
    assert commits == [True]


def test_an_unresolvable_observation_stops_the_run_before_any_write(
    run_backfill, tmp_path
):
    """Nothing is committed when an observation cannot be resolved at all."""
    job = _make_job(tmp_path, "0123abcd-0123-0123-0123-000000000011", {
        "via": "inat_phylogenetic_tree", "inat_observation_id": 111,
    })

    code, summary, commits = run_backfill([job], {}, ["--apply"])

    assert code == 1
    assert summary["unresolved"][0]["observation_id"] == 111
    assert commits == []
    assert "inat_genus" not in job.metrics
    assert json.loads(
        (tmp_path / "jobs" / job.id / "input_info.json").read_text()
    )["notes"] == "old title"


# --- backfill rollback journal ----------------------------------------------
#
# The transaction is artifact-first, database-second, so between the first
# rewrite and the commit there is a window where input_info.json files on disk
# disagree with the database. The journal is what closes that window. It used to
# hold each file's original *bytes*, so a run across the whole job corpus kept
# every rewritten input_info.json -- submitted FASTA and all -- resident until it
# committed. These pin the disk-backed replacement: same transactional
# behaviour, memory that does not grow with the artifacts.

def _backup_files(tmp_path):
    return sorted(
        path.name for path in (tmp_path / "jobs").rglob("*" + _BACKUP_SUFFIX)
    )


_BACKUP_SUFFIX = ".backfill-backup"


def test_the_journal_holds_paths_rather_than_artifact_contents(backfill, tmp_path, monkeypatch):
    """O(1) memory per job: the journal must not grow with the file's size."""
    from app.config import Config

    monkeypatch.setattr(Config, "JOB_DIR", tmp_path / "jobs")
    job = _make_job(tmp_path, "0123abcd-0123-0123-0123-000000000020", {
        "via": "inat_phylogenetic_tree", "inat_observation_id": 111,
    })
    # A megabyte-scale artifact, the way a real submitted FASTA is.
    payload = {"notes": "old title", "sequence": ">A\n" + ("ACGT" * 300_000)}
    (tmp_path / "jobs" / job.id / "input_info.json").write_text(
        json.dumps(payload), encoding="utf-8")

    journal = []
    assert backfill._update_input_info(job, "new title", journal=journal) == "updated"

    assert len(journal) == 1
    entry = journal[0]
    assert all(isinstance(item, Path) for item in entry), entry
    # Nothing in the journal is anywhere near the size of the artifact.
    assert sum(len(str(item)) for item in entry) < 1000


def test_a_committed_run_leaves_no_rollback_files_behind(run_backfill, tmp_path):
    """Backups exist only for the life of the transaction."""
    jobs = [
        _make_job(tmp_path, f"0123abcd-0123-0123-0123-00000000002{index}", {
            "via": "inat_phylogenetic_tree", "inat_observation_id": 100 + index,
        })
        for index in (1, 2, 3)
    ]
    observations = {100 + index: _observation(100 + index) for index in (1, 2, 3)}

    code, summary, commits = run_backfill(jobs, observations, ["--apply"])

    assert code == 0
    assert commits == [True]
    assert summary["input_info"] == {"updated": 3}
    assert "input_info_backups_left" not in summary
    assert _backup_files(tmp_path) == []
    for job in jobs:
        assert json.loads(
            (tmp_path / "jobs" / job.id / "input_info.json").read_text()
        )["notes"] == job.metrics["notes"]


def test_a_failed_commit_restores_every_file_the_run_had_rewritten(
    run_backfill, tmp_path
):
    """The point of the journal: no artifact may outlive a rolled-back commit."""
    jobs = [
        _make_job(tmp_path, f"0123abcd-0123-0123-0123-00000000003{index}", {
            "via": "inat_phylogenetic_tree", "inat_observation_id": 200 + index,
        })
        for index in (1, 2, 3)
    ]
    observations = {200 + index: _observation(200 + index) for index in (1, 2, 3)}
    before = {
        job.id: (tmp_path / "jobs" / job.id / "input_info.json").read_text()
        for job in jobs
    }

    def explode():
        raise RuntimeError("simulated commit failure")

    code, summary, commits = run_backfill(
        jobs, observations, ["--apply"], commit=explode)

    assert code == 1
    assert commits == [True], "the commit was attempted"
    assert summary["database_jobs_updated"] == 0
    assert summary["error"] == "database commit failed: RuntimeError"
    assert summary["input_info_restore_failures"] == []

    # Every artifact is byte-for-byte what it was, and the sequence survived.
    for job in jobs:
        path = tmp_path / "jobs" / job.id / "input_info.json"
        assert path.read_text() == before[job.id]
        assert json.loads(path.read_text())["notes"] == "old title"
    # A successful rollback consumes its own backups.
    assert _backup_files(tmp_path) == []


def test_a_backup_that_cannot_be_restored_is_reported_and_kept(
    backfill, tmp_path, monkeypatch
):
    """A failed rollback must not also delete the only surviving original."""
    from app.config import Config

    monkeypatch.setattr(Config, "JOB_DIR", tmp_path / "jobs")
    job = _make_job(tmp_path, "0123abcd-0123-0123-0123-000000000040", {
        "via": "inat_phylogenetic_tree", "inat_observation_id": 111,
    })
    journal = []
    assert backfill._update_input_info(job, "new title", journal=journal) == "updated"
    backup_path = journal[0][1]
    assert backup_path.is_file()

    def fail_replace(source, target):
        raise OSError("simulated restore failure")

    monkeypatch.setattr(backfill.os, "replace", fail_replace)
    failures = backfill._restore_input_info(journal)

    assert failures == [str(tmp_path / "jobs" / job.id / "input_info.json")]
    # The original is still recoverable by hand.
    assert json.loads(backup_path.read_text())["notes"] == "old title"


def test_a_stale_backup_is_refused_rather_than_overwritten(
    backfill, tmp_path, monkeypatch
):
    """That file holds a previous crashed run's original; it is not ours to lose."""
    from app.config import Config

    monkeypatch.setattr(Config, "JOB_DIR", tmp_path / "jobs")
    job = _make_job(tmp_path, "0123abcd-0123-0123-0123-000000000041", {
        "via": "inat_phylogenetic_tree", "inat_observation_id": 111,
    })
    input_info = tmp_path / "jobs" / job.id / "input_info.json"
    stale = input_info.with_name(input_info.name + _BACKUP_SUFFIX)
    stale.write_text(json.dumps({"notes": "the true original"}), encoding="utf-8")
    current = input_info.read_text()

    journal = []
    assert backfill._update_input_info(job, "new title", journal=journal) == "stale_backup"

    assert journal == []
    assert input_info.read_text() == current, "the artifact was rewritten anyway"
    assert json.loads(stale.read_text())["notes"] == "the true original"


def test_a_failed_write_leaves_neither_a_temp_file_nor_a_backup(
    backfill, tmp_path, monkeypatch
):
    """Nothing was changed, so there is nothing to roll back to."""
    from app.config import Config

    monkeypatch.setattr(Config, "JOB_DIR", tmp_path / "jobs")
    job = _make_job(tmp_path, "0123abcd-0123-0123-0123-000000000042", {
        "via": "inat_phylogenetic_tree", "inat_observation_id": 111,
    })
    original_replace = Path.replace

    def fail_replace(self, target):
        if self.name == "input_info.json.tmp":
            raise OSError("simulated replace failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)

    journal = []
    assert backfill._update_input_info(job, "new title", journal=journal) == "write_failed"

    assert journal == []
    assert _backup_files(tmp_path) == []
    assert not (Path(job.job_dir) / "input_info.json.tmp").exists()


def test_the_rewrite_preserves_the_artifacts_permissions(
    backfill, tmp_path, monkeypatch
):
    """os.replace() hands the target the temp file's mode.

    input_info.json is rewritten in place by ordinary user actions running as
    the dikarya services, so a maintenance run that dropped it to 0644 would
    quietly take that away -- the tree_state.json failure in AGENTS.md.
    """
    from app.config import Config

    monkeypatch.setattr(Config, "JOB_DIR", tmp_path / "jobs")
    job = _make_job(tmp_path, "0123abcd-0123-0123-0123-000000000043", {
        "via": "inat_phylogenetic_tree", "inat_observation_id": 111,
    })
    input_info = tmp_path / "jobs" / job.id / "input_info.json"
    input_info.chmod(0o664)

    assert backfill._update_input_info(job, "new title", journal=[]) == "updated"

    assert input_info.stat().st_mode & 0o777 == 0o664


# --------------------------------------------------------------------------
# scripts/dikarya-claude-review: the spend boundary
# --------------------------------------------------------------------------

WRAPPER = REPO_ROOT / "scripts" / "dikarya-claude-review"


def _wrapper_budget_verdict(value):
    """Run the wrapper's budget validation alone and report accept/reject.

    The wrapper exits 78 before spending anything when the prompt files are not
    installed, which they are not in a test environment. That is *after* the
    budget check, so a 64 means the budget was rejected and anything else means
    it was accepted.
    """
    completed = subprocess.run(
        ["bash", str(WRAPPER)],
        input="",
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "DIKARYA_CLAUDE_MAX_BUDGET": value,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )
    if completed.returncode == 64 and "budget" in completed.stderr:
        return "reject"
    return "accept"


@pytest.mark.parametrize("value", ["1.00", "0.01", "2.00", "1.5", "1", "01.50"])
def test_wrapper_accepts_well_formed_budgets_within_the_cap(value):
    assert _wrapper_budget_verdict(value) == "accept", value


@pytest.mark.parametrize(
    "value",
    [
        "",            # handled by ${VAR:-default}, so it never reaches the CLI raw
        ".",           # no digits at all, but the old glob let it through
        "0",           # zero budget
        "0.00",
        "-1",          # sign
        "+1",
        "1e9",         # exponent
        "1E9",
        "NaN",
        "nan",
        "Infinity",
        "inf",
        "-inf",
        "1.234",       # more precision than dollars have
        "1..2",
        "1.",
        "10000",       # above the format's width
        "2.01",        # just above the hard cap
        "5.00",
        "99.99",
        "0500",        # 500 dollars: well formed, far above the cap
        " 1.00",       # whitespace: the wrapper is the strict layer
        "1.00 ",
        "1.00; rm -rf /",
        "$(echo 1.00)",
        "`echo 1.00`",
        "1.00\n2.00",
    ],
)
def test_wrapper_rejects_malformed_or_unbounded_budgets(value):
    """The one knob that costs money gets a format *and* a ceiling.

    The previous glob rejected only empty strings, non-[0-9.] characters and
    multiple dots. It accepted "." and, more importantly, any magnitude -- so a
    caller who could set the variable could simply delete the spending cap.

    An empty value is the one entry here that is not really a rejection: the
    wrapper's ${DIKARYA_CLAUDE_MAX_BUDGET:-1.00} substitutes the default first,
    so an empty variable behaves as unset. It is listed to pin that behaviour.
    """
    if value == "":
        # Empty means "unset", which resolves to the default and is accepted.
        assert _wrapper_budget_verdict(value) == "accept"
        return
    assert _wrapper_budget_verdict(value) == "reject", value


def _app_config():
    spec = importlib.util.spec_from_file_location(
        "config_under_test", REPO_ROOT / "app" / "config.py"
    )
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    return config


def test_the_hard_cap_is_the_same_number_on_both_sides():
    """One ceiling, stated in two languages; drift between them is the bug.

    The figure is derived in app/config.py (twice the shipped default, ~6x the
    measured cost of a review, and a bounded worst-case daily spend against
    CLAUDE_REVIEW_MAX_DAILY). This asserts the wrapper agrees with it.
    """
    config = _app_config()
    wrapper = WRAPPER.read_text()

    cents_line = next(
        line for line in wrapper.splitlines()
        if line.startswith("MAX_BUDGET_HARD_CENTS=")
    )
    wrapper_cents = int(cents_line.split("=", 1)[1])

    assert wrapper_cents == int(config.CLAUDE_REVIEW_MAX_BUDGET_HARD_CAP_USD * 100)
    # And the documented figure has not silently drifted from the code.
    assert str(config.CLAUDE_REVIEW_MAX_BUDGET_HARD_CAP_USD) == "2.00"
    assert "$2.00" in (REPO_ROOT / "ARCHITECTURE.md").read_text()


def test_everything_the_app_can_emit_is_accepted_by_the_wrapper(monkeypatch):
    """The invariant that actually matters across the privilege boundary.

    The two layers are not the same grammar: the app canonicalizes (it tolerates
    whitespace and any accepted precision, and always emits the two-decimal
    form), while the wrapper enforces only that canonical shape. That asymmetry
    is safe in exactly one direction -- a legal configuration must never fail
    inside sudo -- which is what this asserts.
    """
    config = _app_config()
    candidates = [
        "0.01", "1", "1.0", "1.00", "1.5", " 1.25 ", "2", "2.00", "01.50",
        # Rejected by the app, so it emits its default instead.
        ".", "0", "-1", "1e9", "NaN", "Infinity", "2.01", "99999", "1.234",
        "1.00; rm -rf /", "",
    ]
    for candidate in candidates:
        monkeypatch.setenv("BUDGET_PROBE", candidate)
        emitted = config.budget_env("BUDGET_PROBE", "1.00")
        assert _wrapper_budget_verdict(emitted) == "accept", (candidate, emitted)


def test_the_app_canonicalizes_and_clamps_out_of_range_values(monkeypatch):
    config = _app_config()
    cases = {
        "1": "1.00", "1.5": "1.50", " 1.25 ": "1.25", "01.50": "1.50",
        "2.00": "2.00",
        # Anything unusable falls back to the default, never upward.
        "2.01": "1.00", "5.00": "1.00", "0": "1.00", ".": "1.00",
        "-1": "1.00", "1e9": "1.00", "NaN": "1.00", "": "1.00",
    }
    for raw, expected in cases.items():
        monkeypatch.setenv("BUDGET_PROBE", raw)
        assert config.budget_env("BUDGET_PROBE", "1.00") == expected, raw

    # Unset is left to the caller's default rather than canonicalized.
    monkeypatch.delenv("BUDGET_PROBE", raising=False)
    assert config.budget_env("BUDGET_PROBE", "1.00") == "1.00"


def test_the_shipped_default_budget_is_unchanged():
    """The cap is a backstop; the operating budget must stay where it was."""
    config = _app_config()
    assert config.Config.CLAUDE_REVIEW_MAX_BUDGET_USD == "1.00"


# --------------------------------------------------------------------------
# wsgi.py
# --------------------------------------------------------------------------

def test_wsgi_does_not_hardcode_the_werkzeug_debugger():
    """`python wsgi.py` must not enable the interactive debugger on its own.

    app.run(debug=True) there exposes remote code execution to anyone who can
    reach a traceback, on any host where that command is how the service starts.
    """
    source = (REPO_ROOT / "wsgi.py").read_text()
    assert "app.run(debug=True)" not in source
    assert 'app.run(debug=bool(app.config.get("DEBUG")))' in source


def test_default_config_leaves_debug_off():
    spec = importlib.util.spec_from_file_location(
        "config_debug_probe", REPO_ROOT / "app" / "config.py"
    )
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)

    assert config.ProductionConfig.DEBUG is False
    assert config.DevelopmentConfig.DEBUG is True
