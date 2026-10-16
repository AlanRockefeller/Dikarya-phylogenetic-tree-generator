"""Exact MycoMap sequence links from tree-tip provenance."""

import json
from unittest.mock import patch

import pytest

from app.services.mycomap_sequence_link_service import (
    MycoMapSequenceLinkError,
    resolve_job_tip_url,
)


TIP = "iNat393666515 Lepiota Greenville Ohio US RiC 44"
SEQUENCE = "ACGTRYSWKMBDHVN" * 8


def _write_job(tmp_path, *, metadata=None, tip=TIP, sequence=SEQUENCE, renamed=None):
    (tmp_path / "input").mkdir()
    state = {
        "tree_structure": {
            "name": tip,
            "original_name": tip,
        },
        "renames": {tip: renamed} if renamed else {},
    }
    (tmp_path / "tree_state.json").write_text(json.dumps(state))
    row = {
        "name": tip,
        "fasta_header": tip,
        "source": "mycomap",
        "hit_source": "local",
    }
    row.update(metadata or {})
    (tmp_path / "input_info.json").write_text(json.dumps({
        "sequence": f">{tip}\n{sequence}\n",
        "sequence_metadata": [row],
    }))
    (tmp_path / "input" / "input_raw.fasta").write_text(f">{tip}\n{sequence}\n")


def test_saved_sequence_id_resolves_without_remote_lookup(tmp_path):
    _write_job(tmp_path, metadata={"mycomap_sequence_id": 842537})
    with patch("app.services.mycomap_org_service.observation_sequences",
               side_effect=AssertionError("saved IDs must not need a lookup")):
        assert resolve_job_tip_url(tmp_path, TIP) == "https://mycomap.org/sequences/842537"


def test_old_job_matches_exact_dna_and_ric_among_observation_records(tmp_path):
    _write_job(tmp_path, renamed="Boletus edulis from Ohio")
    rows = [
        {"id": "842538", "title": "same observation ric=18", "sequence": SEQUENCE},
        {"id": "842537", "title": "same observation ric=44", "sequence": SEQUENCE},
        {"id": "842536", "title": "different read ric=3", "sequence": "T" * 120},
    ]
    with patch("app.services.mycomap_org_service.observation_sequences",
               return_value=rows) as lookup:
        assert resolve_job_tip_url(tmp_path, "Boletus edulis from Ohio") == (
            "https://mycomap.org/sequences/842537"
        )
    lookup.assert_called_once_with("inat:393666515")


def test_reverse_oriented_tree_input_still_matches_source_record(tmp_path):
    source = "AACCGGRYTT" * 12
    reverse = source.translate(str.maketrans(
        "ACGTRYSWKMBDHVN", "TGCAYRSWMKVHDBN"
    ))[::-1]
    _write_job(tmp_path, sequence=reverse)
    with patch("app.services.mycomap_org_service.observation_sequences", return_value=[
        {"id": "99", "title": "record ric=44", "sequence": source},
    ]):
        assert resolve_job_tip_url(tmp_path, TIP) == "https://mycomap.org/sequences/99"


def test_non_mycomap_tip_is_rejected_before_remote_lookup(tmp_path):
    _write_job(tmp_path, metadata={"source": "manual", "hit_source": ""})
    with patch("app.services.mycomap_org_service.observation_sequences") as lookup:
        with pytest.raises(MycoMapSequenceLinkError, match="not a MycoMap sequence") as caught:
            resolve_job_tip_url(tmp_path, TIP)
    assert caught.value.status == 422
    lookup.assert_not_called()


def test_ambiguous_identical_records_without_ric_match_are_not_guessed(tmp_path):
    tip = "iNat393666515 Lepiota Greenville Ohio US"
    _write_job(tmp_path, tip=tip)
    with patch("app.services.mycomap_org_service.observation_sequences", return_value=[
        {"id": "1", "title": "read one", "sequence": SEQUENCE},
        {"id": "2", "title": "read two", "sequence": SEQUENCE},
    ]):
        with pytest.raises(MycoMapSequenceLinkError, match="more than one") as caught:
            resolve_job_tip_url(tmp_path, tip)
    assert caught.value.status == 409
