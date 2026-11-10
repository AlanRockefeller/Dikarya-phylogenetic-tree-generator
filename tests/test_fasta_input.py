"""Submission failures stay actionable, reproducible, and non-destructive."""
import gzip
import json
from unittest.mock import Mock

import pytest
from flask import Flask, g

from app.services import fasta_input_service as inputs
from app.services.fasta_utils import analyze_dna_fasta, validate_dna_fasta, FastaValidationError


def undecorated(function):
    while hasattr(function, "__wrapped__"):
        function = function.__wrapped__
    return function


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(inputs, "ARCHIVE_DIR", tmp_path / "fasta-rejections")
    return inputs.ARCHIVE_DIR


def snapshots(directory):
    return [json.loads(gzip.decompress(path.read_bytes())) for path in directory.glob("*/*.gz")]


def test_issues_name_every_bad_record_and_original_position():
    raw = ">one\n A C*G\n>two\n\n>\nTTZ\n"
    result = analyze_dna_fasta(raw)
    assert result["record_count"] == 3
    assert [issue["code"] for issue in result["issues"]] == [
        "invalid_symbols", "empty_sequence", "empty_header", "invalid_symbols",
    ]
    first = result["issues"][0]
    assert (first["line"], first["column"], first["sequence_position"]) == (2, 5, 3)
    assert first["record_name"] == "one"
    assert first["character"] == "*"
    assert first["character_code"] == "U+002A"


def test_preamble_is_not_silently_discarded():
    with pytest.raises(FastaValidationError) as caught:
        validate_dna_fasta("copied extra text\n>one\nACGT\n")
    assert caught.value.analysis["issues"][0]["code"] == "text_before_header"
    assert caught.value.analysis["issues"][0]["line"] == 1


def test_supported_symbols_and_whitespace_stay_valid():
    assert validate_dna_fasta(">sample name\r\nacg tu\tRYSWKMBDHVN-?\r\n") == 1
    assert not analyze_dna_fasta(">sample\nACGß\n")["valid"]


@pytest.mark.parametrize("raw,expected", [
    ("\ufeff>one\r\n1 ACGT 4\r\n5 ttaa 8\r\n", ">one\nACGT\nttaa\n"),
    ("AC GT\nTTAA", ">sequence_1\nACGTTTAA\n"),
    (">\nACGT\n", ">sequence_1\nACGT\n"),
    (">sequence_2\nACGT\n>\nTGCA\n", ">sequence_2\nACGT\n>sequence_2_unnamed\nTGCA\n"),
])
def test_explicit_formatting_proposal_preserves_the_bases(raw, expected):
    proposal = inputs.propose_fasta_repair(raw)
    assert proposal["sequence"] == expected
    assert proposal["valid"]
    assert raw != expected


@pytest.mark.parametrize("raw", [
    ">one\nACGT!\n", ">one\nAC.GT\n", ">one\nAC12GT\n",
    ">one\nACGT\n>empty\n", "prose\n>one\nACGT\n",
])
def test_repairs_never_drop_unknown_symbols_empty_records_or_prose(raw):
    proposal = inputs.propose_fasta_repair(raw)
    assert proposal is None or not proposal["valid"]


def test_full_rejected_submission_context_and_exact_reason_are_archived(archive):
    raw = ">long header, sample 4\r\n" + "ACGT" * 10000 + "!\x00\n"
    app = Flask(__name__)
    with app.test_request_context("/api/fasta/check", headers={"User-Agent": "FASTA-test"}):
        g.request_id = "fasta-request"
        with pytest.raises(FastaValidationError) as caught:
            inputs.validate_submitted_fasta(raw, source="browser_upload", filename="original.fa")
    snapshot = snapshots(archive)[0]
    assert snapshot["sequence"] == raw
    assert snapshot["filename"] == "original.fa"
    assert snapshot["context"]["path"] == "/api/fasta/check"
    assert snapshot["context"]["req"] == "fasta-request"
    assert snapshot["id"] == caught.value.diagnostic_id
    assert snapshot["analysis"]["issues"][0]["column"] == 40001
    assert snapshot["analysis"]["issues"][0]["invalid_count"] == 2


