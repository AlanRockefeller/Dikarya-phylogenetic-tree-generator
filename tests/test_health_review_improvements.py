"""Regressions from the November 6 production health review."""
import importlib.util
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask, g

from app.api import routes
from app.api_v1 import routes as v1
from app.config import Config
from app.models import TreeBuilderParams
from app.monitoring import services as monitoring
from app.services import blast_service, tree_builder_service as tbs
from app.services.tree_parameter_validation import (
    TREE_METHOD_LABELS, TREE_MIN_SEQUENCES, submitted_tree_count_error, tree_sequence_count_error,
)

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("november_digest", ROOT / "scripts/dikarya_log_digest.py")
digest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(digest)
TWO = ">a\nACGTACGT\n>b\nACGTACGA\n"
ONE = ">a\nACGTACGT\n"
FOUR = TWO + ">c\nACGTACGC\n>d\nACGTACGG\n"


def access_line(timestamp="06/Nov/2026:09:59:00 +0000", host="dikarya.us"):
    return (f'192.0.2.1 - - [{timestamp}] "GET /api/background/abc HTTP/2.0" '
            f'504 20 "-" "browser" host="{host}" server="{host}" '
            'edge_rid="edge" app_rid="app" rt="0.004" upstream_status="504" urt="0.004"\n')


def test_current_nginx_format_and_fixed_reader_keep_host_scoping():
    match = digest.parse_nginx_access(access_line())
    assert match and match["status"] == "504"
    assert digest.parse_nginx_access(access_line(host="images.mushroomobserver.org")) is None
    old = access_line().split(' edge_rid=')[0] + "\n"
    assert digest.parse_nginx_access(old)
    reader = (ROOT / "scripts/dikarya-nginx-log").read_text()
    awk = re.search(r"app_access\).*?/usr/bin/awk '([^']+)'", reader)[1]
    result = subprocess.run(["awk", awk], input=old + access_line()
                            + access_line(host="images.mushroomobserver.org"),
                            text=True, capture_output=True, check=True)
    assert result.stdout == old + access_line()


def test_stale_scoped_reader_merges_newer_app_history(monkeypatch):
    commands = []
    def run(command, **kwargs):
        commands.append(command.copy())
        text = (access_line("29/Oct/2026:01:00:00 +0000") if command[-2] == "app-access-all"
                else access_line() if command[-2] == "access-all" else "")
        return SimpleNamespace(returncode=0, stdout=text)
    monkeypatch.setattr(digest.subprocess, "run", run)
    counts, coverage = digest.analyze_nginx(datetime(2026, 11, 4), datetime(2026, 11, 6, 10))
    assert commands[0][-2] == "app-access-all"
    assert commands[1][-2] == "access-all"
    assert counts[("access", "504", "GET /api/background/<task_id>")] == 1
    assert "range=2026-10-29 01:00:00..2026-11-06 09:59:00" in coverage[0]
    assert "WARNING partial historical coverage" in coverage[0]
    assert "scoped=no" in coverage[0]


@pytest.mark.parametrize("fallback_app_time", [None, "06/Nov/2026:08:00:00 +0000",
                                              "06/Nov/2026:09:00:00 +0000"])
def test_poorer_fallback_retains_scoped_failures_and_stale_warning(
        monkeypatch, fallback_app_time):
    scoped = access_line("06/Nov/2026:09:00:00 +0000")
    fallback = access_line(host="images.mushroomobserver.org")
    if fallback_app_time:
        fallback += access_line(fallback_app_time)
    def run(command, **kwargs):
        text = {"app-access-all": scoped, "access-all": fallback}.get(command[-2], "")
        return SimpleNamespace(returncode=0, stdout=text)
    monkeypatch.setattr(digest.subprocess, "run", run)

    counts, coverage = digest.analyze_nginx(datetime(2026, 11, 4), datetime(2026, 11, 6, 10))

    assert counts[("access", "504", "GET /api/background/<task_id>")] == 1
    assert "range=2026-11-06 09:00:00..2026-11-06 09:00:00" in coverage[0]
    assert "scoped=yes" in coverage[0]
    assert "WARNING stale access history" in coverage[0]


