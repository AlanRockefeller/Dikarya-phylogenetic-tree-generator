"""Regression coverage for MrBayes convergence-based early stopping.

The 50,000-generation default was demonstrated inadequate on a real Dikarya
analysis (min ESS ~10, max PSRF ~1.10). New jobs now run up to 1,000,000
generations and let MrBayes stop as soon as its independent runs agree on split
frequencies -- without that stop rule being mistaken for "this analysis
converged", and without rewriting what older jobs ran.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask, g

from app.api import routes
from app.api_v1 import routes as routes_v1
from app.config import Config
from app.models import TreeBuilderParams
from app.services import tree_builder_service as tbs


JOB_ID = "12345678-1234-1234-1234-123456789abc"

FASTA = ">A\nACGTACGTAC\n>B\nACGTACGTAT\n>C\nACGTACGTAA\n"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_new_mrbayes_jobs_default_to_one_million_max_generations():
    assert Config.DEFAULT_MCMC_GENERATIONS == 1_000_000
    assert TreeBuilderParams(method="mrbayes").mcmc_generations == 1_000_000


def test_new_mrbayes_jobs_default_to_convergence_based_early_stopping():
    assert Config.DEFAULT_MCMC_STOP_EARLY is True
    assert TreeBuilderParams(method="mrbayes").mcmc_stop_early is True
    # RAxML's unrelated bootstrap-convergence flag must stay off.
    assert TreeBuilderParams(method="mrbayes").early_stopping is False


# ---------------------------------------------------------------------------
# Generated MrBayes block
# ---------------------------------------------------------------------------

def _run_mrbayes(tmp_path, mcmc_file=None, **param_overrides):
    """Run _run_mrbayes with every external tool stubbed out.

    Returns (nexus_text, metadata, task_logger).
    """
    tree_dir = tmp_path / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    alignment = tmp_path / "alignment.fasta"
    alignment.write_text(FASTA)
    output_newick = tree_dir / "tree_original.newick"
    output_nexus = tree_dir / "tree_original.nexus"

    kwargs = {
        "method": "mrbayes",
        "model": "GTR+G",
        "mcmc_generations": 1_000_000,
        "mcmc_nruns": 2,
        "mcmc_nchains": 4,
        "mcmc_burnin_fraction": 0.25,
        "mcmc_stop_early": True,
    }
    kwargs.update(param_overrides)
    params = TreeBuilderParams(**kwargs)

    nexus_input = tree_dir / "mrbayes_input.nex"

    def _fake_run_command(cmd, log_file=None, timeout=None):
        # MrBayes writes the consensus tree beside its input.
        (tree_dir / "mrbayes_input.nex.con.tre").write_text("(A,B,C);")
        if mcmc_file is not None:
            (tree_dir / "mrbayes_input.nex.mcmc").write_text(mcmc_file)
        return 0, "", ""

    task_logger = MagicMock()
    with (
        patch.object(tbs, "sanitize_fasta_headers", return_value={}),
        patch.object(tbs, "_convert_fasta_to_nexus"),
        patch.object(tbs, "_convert_nexus_to_newick"),
        patch.object(tbs, "restore_tree_names"),
        patch.object(tbs, "run_command", side_effect=_fake_run_command),
    ):
        metadata = tbs._run_mrbayes(
            alignment, output_newick, output_nexus, params, Config, task_logger
        )
    return nexus_input.read_text(), metadata, task_logger


def _mcmc_line(nexus_text):
    for line in nexus_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("mcmc "):
            return stripped
    raise AssertionError(f"no mcmc line in:\n{nexus_text}")


def test_stop_rule_emitted_with_two_runs(tmp_path):
    nexus, metadata, _ = _run_mrbayes(tmp_path)
    mcmc = _mcmc_line(nexus)

    assert "ngen=1000000" in mcmc
    assert "nruns=2" in mcmc
    assert "nchains=4" in mcmc
    assert "mcmcdiagn=yes" in mcmc
    assert "stoprule=yes" in mcmc
    assert "stopval=0.01" in mcmc
    assert metadata["mcmc_stoprule"] is True
    assert metadata["mcmc_stop_early_requested"] is True
    assert metadata["mcmc_stopval"] == 0.01
    assert metadata["mcmc_max_generations"] == 1_000_000


def test_stop_rule_absent_when_disabled(tmp_path):
    nexus, metadata, _ = _run_mrbayes(tmp_path, mcmc_stop_early=False)
    mcmc = _mcmc_line(nexus)

    assert "stoprule" not in mcmc
    assert "stopval" not in mcmc
    assert "ngen=1000000" in mcmc
    assert metadata["mcmc_stoprule"] is False
    assert metadata["mcmc_stop_early_requested"] is False
    assert "mcmc_stopval" not in metadata


def test_single_run_never_gets_an_invalid_stop_rule(tmp_path):
    nexus, metadata, task_logger = _run_mrbayes(
        tmp_path, mcmc_nruns=1, mcmc_stop_early=True
    )
    mcmc = _mcmc_line(nexus)

    assert "stoprule" not in mcmc
    # The requested analysis is preserved: one run stays one run.
    assert "nruns=1" in mcmc
    assert metadata["mcmc_nruns"] == 1
    assert metadata["mcmc_stoprule"] is False
    # ...and the mismatch is reported rather than hidden.
    assert metadata["mcmc_stop_early_requested"] is True
    assert task_logger.warning.called


def test_burnin_reaches_mcmc_sump_and_sumt(tmp_path):
    nexus, metadata, _ = _run_mrbayes(tmp_path, mcmc_burnin_fraction=0.4)

    assert "relburnin=yes burninfrac=0.4" in _mcmc_line(nexus)
    assert "sump relburnin=yes burninfrac=0.4;" in nexus
    assert "sumt relburnin=yes burninfrac=0.4;" in nexus
    assert metadata["mcmc_burnin_fraction"] == 0.4


def test_completed_generations_recorded_when_run_stops_early(tmp_path):
    # samplefreq for 1,000,000 generations is 500, so a last sample at 240,000
    # can only mean MrBayes ended the run itself.
    mcmc_file = "Gen\tavgStdDev(s)\n1000\t0.20\n240000\t0.009\n"
    _, metadata, _ = _run_mrbayes(tmp_path, mcmc_file=mcmc_file)

    assert metadata["mcmc_generations_completed"] == 240000
    assert metadata["mcmc_stopped_at_stopval"] is True
    assert metadata["asdsf"] == 0.009


def test_full_length_run_is_not_reported_as_stopped_early(tmp_path):
    mcmc_file = "Gen\tavgStdDev(s)\n1000\t0.20\n1000000\t0.03\n"
    _, metadata, _ = _run_mrbayes(tmp_path, mcmc_file=mcmc_file)

    assert metadata["mcmc_generations_completed"] == 1_000_000
    assert metadata["mcmc_stopped_at_stopval"] is False
    # The stop rule does not suppress the independent convergence verdict.
    assert metadata["converged"] is False
    assert metadata["convergence_warnings"]


def test_unreadable_generation_count_is_omitted_not_guessed(tmp_path):
    _, metadata, _ = _run_mrbayes(tmp_path)

    assert "mcmc_generations_completed" not in metadata
    assert "mcmc_stopped_at_stopval" not in metadata


# ---------------------------------------------------------------------------
# Web submission
# ---------------------------------------------------------------------------

def _submit(payload):
    """Call POST /api/job's handler and return the params it enqueued."""
    captured = {}

    def _fake_enqueue(job_params, *args, **kwargs):
        captured.update(job_params)
        return kwargs.get("job_id") or JOB_ID

    app = Flask(__name__)
    with (
        app.test_request_context(method="POST", json=payload),
        patch.object(routes, "enqueue_job", side_effect=_fake_enqueue),
        patch.object(routes, "Job", MagicMock()),
        patch.object(routes, "db", MagicMock()),
        patch.object(routes, "current_user", SimpleNamespace(is_authenticated=False)),
    ):
        routes.create_job.__wrapped__()
    return captured


