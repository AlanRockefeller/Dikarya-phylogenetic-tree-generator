"""Existing-tree additions obey the same size ceilings as new jobs."""

from unittest.mock import patch

from flask import Flask

from app.api import routes
from app.config import Config
from app.services.fasta_utils import MAX_SEQUENCE_LENGTH, MAX_TOTAL_BASES


def _client(job_root):
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(routes.bp, url_prefix="/api")
    return app.test_client()


def test_pasted_whole_genome_is_rejected_before_existing_job_is_changed(tmp_path):
    job_id = "wholegenome"
    input_dir = tmp_path / job_id / "input"
    input_dir.mkdir(parents=True)
    input_path = input_dir / "input_raw.fasta"
    original = ">existing\nACGT\n"
    input_path.write_text(original)

    with (
        patch.object(Config, "JOB_DIR", tmp_path),
        patch("app.api.routes.check_job_access", return_value=(None, None, None)),
    ):
        response = _client(tmp_path).post(
            f"/api/job/{job_id}/sequences/add",
            json={"input": ">genome\n" + "A" * (MAX_SEQUENCE_LENGTH + 1)},
        )

    assert response.status_code == 400
    assert f"{MAX_SEQUENCE_LENGTH:,} bp per-sequence limit" in response.json["error"]
    assert input_path.read_text() == original


def test_repeated_additions_cannot_cross_the_submission_total(tmp_path):
    job_id = "cumulative"
    input_dir = tmp_path / job_id / "input"
    input_dir.mkdir(parents=True)
    input_path = input_dir / "input_raw.fasta"

    remaining = MAX_TOTAL_BASES - 10
    records = []
    index = 0
    while remaining:
        length = min(MAX_SEQUENCE_LENGTH, remaining)
        records.append(f">existing_{index}\n{'A' * length}\n")
        remaining -= length
        index += 1
    original = "".join(records)
    input_path.write_text(original)

    with (
        patch.object(Config, "JOB_DIR", tmp_path),
        patch("app.api.routes.check_job_access", return_value=(None, None, None)),
    ):
        response = _client(tmp_path).post(
            f"/api/job/{job_id}/sequences/add",
            json={"input": ">one_more\n" + "C" * 20},
        )

    assert response.status_code == 400
    assert f"{MAX_TOTAL_BASES:,} base limit" in response.json["error"]
    assert input_path.read_text() == original
