import gzip
from pathlib import Path


def test_install_recompute_outputs_replaces_complete_mrbayes_generation(tmp_path):
    from app.services.tree_edit_service import _install_recompute_outputs

    job_dir = tmp_path / "job"
    output_dir = tmp_path / "staged"
    for root in (job_dir, output_dir):
        (root / "alignment").mkdir(parents=True)
        (root / "tree").mkdir()

    required = (
        "alignment/alignment_pruned.fasta",
        "alignment/alignment_pruned_aligned.fasta",
        "alignment/alignment_pruned_trimmed.fasta",
        "tree/tree_pruned.newick",
        "tree/tree_pruned.nexus",
        "tree/tree_pruned_metadata.json",
    )
    for relative in required:
        path = output_dir / relative
        path.write_text("new")

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


def test_install_recompute_outputs_installs_compressed_trim_report(tmp_path):
    from app.services.tree_edit_service import _install_recompute_outputs

    job_dir = tmp_path / "job"
    output_dir = tmp_path / "staged"
    for root in (job_dir, output_dir):
        (root / "alignment").mkdir(parents=True)
        (root / "tree").mkdir()

    required = (
        "alignment/alignment_pruned.fasta",
        "alignment/alignment_pruned_aligned.fasta",
        "alignment/alignment_pruned_trimmed.fasta",
        "tree/tree_pruned.newick",
        "tree/tree_pruned.nexus",
        "tree/tree_pruned_metadata.json",
    )
    for relative in required:
        (output_dir / relative).write_text("new")

    report = Path("alignment/alignment_pruned_trimmed_report.html")
    (job_dir / report).write_text("stale")
    with gzip.open(output_dir / f"{report}.gz", "wb") as handle:
        handle.write(b"new compressed report")

    _install_recompute_outputs(job_dir, output_dir)

    assert not (job_dir / report).exists()
    with gzip.open(job_dir / f"{report}.gz", "rb") as handle:
        assert handle.read() == b"new compressed report"


def test_install_recompute_outputs_removes_stale_report_when_none_was_produced(tmp_path):
    from app.services.tree_edit_service import _install_recompute_outputs

    job_dir = tmp_path / "job"
    output_dir = tmp_path / "staged"
    for root in (job_dir, output_dir):
        (root / "alignment").mkdir(parents=True)
        (root / "tree").mkdir()

    required = (
        "alignment/alignment_pruned.fasta",
        "alignment/alignment_pruned_aligned.fasta",
        "alignment/alignment_pruned_trimmed.fasta",
        "tree/tree_pruned.newick",
        "tree/tree_pruned.nexus",
        "tree/tree_pruned_metadata.json",
    )
    for relative in required:
        (output_dir / relative).write_text("new")

    report = Path("alignment/alignment_pruned_trimmed_report.html")
    with gzip.open(job_dir / f"{report}.gz", "wb") as handle:
        handle.write(b"stale")

    _install_recompute_outputs(job_dir, output_dir)

    assert not (job_dir / report).exists()
    assert not (job_dir / f"{report}.gz").exists()
