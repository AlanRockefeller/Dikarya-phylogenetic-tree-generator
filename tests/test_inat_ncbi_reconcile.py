"""Delayed NCBI reconciliation repairs an interrupted metadata update."""

import json

import pytest

from app.services.inaturalist_tree_service import (
    _append_fasta_to_job_input,
    _remember_reconciled_sequence_metadata,
)
from app.services.fasta_utils import MAX_SEQUENCE_LENGTH


def test_retry_repairs_metadata_for_already_appended_record(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    fasta_path = input_dir / "input_raw.fasta"
    fasta_path.write_text(">PX215422 fungus\nTTTT\n")
    info_path = tmp_path / "input_info.json"
    info_path.write_text(json.dumps({"sequence_metadata": []}))
    records = [{"name": "PX215422 fungus", "sequence": "ACGT",
                "source": "mycomap", "hit_source": "ncbi",
                "accession": "PX215422"}]

    first = []
    assert _append_fasta_to_job_input(tmp_path, "", sequence_records=records,
                                      added_records=first) == 1
    assert first[0]["name"] == "PX215422_added fungus"
    original_fasta = fasta_path.read_text()

    retry = []
    assert _append_fasta_to_job_input(tmp_path, "", sequence_records=records,
                                      added_records=retry) == 0
    assert retry[0]["name"] == "PX215422_added fungus"
    assert _remember_reconciled_sequence_metadata(tmp_path, retry) == 1
    assert _remember_reconciled_sequence_metadata(tmp_path, retry) == 0
    assert fasta_path.read_text() == original_fasta
    assert json.loads(info_path.read_text())["sequence_metadata"][0][
        "fasta_header"] == "PX215422_added fungus"


def test_delayed_ncbi_whole_genome_is_rejected_before_atomic_replace(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    fasta_path = input_dir / "input_raw.fasta"
    original = ">local_hit\nACGT\n"
    fasta_path.write_text(original)

    with pytest.raises(ValueError, match="per-sequence limit"):
        _append_fasta_to_job_input(
            tmp_path,
            "",
            sequence_records=[{
                "name": "late_genome",
                "sequence": "A" * (MAX_SEQUENCE_LENGTH + 1),
            }],
        )

    assert fasta_path.read_text() == original