def test_newer_fallback_merges_history_without_double_counting_overlap(monkeypatch):
    historical = access_line("04/Nov/2026:00:00:00 +0000").replace("504 20", "502 20")
    overlap = access_line("06/Nov/2026:09:00:00 +0000")
    # Identical requests within one source must still be counted separately.
    scoped = historical + overlap * 2
    fallback = overlap * 2 + access_line() + access_line(host="images.mushroomobserver.org")
    def run(command, **kwargs):
        text = {"app-access-all": scoped, "access-all": fallback}.get(command[-2], "")
        return SimpleNamespace(returncode=0, stdout=text)
    monkeypatch.setattr(digest.subprocess, "run", run)

    counts, coverage = digest.analyze_nginx(datetime(2026, 11, 4), datetime(2026, 11, 6, 10))

    assert counts[("access", "502", "GET /api/background/<task_id>")] == 1
    assert counts[("access", "504", "GET /api/background/<task_id>")] == 3
    assert "in-window-app=4" in coverage[0]
    assert "scoped=no" in coverage[0]
    assert "WARNING partial historical coverage" not in coverage[0]
    assert "WARNING stale access history" not in coverage[0]


@pytest.mark.parametrize("failure", [1, subprocess.TimeoutExpired("access-all", 45)])
def test_failed_fallback_keeps_valid_scoped_history(monkeypatch, failure):
    def run(command, **kwargs):
        if command[-2] == "access-all":
            if isinstance(failure, Exception):
                raise failure
            return SimpleNamespace(returncode=failure, stdout="")
        return SimpleNamespace(returncode=0, stdout=(
            access_line("06/Nov/2026:09:00:00 +0000")
            if command[-2] == "app-access-all" else ""))
    monkeypatch.setattr(digest.subprocess, "run", run)

    counts, coverage = digest.analyze_nginx(datetime(2026, 11, 4), datetime(2026, 11, 6, 10))

    assert counts[("access", "504", "GET /api/background/<task_id>")] == 1
    assert "unavailable" not in coverage[0]
    assert "WARNING stale access history" in coverage[0]


def test_unfiltered_reader_coverage_ignores_unrelated_hosts(monkeypatch):
    def run(command, **kwargs):
        if command[-2] == "app-access-all":
            return SimpleNamespace(returncode=2, stdout="")
        text = (access_line("06/Nov/2026:09:00:00 +0000")
                + access_line(host="images.mushroomobserver.org")
                + access_line("01/Nov/2026:00:00:00 +0000", host="images.mushroomobserver.org"))
        return SimpleNamespace(returncode=0, stdout=text if command[-2] == "access-all" else "")
    monkeypatch.setattr(digest.subprocess, "run", run)

    counts, coverage = digest.analyze_nginx(datetime(2026, 11, 4), datetime(2026, 11, 6, 10))

    assert counts[("access", "504", "GET /api/background/<task_id>")] == 1
    assert "range=2026-11-06 09:00:00..2026-11-06 09:00:00" in coverage[0]
    assert "WARNING partial historical coverage" in coverage[0]
    assert "WARNING stale access history" in coverage[0]


def test_stale_history_is_never_reported_as_complete(monkeypatch):
    monkeypatch.setattr(digest.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=0, stdout=access_line("29/Oct/2026:01:00:00 +0000"))))
    _, coverage = digest.analyze_nginx(datetime(2026, 11, 4), datetime(2026, 11, 6, 10))
    assert "WARNING stale access history" in coverage[0]


def test_changed_nginx_format_reports_unparseable_app_records(monkeypatch):
    line = access_line().replace('rt="0.004"', "rt=0.004")
    monkeypatch.setattr(digest.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=0, stdout=line)))
    _, coverage = digest.analyze_nginx(datetime(2026, 11, 4), datetime(2026, 11, 6, 10))
    assert "WARNING unparsed app access records=1" in coverage[0]


def test_browser_and_worker_minimum_counts_agree():
    source = (ROOT / "app/templates/sequence_entry.html").read_text()
    literal = re.search(r"const TREE_MIN_SEQUENCES = (\{[^;]+\});", source)[1]
    quoted = re.sub(r"(\w+):", r'"\1":', literal)
    assert json.loads(quoted) == TREE_MIN_SEQUENCES
    labels = re.search(r"const TREE_METHOD_LABELS = (\{[^;]+\});", source)[1]
    labels = re.sub(r"(\w+):", r'"\1":', labels).replace("'", '"')
    assert json.loads(labels) == TREE_METHOD_LABELS


@pytest.mark.parametrize("method,minimum", TREE_MIN_SEQUENCES.items())
def test_method_specific_sequence_counts(method, minimum):
    assert tree_sequence_count_error(method, minimum) is None
    assert f"at least {minimum}" in tree_sequence_count_error(method, minimum - 1)


def test_deferred_imports_and_blast_can_still_augment_small_inputs():
    assert submitted_tree_count_error({"tree_method": "iqtree_fast", "sequence": TWO})
    assert submitted_tree_count_error({"tree_method": "fasttree", "sequence": TWO}) is None
    assert submitted_tree_count_error({"tree_method": "iqtree_fast", "sequence": ONE,
                                       "blast_mode": "ncbi"}) is None
    assert submitted_tree_count_error({"tree_method": "mrbayes", "accessions": ["AB123456"]}) is None
    assert submitted_tree_count_error({"tree_method": "mrbayes", "sequence": TWO,
                                       "_inat_tree_preparation": {"pending": True}}) is None


