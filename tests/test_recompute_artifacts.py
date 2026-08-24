import gzip
import shutil
from pathlib import Path

import pytest

from app.models import (
    AlignmentParams, JobParams, TreeBuilderParams, TrimmingParams,
)

# What _install_recompute_outputs() refuses to install without. Declared once:
# the tuple used to be repeated in three tests, so a change to the contract
# needed three edits and could be applied to only some of them.
REQUIRED_OUTPUTS = (
    "alignment/alignment_pruned.fasta",
    "alignment/alignment_pruned_aligned.fasta",
    "alignment/alignment_pruned_trimmed.fasta",
    "tree/tree_pruned.newick",
    "tree/tree_pruned.nexus",
    "tree/tree_pruned_metadata.json",
)


@pytest.fixture
def recompute_dirs(tmp_path):
    """A job dir plus a staged output dir holding every required output."""
    job_dir = tmp_path / "job"
    output_dir = tmp_path / "staged"
    for root in (job_dir, output_dir):
        (root / "alignment").mkdir(parents=True)
        (root / "tree").mkdir()
    for relative in REQUIRED_OUTPUTS:
        (output_dir / relative).write_text("new")
    return job_dir, output_dir


def test_install_recompute_outputs_replaces_complete_mrbayes_generation(recompute_dirs):
    from app.services.tree_edit_service import _install_recompute_outputs

    job_dir, output_dir = recompute_dirs

    staged_names = (
        "mrbayes_input.nex", "mrbayes_input.nex.p", "mrbayes_input.nex.t",
        "mrbayes_input.nex.pstat", "mrbayes_input.nex.tstat",
        "mrbayes_input.nex.run1.p", "mrbayes_input.nex.run1.t",
        "mrbayes_input.nex.con.tre",
    )
    for name in staged_names:
        (output_dir / "tree" / name).write_text(f"new:{name}")
    (job_dir / "tree" / "mrbayes_input.nex.run2.t").write_text("stale")

    _install_recompute_outputs(job_dir, output_dir)

    for name in staged_names:
        assert (job_dir / "tree" / name).read_text() == f"new:{name}"
    assert not (job_dir / "tree" / "mrbayes_input.nex.run2.t").exists()


def test_install_recompute_outputs_installs_compressed_trim_report(recompute_dirs):
    from app.services.tree_edit_service import _install_recompute_outputs

    job_dir, output_dir = recompute_dirs

    report = Path("alignment/alignment_pruned_trimmed_report.html")
    (job_dir / report).write_text("stale")
    with gzip.open(output_dir / f"{report}.gz", "wb") as handle:
        handle.write(b"new compressed report")

    _install_recompute_outputs(job_dir, output_dir)

    assert not (job_dir / report).exists()
    with gzip.open(job_dir / f"{report}.gz", "rb") as handle:
        assert handle.read() == b"new compressed report"


def test_install_recompute_outputs_removes_stale_report_when_none_was_produced(recompute_dirs):
    from app.services.tree_edit_service import _install_recompute_outputs

    job_dir, output_dir = recompute_dirs

    # Both stale forms, because resolve_artifact() prefers the plain file: a
    # recompute that produced no report must clear the archive *and* the plain
    # copy, or the viewer keeps serving the previous run's report. Only the .gz
    # used to be created here, so the plain-file assertion held for a file that
    # had never existed and proved no removal at all.
    report = Path("alignment/alignment_pruned_trimmed_report.html")
    (job_dir / report).write_text("stale plain report")
    with gzip.open(job_dir / f"{report}.gz", "wb") as handle:
        handle.write(b"stale")

    _install_recompute_outputs(job_dir, output_dir)

    assert not (job_dir / report).exists()
    assert not (job_dir / f"{report}.gz").exists()


def test_malformed_staged_recompute_does_not_replace_live_tree(tmp_path, monkeypatch):
    from app.services import (
        alignment_service, tree_builder_service, tree_edit_service,
        trimming_service,
    )

    job_dir = tmp_path / "job"
    output_dir = tmp_path / "staged"
    for root in (job_dir, output_dir):
        (root / "alignment").mkdir(parents=True)
        (root / "tree").mkdir()
    (job_dir / "input").mkdir()
    (job_dir / "input" / "input_raw.fasta").write_text(
        ">A\nACGT\n>B\nACGA\n"
    )
    live_newick = job_dir / "tree" / "tree_pruned.newick"
    live_nexus = job_dir / "tree" / "tree_pruned.nexus"
    live_newick.write_text("(A:0.1,B:0.1);\n")
    live_nexus.write_text("last usable nexus\n")

    monkeypatch.setattr(
        tree_edit_service, "load_tree_state",
        lambda _job_dir: {"pruned_taxa": [], "structure": {}},
    )
    monkeypatch.setattr(
        alignment_service, "run_alignment",
        lambda source, destination, *args, **kwargs: shutil.copy(source, destination),
    )

    def fake_trim(source, destination, *args, **kwargs):
        shutil.copy(source, destination)
        return {"terminal_overhang_trim": {"enabled": False}}

    monkeypatch.setattr(trimming_service, "run_trimming", fake_trim)

    def malformed_tree(_alignment, newick, nexus, *args, **kwargs):
        newick.write_text("not a tree\n")
        nexus.write_text("#NEXUS\n")
        return {"method": "nj"}

    monkeypatch.setattr(tree_builder_service, "run_tree_builder", malformed_tree)
    installed = []
    monkeypatch.setattr(
        tree_edit_service, "_install_recompute_outputs",
        lambda *args: installed.append(args),
    )

    params = JobParams(
        input_type="fasta",
        alignment_params=AlignmentParams(method="default"),
        trimming_params=TrimmingParams(
            method="none", trim_terminal_overhangs=False,
        ),
        tree_builder_params=TreeBuilderParams(method="nj"),
    )
    with pytest.raises(RuntimeError, match="Pipeline output validation failed"):
        tree_edit_service._recompute_tree_staged(
            job_dir, params, object(), tree_edit_service.logger,
            output_dir=output_dir,
        )

    assert installed == []
    assert live_newick.read_text() == "(A:0.1,B:0.1);\n"
    assert live_nexus.read_text() == "last usable nexus\n"
