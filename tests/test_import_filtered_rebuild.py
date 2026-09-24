import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask


def _call_rebuild(tmp_path: Path, filtered_records):
    from app.api import routes

    source_dir = tmp_path / "source-job"
    source_dir.mkdir()
    source_params = {
        "input_type": "inat_tree_preparation",
        "tree_method": "iqtree",
        "alignment_method": "mafft",
        "trimming_method": "trimal_gappy",
        "blast_mode": "auto",
        "sequence": ">kept_A\nAAAA\n>kept_B\nCCCC\n",
        "sequence_metadata": [
            {"name": "kept_A", "fasta_header": "kept_A"},
            {"name": "kept_B", "fasta_header": "kept_B"},
        ],
        "accessions": ["AB123456"],
        "_inat_tree_preparation": {"observation_id": 123},
        "import_filter_details": {
            "mycomap": {
                "filtered_records": filtered_records,
                "counts": {"contaminant": 1, "conflicting_local": 1},
            },
            "queue_duplicates": {"removed_count": 1, "removed_records": []},
        },
    }
    (source_dir / "input_info.json").write_text(json.dumps(source_params))
    captured = {}

    def fake_enqueue(params, *args, **kwargs):
        captured.update(params)
        captured["enqueued_job_id"] = kwargs.get("job_id")

    record = SimpleNamespace(status="queued", metrics={}, user_id=None)

    def fake_job(**kwargs):
        record.__dict__.update(kwargs)
        return record

    session = SimpleNamespace(add=Mock(), commit=Mock(), rollback=Mock())
    app = Flask(__name__)
    app.secret_key = "test"
    endpoint = routes.rebuild_with_import_filtered.__wrapped__
    with (
        app.test_request_context(method="POST", json={}),
        patch.object(routes.Config, "JOB_DIR", tmp_path),
        patch.object(
            routes,
            "check_job_access",
            return_value=(SimpleNamespace(user_id=7), None, 200),
        ),
        patch.object(routes, "prepare_phylo_job_params"),
        patch.object(routes, "enqueue_job", side_effect=fake_enqueue),
        patch.object(routes, "generate_job_id", return_value="new-job"),
        patch.object(routes, "Job", side_effect=fake_job),
        patch.object(routes, "db", SimpleNamespace(session=session)),
    ):
        response = endpoint("source-job")
    return response, captured, record, session


def test_import_filter_details_retain_restorable_sequence_and_metadata():
    from app.api.routes import _append_import_filter_detail, _normalize_import_filter_details

    rows = []
    _append_import_filter_detail(
        rows,
        name="filtered one",
        source="mycomap",
        hit_source="local",
        reason="contaminant",
        reason_label="Marked contaminant",
        record={
            "name": "raw name",
            "sequence": "AC GT\nNN",
            "identity": 97.5,
            "location": "California US",
        },
    )

    normalized = _normalize_import_filter_details({
        "mycomap": {"filtered_records": rows, "counts": {"contaminant": 1}},
    })
    restored = normalized["mycomap"]["filtered_records"][0]
    assert restored["sequence"] == "ACGTNN"
    assert restored["metadata"]["name"] == "filtered one"
    assert restored["metadata"]["identity"] == 97.5
    assert restored["metadata"]["location"] == "California US"


def test_rebuild_adds_sequence_bearing_import_filtered_records(tmp_path):
    response, captured, record, session = _call_rebuild(tmp_path, [
        {
            "name": "filtered_X contaminant",
            "sequence": "GGGG",
            "metadata": {"name": "filtered_X contaminant", "identity": 91},
        },
        {
            "name": "kept_A conflicting copy",
            "sequence": "TTTT",
            "metadata": {"name": "kept_A conflicting copy", "identity": 92},
        },
        {"name": "invalid_without_bases", "reason": "invalid_sequence"},
    ])

    body, status = response
    assert status == 202
    assert body.get_json()["restored_count"] == 2
    assert captured["sequence"].count(">") == 4
    assert ">filtered_X contaminant\nGGGG" in captured["sequence"]
    assert ">kept_A_2 conflicting copy\nTTTT" in captured["sequence"]
    assert [item.get("identity") for item in captured["sequence_metadata"]][-2:] == [91, 92]
    assert captured["skip_observation_dedup"] is True
    assert captured["preserve_exact_duplicate_records"] is True
    assert captured["input_type"] == "pasted_sequence"
    assert captured["blast_mode"] == "off"
    assert captured["accessions"] == []
    assert "_inat_tree_preparation" not in captured
    assert "mycomap" not in captured["import_filter_details"]
    assert "queue_duplicates" in captured["import_filter_details"]
    assert captured["enqueued_job_id"] == "new-job"
    assert record.user_id == 7
    assert record.metrics["restored_import_filtered_count"] == 2
    session.add.assert_called_once_with(record)


def test_older_import_filter_rows_without_sequences_are_not_claimed_restorable(tmp_path):
    response, captured, _record, session = _call_rebuild(tmp_path, [
        {"name": "legacy filtered row", "reason": "contaminant"},
    ])

    body, status = response
    assert status == 400
    assert "Older jobs recorded their names and reasons only" in body.get_json()["error"]
    assert captured == {}
    session.add.assert_not_called()