def test_web_submission_defaults_mcmc_stop_early_on():
    params = _submit({"input_type": "sequence", "tree_method": "mrbayes"})

    assert params["mcmc_stop_early"] is True
    assert params["mcmc_generations"] == 1_000_000
    assert params["mcmc_nruns"] == 2
    assert params["mcmc_nchains"] == 4


def test_web_submission_propagates_explicit_mcmc_stop_early():
    params = _submit({
        "input_type": "sequence",
        "tree_method": "mrbayes",
        "mcmc_stop_early": False,
        "mcmc_generations": 50000,
    })

    assert params["mcmc_stop_early"] is False
    assert params["mcmc_generations"] == 50000


# ---------------------------------------------------------------------------
# Public API v1
# ---------------------------------------------------------------------------

def _undecorated(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _submit_v1(payload):
    captured = {}

    def _fake_enqueue(job_params, *args, **kwargs):
        # v1 now mints the id itself and commits the Job row before enqueueing,
        # so the id arrives as a keyword rather than coming back from RQ.
        captured.update(job_params)
        return kwargs.get("job_id") or JOB_ID

    app = Flask(__name__)
    with (
        app.test_request_context(method="POST", json=payload),
        patch.object(routes_v1, "enqueue_job", side_effect=_fake_enqueue),
        patch.object(routes_v1, "Job", MagicMock()),
        patch.object(routes_v1, "db", MagicMock()),
        patch.object(routes_v1, "serialize_job", return_value={}),
    ):
        g.api_user = SimpleNamespace(id=1)
        g.api_token = SimpleNamespace(id=1)
        response = _undecorated(routes_v1.create_job)()
    return captured, response


def test_api_v1_defaults_and_propagates_mcmc_stop_early():
    captured, _ = _submit_v1({
        "input_type": "pasted_sequence",
        "sequence": FASTA,
        "tree_method": "mrbayes",
    })
    assert captured["mcmc_stop_early"] is True
    assert captured["mcmc_generations"] == 1_000_000

    captured, _ = _submit_v1({
        "input_type": "pasted_sequence",
        "sequence": FASTA,
        "tree_method": "mrbayes",
        "mcmc_stop_early": False,
    })
    assert captured["mcmc_stop_early"] is False


def test_api_v1_rejects_a_non_boolean_mcmc_stop_early():
    captured, response = _submit_v1({
        "input_type": "pasted_sequence",
        "sequence": FASTA,
        "tree_method": "mrbayes",
        "mcmc_stop_early": "sometimes",
    })

    assert response.status_code == 422
    assert response.get_json()["error"]["details"]["field"] == "mcmc_stop_early"
    assert captured == {}


def test_api_v1_serializes_legacy_jobs_as_not_using_early_stopping(tmp_path):
    from app.api_v1.jobs import serialize_job

    job_dir = tmp_path / JOB_ID
    job_dir.mkdir()
    (job_dir / "input_info.json").write_text(json.dumps({
        "tree_method": "mrbayes", "mcmc_generations": 50000,
    }))

    app = Flask(__name__)
    job = SimpleNamespace(
        id=JOB_ID, status="completed", created_at=None, updated_at=None,
        input_type="sequence", metrics={},
    )
    with (
        app.test_request_context(),
        patch.object(Config, "JOB_DIR", tmp_path),
        patch("app.api_v1.jobs.url_for", return_value="/x"),
    ):
        payload = serialize_job(job)

    assert payload["params"]["mcmc_stop_early"] is False
    assert payload["params"]["mcmc_generations"] == 50000


# ---------------------------------------------------------------------------
# Recompute / backward compatibility
# ---------------------------------------------------------------------------

def test_recompute_preserves_an_explicit_stop_early_setting():
    from app.services.tree_edit_service import build_recompute_job_params

    stored = {
        "tree_method": "mrbayes", "mcmc_generations": 200000,
        "mcmc_nruns": 2, "mcmc_stop_early": True,
    }
    params = build_recompute_job_params(stored).tree_builder_params
    assert params.mcmc_stop_early is True
    assert params.mcmc_generations == 200000

    stored["mcmc_stop_early"] = False
    params = build_recompute_job_params(stored).tree_builder_params
    assert params.mcmc_stop_early is False


def test_recompute_of_a_legacy_job_does_not_invent_early_stopping():
    from app.services.tree_edit_service import build_recompute_job_params

    stored = {"tree_method": "mrbayes", "mcmc_generations": 50000, "mcmc_nruns": 2}
    params = build_recompute_job_params(stored).tree_builder_params

    assert params.mcmc_stop_early is False
    # An explicitly stored generation count survives untouched.
    assert params.mcmc_generations == 50000


def test_job_params_endpoint_reports_legacy_mrbayes_jobs_as_not_stopping_early(tmp_path):
    job_dir = tmp_path / JOB_ID
    job_dir.mkdir()
    (job_dir / "input_info.json").write_text(json.dumps({
        "tree_method": "mrbayes", "mcmc_generations": 50000, "mcmc_nruns": 2,
    }))

    app = Flask(__name__)
    with (
        app.test_request_context(method="GET"),
        patch.object(Config, "JOB_DIR", tmp_path),
        patch.object(routes, "check_job_access", return_value=(None, None, 200)),
    ):
        body = routes.get_job_pipeline_params(JOB_ID).get_json()

    assert body["params"]["mcmc_stop_early"] is False
    assert body["params"]["mcmc_generations"] == 50000


def test_job_params_endpoint_reports_a_stored_stop_early_setting(tmp_path):
    job_dir = tmp_path / JOB_ID
    job_dir.mkdir()
    (job_dir / "input_info.json").write_text(json.dumps({
        "tree_method": "mrbayes", "mcmc_generations": 1000000,
        "mcmc_nruns": 2, "mcmc_stop_early": True,
    }))

    app = Flask(__name__)
    with (
        app.test_request_context(method="GET"),
        patch.object(Config, "JOB_DIR", tmp_path),
        patch.object(routes, "check_job_access", return_value=(None, None, 200)),
    ):
        body = routes.get_job_pipeline_params(JOB_ID).get_json()

    assert body["params"]["mcmc_stop_early"] is True


def test_worker_treats_a_missing_setting_as_the_old_behaviour():
    from app.services.security_utils import coerce_bool

    # The worker's own read of a legacy job's stored params.
    assert coerce_bool({}.get("mcmc_stop_early"), False)[0] is False
    assert coerce_bool({"mcmc_stop_early": True}.get("mcmc_stop_early"), False)[0] is True


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------

def test_openapi_documents_the_new_default_and_semantics():
    from app.api_v1.openapi import _schemas

    create = _schemas()["CreateJobRequest"]["properties"]
    assert create["mcmc_generations"]["default"] == 1_000_000
    assert "maximum" in create["mcmc_generations"]["description"]

    stop_early = create["mcmc_stop_early"]
    assert stop_early["type"] == "boolean"
    assert stop_early["default"] is True
    assert "mcmc_nruns >= 2" in stop_early["description"]
    assert "stoprule=yes" in stop_early["description"]
    assert "ESS" in stop_early["description"]

    recompute = _schemas()["RecomputeRequest"]["properties"]
    assert recompute["mcmc_stop_early"]["type"] == "boolean"

    job_params = _schemas()["Job"]["properties"]["params"]["properties"]
    assert job_params["mcmc_stop_early"]["type"] == "boolean"


if __name__ == "__main__":
    pytest.main([__file__])
