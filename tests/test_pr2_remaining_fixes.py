"""Regression coverage for the remaining PR #2 audit findings."""

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rq.timeouts import JobTimeoutException

from app.services import orientation_service, subprocess_utils
from app.services.fasta_utils import validate_dna_fasta
from app.services.security_utils import validate_job_id
from app.workers import tasks


def test_rq_timeout_propagates_and_reaps_non_streaming_child(monkeypatch):
    popen = subprocess.Popen
    children = []
    timeout = JobTimeoutException("job budget expired")

    def start_child(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        child.communicate = Mock(side_effect=timeout)
        return child

    monkeypatch.setattr(subprocess_utils.subprocess, "Popen", start_child)
    with pytest.raises(JobTimeoutException) as caught:
        subprocess_utils.run_command([sys.executable, "-c", "import time; time.sleep(30)"])

    assert caught.value is timeout
    assert len(children) == 1
    assert children[0].poll() is not None


def test_accepted_uracil_complements_and_matches_thymine_motifs():
    assert validate_dna_fasta(">rna\n" + "ACGU" * 30 + "\n") == 1
    assert orientation_service.revcomp("ACGUacgu") == "acgtACGT"
    assert orientation_service.revcomp("UUUU") == "AAAA"
    for sequence in ("UUUU", "uuuu"):
        hit = orientation_service.best_hit(sequence, "TTTT", max_mm=0)
        assert hit is not None
        assert (hit.mism, hit.pos) == (0, 0)


@pytest.mark.parametrize("job_id", ["/tmp/escaped", "../escaped", "a/../../escaped", "abcd\n", "", None])
@pytest.mark.parametrize("entrypoint", ["recompute", "initial"])
def test_workers_reject_invalid_ids_before_creating_artifacts(monkeypatch, tmp_path, job_id, entrypoint):
    job_root = tmp_path / "jobs"
    monkeypatch.setattr(tasks.Config, "JOB_DIR", job_root)
    monkeypatch.setattr(tasks, "get_current_job", lambda: SimpleNamespace(id=job_id))
    with pytest.raises(ValueError, match="Invalid job ID format"):
        if entrypoint == "recompute":
            tasks.run_recompute_job(job_id, {})
        else:
            tasks.run_phylo_job({})
    assert not job_root.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("job_id", ["fu81", "abc123xyz", "12345678-abcd-4abc-8abc-123456789abc"])
def test_worker_id_validation_preserves_supported_ids(job_id):
    assert validate_job_id(job_id)