@pytest.mark.parametrize("mode", [None, "", "auto", "on", "off", " OFF ",
                                 "ncbi", "none", "disabled"])
@pytest.mark.parametrize("sequence,count", [(ONE, 1), (TWO, 2)])
def test_submission_count_validation_matches_worker_blast_policy(mode, sequence, count):
    from app.workers.tasks import _normalize_blast_mode, _should_blast_single_only

    params = {"tree_method": "iqtree_fast", "sequence": sequence}
    if mode is not None:
        params["blast_mode"] = mode
    worker_will_blast = _should_blast_single_only(_normalize_blast_mode(mode), count)

    error = submitted_tree_count_error(params)

    assert (error is None) == worker_will_blast
    if not worker_will_blast:
        assert "at least 3" in error


def test_auto_blast_does_not_bypass_final_builder_minimum(tmp_path):
    alignment = tmp_path / "alignment.fasta"
    alignment.write_text(ONE)
    with pytest.raises(ValueError, match="only one sequence"):
        tbs._require_enough_sequences("iqtree_fast", alignment)


def undecorated(function):
    while hasattr(function, "__wrapped__"):
        function = function.__wrapped__
    return function


@pytest.mark.parametrize("module,code", [(routes, 400), (v1, 422)])
def test_small_input_is_rejected_before_database_or_enqueue(module, code, monkeypatch):
    app = Flask(__name__)
    job = Mock()
    enqueue = Mock()
    monkeypatch.setattr(module, "Job", job)
    monkeypatch.setattr(module, "enqueue_job", enqueue)
    # Exercise validation after preparation, including a dedup reduction.
    monkeypatch.setattr(module, "prepare_phylo_job_params",
                        lambda params: params.update(sequence=TWO))
    with app.test_request_context(method="POST", json={
            "input_type": "pasted_sequence", "sequence": FOUR, "tree_method": "iqtree_fast"}):
        response = app.make_response(undecorated(module.create_job)())
    assert response.status_code == code
    assert "at least 3" in str(response.get_json())
    job.assert_not_called()
    enqueue.assert_not_called()


@pytest.mark.parametrize("resumable,code", [(True, 202), (False, 504)])
def test_only_resumable_blast_timeouts_become_pending(resumable, code, monkeypatch):
    def timeout(*args, **kwargs):
        g.blast_can_resume = resumable
        raise TimeoutError("upstream is still working")
    monkeypatch.setattr(blast_service, "blast_from_sequence", timeout)
    with Flask(__name__).test_request_context(method="POST", headers={"X-Dikarya-BLAST-Pending": "1"}):
        response, status = routes._run_blast_request({"query": "ACGT" * 20})
    assert status == code
    data = response.get_json()
    assert data["can_resume"] is resumable
    if resumable:
        assert data["status"] == "pending" and "Continue NCBI BLAST" in data["message"]
        assert "error" not in data


def test_old_browser_tabs_keep_the_resumable_error_contract(monkeypatch):
    def timeout(*args, **kwargs):
        g.blast_can_resume = True
        raise TimeoutError("upstream is still working")
    monkeypatch.setattr(blast_service, "blast_from_sequence", timeout)
    with Flask(__name__).test_request_context(method="POST"):
        response, status = routes._run_blast_request({"query": "ACGT" * 20})
    assert status == 504
    assert response.get_json()["can_resume"] is True
    assert "Continue NCBI BLAST" in response.get_json()["error"]


def test_missing_queue_warns_even_when_other_workers_are_healthy(monkeypatch, caplog):
    app = Flask(__name__)
    app.config.update(WORKER_EXPECTED_COUNTS={"phylo_high": 2, "phylo_bulk": 1, "voucher_sync": 1},
                      WORKER_HEALTH_STARTUP_GRACE_SECONDS=120)
    workers = [{"id": "a", "status": "healthy", "queues": ["phylo_high"]},
               {"id": "b", "status": "healthy", "queues": ["phylo_high"]}]
    monkeypatch.setattr(monitoring, "_HEALTH_STATES", {})
    monkeypatch.setattr(monitoring, "_HEALTH_LAST_EMIT", {})
    monkeypatch.setattr(monitoring, "_WORKER_HEALTH_STARTED_AT", None)
    monkeypatch.setattr(monitoring, "get_worker_queue_health", lambda: {"workers": workers})
    with app.app_context():
        monitoring._emit_worker_health(0)
        assert not caplog.records
        monitoring._emit_worker_health(120)
        assert "health.worker_queue.phylo_bulk.unhealthy" in caplog.text
        assert "health.worker_queue.voucher_sync.unhealthy" in caplog.text
        assert "health.worker_heartbeat.unhealthy" not in caplog.text
        caplog.clear()
        workers.extend([{"id": "c", "status": "healthy", "queues": ["phylo_bulk"]},
                        {"id": "d", "status": "healthy", "queues": ["voucher_sync"]}])
        monitoring._emit_worker_health(121)
        assert "health.worker_queue.phylo_bulk.recovered" in caplog.text
        assert "health.worker_queue.voucher_sync.recovered" in caplog.text


