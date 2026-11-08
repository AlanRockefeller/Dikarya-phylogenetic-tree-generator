"""New and historical RAxML jobs replay the cutoff their tree actually used."""

import gzip
import json
from unittest.mock import patch

import pytest
from flask import Flask

from app.api import routes
from app.config import Config
from app.services.raxml_validator import LEGACY_BOOTSTRAP_CUTOFF, stored_bootstrap_cutoff
from app.services.tree_edit_service import build_recompute_job_params


def write_metadata(job_dir, name, metadata, *, compressed=False):
    tree_dir = job_dir / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    path = tree_dir / name
    if compressed:
        with gzip.open(str(path) + ".gz", "wt") as handle:
            json.dump(metadata, handle)
    else:
        path.write_text(json.dumps(metadata))


@pytest.mark.parametrize("compressed", [False, True])
def test_api_job_with_omitted_key_recovers_its_recorded_cutoff(tmp_path, compressed):
    write_metadata(tmp_path, "tree_metadata.json", {
        "method": "raxml", "bootstrap_cutoff": 0.07,
    }, compressed=compressed)

    params = build_recompute_job_params({"tree_method": "raxml"}, job_dir=tmp_path)

    assert params.tree_builder_params.bootstrap_cutoff == 0.07


@pytest.mark.parametrize("metadata", [{}, {"method": "raxml"}, {
    "method": "raxml", "bootstrap_cutoff": None,
}])
def test_legacy_jobs_and_unsupported_cutoff_flags_keep_the_legacy_value(tmp_path, metadata):
    write_metadata(tmp_path, "tree_metadata.json", metadata)
    assert stored_bootstrap_cutoff({}, tmp_path) == LEGACY_BOOTSTRAP_CUTOFF
    assert build_recompute_job_params({}).tree_builder_params.bootstrap_cutoff == 0.03


def test_latest_run_and_explicit_overrides_take_precedence(tmp_path):
    write_metadata(tmp_path, "tree_metadata.json", {"method": "raxml", "bootstrap_cutoff": 0.07})
    write_metadata(tmp_path, "tree_pruned_metadata.json", {"method": "raxml", "bootstrap_cutoff": 0.04})
    assert stored_bootstrap_cutoff({}, tmp_path) == 0.04
    assert stored_bootstrap_cutoff({"bootstrap_cutoff": 0.10}, tmp_path) == 0.10


def test_unreadable_latest_metadata_falls_back_to_the_original_run(tmp_path):
    write_metadata(tmp_path, "tree_metadata.json", {"method": "raxml", "bootstrap_cutoff": 0.07})
    (tmp_path / "tree" / "tree_pruned_metadata.json").write_text("{")
    assert stored_bootstrap_cutoff({}, tmp_path) == 0.07


def test_advanced_panel_reports_the_recovered_cutoff(tmp_path):
    job_dir = tmp_path / "abcd"
    write_metadata(job_dir, "tree_metadata.json", {"method": "raxml", "bootstrap_cutoff": 0.07})
    (job_dir / "input_info.json").write_text(json.dumps({"tree_method": "raxml"}))
    app = Flask(__name__)
    with (
        app.test_request_context(),
        patch.object(Config, "JOB_DIR", tmp_path),
        patch.object(routes, "check_job_access", return_value=(None, None, None)),
    ):
        response = routes.get_job_pipeline_params("abcd")

    assert response.status_code == 200
    assert response.get_json()["params"]["bootstrap_cutoff"] == 0.07