def test_archive_failure_does_not_hide_the_input_error(archive, monkeypatch):
    monkeypatch.setattr(inputs.os, "open", Mock(side_effect=PermissionError("blocked")))
    with pytest.raises(FastaValidationError) as caught:
        inputs.validate_submitted_fasta(">one\nACGT!\n", source="job_submission")
    assert "invalid DNA" in str(caught.value)
    assert caught.value.diagnostic_id is None


def test_displayed_issue_limit_does_not_truncate_retained_input(archive):
    raw = "".join(f">sample_{n}\nAC!\n" for n in range(105))
    analysis = analyze_dna_fasta(raw)
    assert analysis["issue_count"] == 105
    assert len(analysis["issues"]) == 100
    inputs.record_fasta_rejection(raw, analysis, source="browser_paste")
    assert snapshots(archive)[0]["sequence"] == raw


def test_checker_returns_issues_and_reviewable_repair_without_queueing(archive):
    from app.api import routes
    app = Flask(__name__)
    raw = ">one\n1 ACGT 4\n"
    with app.test_request_context("/api/fasta/check", method="POST", json={"sequence": raw}):
        response = undecorated(routes.check_fasta_input)()
    result = response.get_json()
    assert result["valid"] is False
    assert result["repair"]["valid"] is True
    assert result["repair"]["sequence"] == ">one\nACGT\n"
    assert snapshots(archive)[0]["sequence"] == raw
    assert result["diagnostic_id"] == snapshots(archive)[0]["id"]


def test_submission_is_rejected_before_dedup_can_remove_the_empty_record(archive, monkeypatch):
    from app.api import routes
    dedupe = Mock(side_effect=AssertionError("must validate the original first"))
    monkeypatch.setattr(routes, "_dedupe_sequence_payload", dedupe)
    raw = ">good\nACGT\n>empty\n"
    app = Flask(__name__)
    with app.test_request_context("/api/job", method="POST", json={
        "input_type": "sequence", "sequence": raw, "tree_method": "nj",
    }):
        response = app.make_response(undecorated(routes.create_job)())
    assert response.status_code == 400
    assert response.get_json()["fasta_validation"]["issues"][0]["code"] == "empty_sequence"
    assert snapshots(archive)[0]["sequence"] == raw
    dedupe.assert_not_called()


def test_v1_retains_original_lines_and_exposes_the_diagnostic_reference(archive):
    from app.api_v1 import routes
    app = Flask(__name__)
    raw = "\n\n>one\nACGT!\n"
    with app.test_request_context("/api/v1/jobs", method="POST", json={
        "input_type": "pasted_sequence", "sequence": raw,
    }):
        response = app.make_response(undecorated(routes.create_job)())
    assert response.status_code == 422
    result = response.get_json()["error"]["details"]
    assert result["fasta_validation"]["issues"][0]["line"] == 4
    assert result["diagnostic_id"] == snapshots(archive)[0]["id"]
    assert snapshots(archive)[0]["sequence"] == raw


def test_existing_tree_rejects_bad_fasta_before_writing_and_archives_it(archive, tmp_path, monkeypatch):
    from app.api import routes
    from app.config import Config
    job_dir = tmp_path / "jobs" / "abcd" / "input"
    job_dir.mkdir(parents=True)
    original = ">existing\nACGT\n"
    (job_dir / "input_raw.fasta").write_text(original)
    monkeypatch.setattr(Config, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(routes, "check_job_access", lambda *a, **kw: (None, None, None))
    raw = ">new\nACGT!\n>empty\n"
    app = Flask(__name__)
    with app.test_request_context("/api/job/abcd/sequences/add", method="POST", json={"input": raw}):
        response = app.make_response(routes.add_sequences_to_job("abcd"))
    assert response.status_code == 400
    assert (job_dir / "input_raw.fasta").read_text() == original
    assert snapshots(archive)[0]["sequence"] == raw