def test_voucher_backlog_is_included_in_queue_health(monkeypatch):
    from app.workers import queue
    import rq.registry
    connection = SimpleNamespace(
        ping=lambda: True, llen=lambda key: 25 if key.endswith("voucher_sync") else 0)
    monkeypatch.setattr(queue, "get_redis_connection", lambda: connection)
    monkeypatch.setattr(rq.registry, "StartedJobRegistry",
                        lambda **kwargs: SimpleNamespace(count=0))
    monkeypatch.setattr(monitoring, "_emit_worker_health", Mock())
    monkeypatch.setattr(monitoring, "db", Mock())
    monkeypatch.setattr(monitoring, "Job", Mock())
    transition = Mock()
    monkeypatch.setattr(monitoring, "_transition", transition)
    monitoring.emit_health_transitions({"disk_usage": 10, "memory_percent": 10})
    calls = [call for call in transition.call_args_list if call.args[0] == "queue_depth"]
    assert len(calls) == 1 and calls[0].args[1] is True
    assert "voucher_sync=25" in calls[0].args[2]


@pytest.mark.parametrize("final_ess", [300, 40])
def test_mrbayes_continuation_keeps_ceiling_checkpoint_and_remaining_budget(
        tmp_path, monkeypatch, final_ess):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tmp_path / "logs").mkdir()
    alignment = tmp_path / "alignment.fasta"
    alignment.write_text(FOUR)
    nexus = tree / "mrbayes_input.nex"
    commands, timeouts = [], []
    now = [0]
    def run(command, log_file=None, timeout=None):
        commands.append(nexus.read_text())
        timeouts.append(timeout)
        gen, ess = (20000, 9) if len(commands) == 1 else (80000, final_ess)
        Path(str(nexus) + ".mcmc").write_text(f"Gen\tavgStdDev(s)\n{gen}\t0.005\n")
        Path(str(nexus) + ".pstat").write_text(f"Parameter\tminESS\tPSRF\nTL\t{ess}\t1.001\n")
        Path(str(nexus) + ".ckp").write_text("checkpoint")
        Path(str(nexus) + ".con.tre").write_text("(A,B,C,D);")
        now[0] += 20
        return 0, "", ""
    monkeypatch.setattr(tbs, "run_command", run)
    # 20 s per segment: the remaining 80k generations need ~80 s, which fits.
    monkeypatch.setattr(tbs, "_tool_timeout_seconds", lambda *args: 600)
    monkeypatch.setattr(tbs.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(tbs, "sanitize_fasta_headers", lambda *args: {})
    monkeypatch.setattr(tbs, "_convert_fasta_to_nexus", lambda src, dst: dst.write_text("#NEXUS\n"))
    monkeypatch.setattr(tbs, "_convert_nexus_to_newick", lambda *args: None)
    monkeypatch.setattr(tbs, "restore_tree_names", lambda *args: None)
    logger = Mock()
    metadata = tbs._run_mrbayes(
        alignment, tree / "result.newick", tree / "result.nexus",
        TreeBuilderParams(method="mrbayes", mcmc_generations=100000, threads=1),
        Config, logger)
    assert len(commands) == 2
    assert "stoprule=yes" in commands[0] and "append=yes" not in commands[0]
    assert "append=yes" in commands[1] and "stoprule=no" in commands[1]
    # Capped at 4x the first segment, not run on to the 100,000 ceiling.
    assert "ngen=100000" in commands[0] and "ngen=80000" in commands[1]
    assert timeouts == [600, 580]
    assert metadata["mcmc_generations_completed"] == 80000
    assert metadata["mrbayes_early_stop_continuation"]["from_generation"] == 20000
    assert metadata["mrbayes_early_stop_continuation"]["to_generation"] == 80000
    assert metadata["mrbayes_early_stop_continuation"]["limited_by"] == "max_factor"
    assert metadata["converged"] is (final_ess >= 200)
    # The provisional failed check is not logged as degraded final work.
    final_warnings = [str(call) for call in logger.warning.call_args_list
                      if "MrBayes convergence check FAILED" in str(call)]
    assert len(final_warnings) == (0 if final_ess >= 200 else 1)
