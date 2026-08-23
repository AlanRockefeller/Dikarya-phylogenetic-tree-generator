import ast
import logging
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from Bio import AlignIO, Phylo
from Bio.Align import MultipleSeqAlignment
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord

from app.models import AlignmentParams, JobParams, TreeBuilderParams
from app.services import alignment_service, tree_builder_service, trimming_service
from app.services.tree_parameter_validation import validate_iqtree_ufboot_count


LOGGER = logging.getLogger("tests.phylogeny_second_pass")


# MAFFT's own limit, asserted both for a MAFFT alignment and for the direction
# pre-pass that now precedes every other aligner.
MAFFT_LIMIT_HOURS = 1.25

def _config(**overrides):
    values = {
        "MAFFT_BINARY": "mafft",
        "MUSCLE_BINARY": "muscle",
        "CLUSTALO_BINARY": "clustalo",
        "IQTREE_BINARY": "iqtree2",
        "RAXML_BINARY": "raxml-ng",
        "TRIMAL_BINARY": "trimal",
        "BMGE_BINARY": "BMGE.jar",
        "MAFFT_TIME_LIMIT_HOURS": MAFFT_LIMIT_HOURS,
        "MUSCLE_TIME_LIMIT_HOURS": 1.5,
        "CLUSTALO_TIME_LIMIT_HOURS": 1.75,
        "IQTREE_ALIGNMENT_TIME_LIMIT_HOURS": 2,
        "TRIMAL_TIME_LIMIT_HOURS": 2.25,
        "BMGE_TIME_LIMIT_HOURS": 2.5,
        "IQTREE_TIME_LIMIT_HOURS": 3,
        "RAXML_TIME_LIMIT_HOURS": 3,
        "SUBPROCESS_MEMORY_LIMIT_MB": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _write_alignment(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(">A\nACGTACGT\n>B\nACGTACGA\n")


def _write_amino_acid_alignment(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(">A\nMPEPTIDE\n>B\nMPEPTIDQ\n")


def test_alignment_and_trimming_services_have_no_unbounded_external_call_sites():
    """A newly added runner must opt into the shared limit at its actual call site."""
    for module in (alignment_service, trimming_service):
        source_path = Path(module.__file__)
        tree = ast.parse(source_path.read_text())
        unbounded = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in {"run_command", "run_command_streaming"}:
                continue
            has_timeout = any(keyword.arg == "timeout" for keyword in node.keywords)
            has_shared_limits = any(
                keyword.arg is None
                and isinstance(keyword.value, ast.Call)
                and isinstance(keyword.value.func, ast.Name)
                and keyword.value.func.id == "configured_tool_limits"
                for keyword in node.keywords
            )
            if not (has_timeout or has_shared_limits):
                unbounded.append(node.lineno)
        assert not unbounded, f"{source_path.name} has unbounded call(s) at {unbounded}"


@pytest.mark.parametrize(
    ("method", "tool", "hours"),
    [
        ("mafft", "MAFFT", MAFFT_LIMIT_HOURS),
        ("muscle", "MUSCLE", 1.5),
        ("clustalo", "Clustal Omega", 1.75),
        ("iqtree_builtin", "IQ-TREE alignment", 2),
    ],
)
@pytest.mark.parametrize("streaming", [False, True])
def test_every_aligner_call_site_passes_a_wall_clock_limit(
    tmp_path, monkeypatch, method, tool, hours, streaming
):
    input_path = tmp_path / "input" / "input.fasta"
    output_path = tmp_path / "alignment" / "alignment_raw.fasta"
    _write_alignment(input_path)
    output_path.parent.mkdir()
    calls = []

    def make_output(cmd, stdout_path=None):
        fasta = input_path.read_text()
        if stdout_path is not None:
            Path(stdout_path).write_text(fasta)
        elif "-output" in cmd:
            Path(cmd[cmd.index("-output") + 1]).write_text(fasta)
        elif "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_text(fasta)
        elif "-pre" in cmd:
            Path(cmd[cmd.index("-pre") + 1] + ".fasta").write_text(fasta)

    if streaming:
        def fake_stream(cmd, **kwargs):
            calls.append(kwargs)
            make_output(cmd, kwargs.get("stdout_path"))
            return 0, {}

        monkeypatch.setattr(alignment_service, "run_command_streaming", fake_stream)
        monkeypatch.setattr("app.workers.events.publish_command", lambda *a, **k: None)
        job_id = "job"
    else:
        def fake_run(cmd, **kwargs):
            calls.append(kwargs)
            make_output(cmd)
            stdout = input_path.read_text() if method == "mafft" else ""
            return 0, stdout, ""

        monkeypatch.setattr(alignment_service, "run_command", fake_run)
        job_id = None

    alignment_service.run_alignment(
        input_path, output_path, AlignmentParams(method=method), _config(), LOGGER, job_id
    )

    # The direction pre-pass adds a MAFFT invocation ahead of every
    # direction-blind aligner, so the chosen aligner is the *last* call, not the
    # first. Both still have to carry a wall-clock limit, which is what this
    # test exists to guarantee -- so check the pre-pass too rather than
    # skipping past it.
    if method != "mafft":
        assert len(calls) == 2, f"expected direction pre-pass + aligner, got {len(calls)}"
        assert calls[0]["timeout"] == int(MAFFT_LIMIT_HOURS * 3600)
    aligner_call = calls[-1]
    assert aligner_call["timeout"] == int(hours * 3600)
    if streaming:
        assert aligner_call["cpu_limit_seconds"] >= aligner_call["timeout"]


@pytest.mark.parametrize(
    ("runner_name", "tool", "hours"),
    [
        ("_run_trimal_gappy", "trimAl", 2.25),
        ("_run_trimal", "trimAl", 2.25),
        ("_run_bmge", "BMGE", 2.5),
    ],
)
@pytest.mark.parametrize("streaming", [False, True])
def test_every_trimmer_call_site_passes_a_wall_clock_limit(
    tmp_path, monkeypatch, runner_name, tool, hours, streaming
):
    input_path = tmp_path / "alignment" / "input.fasta"
    output_path = tmp_path / "alignment" / "output.fasta"
    report_path = tmp_path / "alignment" / "report.html"
    _write_alignment(input_path)
    calls = []

    def make_output(cmd):
        output_path.write_text(input_path.read_text())
        report_path.write_text("report")

    if streaming:
        def fake_stream(cmd, **kwargs):
            calls.append(kwargs)
            make_output(cmd)
            return 0, {}

        monkeypatch.setattr(trimming_service, "run_command_streaming", fake_stream)
        monkeypatch.setattr("app.workers.events.publish_command", lambda *a, **k: None)
        job_id = "job"
    else:
        def fake_run(cmd, **kwargs):
            calls.append(kwargs)
            make_output(cmd)
            return 0, "", ""

        monkeypatch.setattr(trimming_service, "run_command", fake_run)
        job_id = None

    runner = getattr(trimming_service, runner_name)
    runner(input_path, output_path, report_path, _config(), LOGGER, job_id)

    assert calls[0]["timeout"] == int(hours * 3600)
    if streaming:
        assert calls[0]["cpu_limit_seconds"] >= calls[0]["timeout"]


def test_trimal_gappy_command_and_documented_semantics_cannot_drift(tmp_path, monkeypatch):
    input_path = tmp_path / "alignment" / "input.fasta"
    output_path = tmp_path / "alignment" / "output.fasta"
    report_path = tmp_path / "alignment" / "report.html"
    _write_alignment(input_path)
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        output_path.write_text(input_path.read_text())
        return 0, "", ""

    monkeypatch.setattr(trimming_service, "run_command", fake_run)
    trimming_service._run_trimal_gappy(
        input_path, output_path, report_path, _config(), LOGGER
    )

    cmd = captured[0]
    assert cmd[cmd.index("-gt") + 1] == "0.1"
    assert trimming_service.TRIMAL_GAP_THRESHOLD == 0.1


def test_default_trimal_path_applies_terminal_coverage_before_gap_threshold(
    tmp_path, monkeypatch
):
    input_path = tmp_path / "alignment" / "input.fasta"
    output_path = tmp_path / "alignment" / "output.fasta"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("".join(
        f">s{index}\n{'A' if index < 2 else 'N'}ACGT\n"
        for index in range(10)
    ))
    captured_input = []

    def fake_trimal(tool_input, output, report, config, logger, job_id):
        captured_input.append(tool_input)
        output.write_text(tool_input.read_text())

    monkeypatch.setattr(trimming_service, "_run_trimal_gappy", fake_trimal)
    stats = trimming_service.run_trimming(
        input_path, output_path, "trimal_gappy", _config(), LOGGER,
        trim_terminal_overhangs=True,
    )

    assert captured_input[0] != input_path
    assert not captured_input[0].exists()  # intermediate is cleaned after trimAl
    assert stats["terminal_overhang_trim"]["enabled"] is True
    assert stats["terminal_overhang_trim"]["left_removed"] == 1
    records = list(AlignIO.read(str(output_path), "fasta"))
    assert {str(record.seq) for record in records} == {"ACGT"}


def test_terminal_n_padding_does_not_define_alignment_boundaries(tmp_path):
    input_path = tmp_path / "n_padded.fasta"
    output_path = tmp_path / "trimmed.fasta"
    input_path.write_text(
        ">s1\nNNACGTNN\n>s2\n--ACGT--\n>s3\n..ACGT..\n>s4\n~~ACGT~~\n"
    )

    stats = trimming_service._trim_terminal_overhangs(input_path, output_path, LOGGER)

    assert stats["left_removed"] == 2
    assert stats["right_removed"] == 2
    assert {str(record.seq) for record in AlignIO.read(output_path, "fasta")} == {"ACGT"}


def _split(tree):
    return {
        frozenset(tip.name for tip in clade.get_terminals())
        for clade in tree.get_nonterminals()
        if 1 < len(clade.get_terminals()) < len(tree.get_terminals())
    }


@pytest.mark.parametrize(
    ("bootstrap", "alrt"),
    [(2000, 1000), (1000, 0), (1000.0, 0), (0, 1000), (0, 0)],
)
def test_iqtree_primary_is_always_ml_tree(tmp_path, monkeypatch, bootstrap, alrt):
    alignment = tmp_path / "alignment" / "alignment.fasta"
    output = tmp_path / "tree" / "tree_original.newick"
    nexus = tmp_path / "tree" / "tree_original.nexus"
    _write_alignment(alignment)
    with alignment.open("a") as handle:
        handle.write(">C\nACGTACGG\n")
    output.parent.mkdir()
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        prefix = cmd[cmd.index("-pre") + 1]
        records = list(AlignIO.read(cmd[cmd.index("-s") + 1], "fasta"))
        first, second, third = (record.id for record in records)
        Path(prefix + ".treefile").write_text(
            f"(({first}:0.1,{second}:0.1):0.2,{third}:0.3);\n"
        )
        Path(prefix + ".contree").write_text(
            f"(({first}:0.1,{third}:0.1):0.2,{second}:0.3);\n"
        )
        Path(prefix + ".iqtree").write_text("Best-fit model according to BIC: GTR+G\n")
        return 0, "", ""

    monkeypatch.setattr(tree_builder_service, "run_command", fake_run)
    params = TreeBuilderParams(
        method="iqtree", bootstrap=bootstrap, alrt_replicates=alrt, model="GTR+G"
    )
    tree_builder_service._run_iqtree(
        alignment, output, nexus, params, _config(), LOGGER
    )

    delivered = Phylo.read(str(output), "newick")
    assert frozenset({"A", "B"}) in _split(delivered)
    assert frozenset({"A", "C"}) not in _split(delivered)
    assert ("-B" in commands[0]) is (bootstrap > 0)
    if bootstrap > 0:
        assert commands[0][commands[0].index("-B") + 1] == str(int(bootstrap))
    assert ("-alrt" in commands[0]) is (alrt > 0)


@pytest.mark.parametrize("bootstrap", [1, 999, "many", 1000.5])
def test_iqtree_rejects_invalid_ufboot_before_command_construction(
    tmp_path, monkeypatch, bootstrap
):
    called = []
    monkeypatch.setattr(
        tree_builder_service, "run_command", lambda *args, **kwargs: called.append(args)
    )
    with pytest.raises(ValueError, match="integer|at least 1000"):
        tree_builder_service._run_iqtree(
            tmp_path / "alignment.fasta",
            tmp_path / "tree.newick",
            tmp_path / "tree.nexus",
            TreeBuilderParams(method="iqtree", bootstrap=bootstrap),
            _config(), LOGGER,
        )
    assert called == []


@pytest.mark.parametrize(
    ("bootstrap", "expected"),
    [(0, 0), (1000, 1000), (2000, 2000), ("1000", 1000), (1000.0, 1000)],
)
def test_iqtree_ufboot_validator_returns_exact_integer(bootstrap, expected):
    result = validate_iqtree_ufboot_count("iqtree", bootstrap)
    assert result == expected
    assert type(result) is int


@pytest.mark.parametrize("bootstrap", [1, 999, "many", 1000.5])
def test_iqtree_ufboot_validator_rejects_invalid_counts(bootstrap):
    with pytest.raises(ValueError, match="integer|at least 1000"):
        validate_iqtree_ufboot_count("iqtree", bootstrap)


def test_iqtree_ufboot_validator_does_not_change_raxml_semantics():
    assert validate_iqtree_ufboot_count("raxml", 999.5) == 999.5


def test_iqtree_missing_ml_tree_is_not_replaced_by_consensus(tmp_path, monkeypatch):
    alignment = tmp_path / "alignment" / "alignment.fasta"
    output = tmp_path / "tree" / "tree_original.newick"
    nexus = tmp_path / "tree" / "tree_original.nexus"
    _write_alignment(alignment)
    output.parent.mkdir()

    def fake_run(cmd, **kwargs):
        prefix = cmd[cmd.index("-pre") + 1]
        Path(prefix + ".contree").write_text("(SEQ1,SEQ2);\n")
        return 0, "", ""

    monkeypatch.setattr(tree_builder_service, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="ML tree is missing"):
        tree_builder_service._run_iqtree(
            alignment, output, nexus,
            TreeBuilderParams(method="iqtree", bootstrap=1000), _config(), LOGGER,
        )


@pytest.mark.parametrize(
    ("bootstrap", "alrt", "support_type"),
    [
        (1000, 1000, "alrt_ufboot"),
        (1000, 0, "ufboot"),
        (0, 1000, "alrt"),
        (0, 0, None),
    ],
)
def test_iqtree_support_metadata_matches_analyses_requested(
    tmp_path, monkeypatch, bootstrap, alrt, support_type
):
    output = tmp_path / "tree.newick"

    def fake_iqtree(*args, **kwargs):
        output.write_text("(A,B);\n")
        return None, 7

    monkeypatch.setattr(tree_builder_service, "_run_iqtree", fake_iqtree)
    metadata = tree_builder_service.run_tree_builder(
        tmp_path / "alignment.fasta", output, tmp_path / "tree.nexus",
        TreeBuilderParams(
            method="iqtree", bootstrap=bootstrap, alrt_replicates=alrt
        ),
        _config(), LOGGER,
    )

    assert metadata["support_type"] == support_type
    assert ("alrt_replicates" in metadata) is (alrt > 0)


def test_k2p_metadata_describes_pathological_pairs_without_storing_matrix(caplog):
    alignment = MultipleSeqAlignment([
        SeqRecord(Seq("A" * 40), id="A"),
        SeqRecord(Seq("A" * 39 + "G"), id="B"),
        SeqRecord(Seq("G" * 20 + "C" * 4 + "A" * 16), id="C"),
        SeqRecord(Seq("C" * 30 + "G" * 10), id="D"),
        SeqRecord(Seq("N" * 20 + "A" * 20), id="E"),
    ])

    with caplog.at_level(logging.WARNING):
        _matrix, stats = tree_builder_service._k2p_distance_matrix(alignment, LOGGER)

    classified = (
        stats["ordinary_k2p_pairs"] + stats["jc_fallback_pairs"]
        + stats["saturated_pairs"] + stats["low_overlap_pairs"]
    )
    assert stats["total_pairwise_distances"] == 10
    assert classified == stats["total_pairwise_distances"]
    assert stats["ordinary_k2p_pairs"] > 0
    assert stats["jc_fallback_pairs"] > 0
    assert stats["saturated_pairs"] > 0
    assert stats["low_overlap_pairs"] > 0
    assert stats["minimum_pairwise_overlap"] == 20
    assert stats["median_pairwise_overlap"] >= 20
    assert len(stats["taxa_with_many_low_overlap_pairs"]) <= 10
    assert "quick NJ topology is poorly determined" in caplog.text
    assert not any("matrix" in key for key in stats)


def test_hard_pipeline_invariants_raise_instead_of_reaching_success(tmp_path):
    from app.workers.tasks import require_valid_pipeline_outputs

    with pytest.raises(RuntimeError, match="Pipeline output validation failed"):
        require_valid_pipeline_outputs(tmp_path, {"tree_method": "nj"}, LOGGER)


def test_recompute_validation_checks_new_generation_not_valid_original(tmp_path):
    from app.services.tree_io import write_nexus_tree
    from app.workers.tasks import validate_pipeline_outputs

    (tmp_path / "input").mkdir()
    (tmp_path / "alignment").mkdir()
    (tmp_path / "tree").mkdir()
    fasta = ">A\nACGT\n>B\nACGA\n"
    for relative in (
        "input/input_raw.fasta",
        "alignment/alignment_raw.fasta",
        "alignment/alignment_trimmed.fasta",
        "alignment/alignment_pruned.fasta",
        "alignment/alignment_pruned_aligned.fasta",
        "alignment/alignment_pruned_trimmed.fasta",
    ):
        (tmp_path / relative).write_text(fasta)

    original_tree = Phylo.read(StringIO("(A:0.1,B:0.1);"), "newick")
    (tmp_path / "tree/tree_original.newick").write_text("(A:0.1,B:0.1);\n")
    write_nexus_tree(original_tree, tmp_path / "tree/tree_original.nexus")

    # The previous generation is valid, while the staged/new recompute is not.
    (tmp_path / "tree/tree_pruned.newick").write_text("not a tree\n")
    (tmp_path / "tree/tree_pruned.nexus").write_text("#NEXUS\n")

    assert validate_pipeline_outputs(
        tmp_path, {"tree_method": "nj"}, LOGGER
    )["ok"] is True
    recomputed = validate_pipeline_outputs(
        tmp_path, {"tree_method": "nj"}, LOGGER, recompute=True
    )
    assert recomputed["ok"] is False
    assert any(failure.startswith("unparseable_nexus") for failure in recomputed["failures"])


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"tree_method": "nj"}, False),
        ({"tree_method": "iqtree", "bootstrap": 0, "alrt_replicates": 0}, False),
        ({"tree_method": "iqtree", "bootstrap": 1000, "alrt_replicates": 0}, True),
        ({"tree_method": "raxml", "enable_bootstrap": False}, False),
        ({"tree_method": "raxml", "enable_bootstrap": True}, True),
        (JobParams(input_type="fasta", tree_builder_params=TreeBuilderParams(
            method="iqtree", bootstrap=0, alrt_replicates=1000
        )), True),
    ],
)
def test_support_expectation_uses_method_and_settings(params, expected):
    from app.workers.tasks import _support_expected

    assert _support_expected(params) is expected


def test_pipeline_quality_recognizes_iqtree_dual_support_labels(caplog):
    from app.workers.tasks import _summarize_tree_quality

    tree = Phylo.read(
        StringIO("((A:0.1,B:0.1)82.7/87:0.2,(C:0.1,D:0.1)70/91:0.2);"),
        "newick",
    )
    with caplog.at_level(logging.WARNING):
        summary = _summarize_tree_quality(tree, LOGGER, support_expected=True)

    assert summary["internal_nodes_with_support"] == 2
    assert "tree_without_support_values" not in caplog.text


@pytest.mark.parametrize(
    ("requested_model", "moose_enabled"),
    [("NOT_A_MODEL", False), ("", True)],
)
def test_raxml_adjustments_are_retained_in_metadata_and_degraded(
    tmp_path, monkeypatch, caplog, requested_model, moose_enabled
):
    alignment = tmp_path / "alignment" / "alignment.fasta"
    output = tmp_path / "tree" / "tree_original.newick"
    nexus = tmp_path / "tree" / "tree_original.nexus"
    _write_alignment(alignment)
    output.parent.mkdir()

    monkeypatch.setattr(tree_builder_service, "_check_raxml_feature", lambda *a: False)

    def fake_run(cmd, **kwargs):
        if "--prefix" in cmd:
            prefix = cmd[cmd.index("--prefix") + 1]
            Path(prefix + ".raxml.bestTree").write_text("(SEQ1:0.1,SEQ2:0.1);\n")
        return 0, "", ""

    monkeypatch.setattr(tree_builder_service, "run_command", fake_run)
    params = TreeBuilderParams(
        method="raxml",
        model=requested_model,
        bootstrap_cap=1,
        start_tree_override="not-valid",
        seed="bad",
        outgroup="bad outgroup",
        early_stopping=True,
        moose_enabled=moose_enabled,
    )

    with caplog.at_level(logging.WARNING):
        effective, _selector, metadata = tree_builder_service._run_raxml(
            alignment, output, nexus, params, _config(), LOGGER
        )

    assert effective == "GTR+G"
    assert metadata["parameters_requested"]["model"] == requested_model
    assert metadata["parameters_applied"]["model"] == "GTR+G"
    assert metadata["parameters_applied"]["bootstrap_cap"] >= 100
    assert metadata["parameters_applied"]["outgroup"] is None
    assert len(metadata["parameter_warnings"]) >= 5
    assert "event=degraded.raxml_parameters_adjusted" in caplog.text


@pytest.mark.parametrize(
    ("requested_outgroup", "applied_outgroup"),
    [("A", "A"), ("MISSING", None)],
)
def test_raxml_applied_outgroup_records_actual_rooting_outcome(
    tmp_path, monkeypatch, requested_outgroup, applied_outgroup
):
    alignment = tmp_path / "alignment" / "alignment.fasta"
    output = tmp_path / "tree" / "tree_original.newick"
    nexus = tmp_path / "tree" / "tree_original.nexus"
    _write_alignment(alignment)
    with alignment.open("a") as handle:
        handle.write(">C\nACGTACGG\n")
    output.parent.mkdir()

    monkeypatch.setattr(tree_builder_service, "_check_raxml_feature", lambda *a: False)

    def fake_run(cmd, **kwargs):
        prefix = cmd[cmd.index("--prefix") + 1]
        safe_ids = [
            record.id for record in AlignIO.read(cmd[cmd.index("--msa") + 1], "fasta")
        ]
        Path(prefix + ".raxml.bestTree").write_text(
            f"(({safe_ids[0]}:0.1,{safe_ids[1]}:0.1):0.2,{safe_ids[2]}:0.3);\n"
        )
        return 0, "", ""

    monkeypatch.setattr(tree_builder_service, "run_command", fake_run)
    _effective, _selector, metadata = tree_builder_service._run_raxml(
        alignment,
        output,
        nexus,
        TreeBuilderParams(
            method="raxml", model="GTR+G", enable_bootstrap=False,
            outgroup=requested_outgroup,
        ),
        _config(),
        LOGGER,
    )

    assert metadata["parameters_requested"]["outgroup"] == requested_outgroup
    assert metadata["parameters_applied"]["outgroup"] == applied_outgroup
    if applied_outgroup is None:
        assert any("not outgroup-rooted" in warning for warning in metadata["parameter_warnings"])


@pytest.mark.parametrize(
    ("requested_model", "selected_model", "moose_supported", "moose_applied", "expected_model"),
    [
        ("GTR+G", None, True, False, "GTR+G"),
        ("NOT_A_MODEL", None, True, False, "GTR+G"),
        ("GTR+G", "HKY+G", True, True, "HKY+G"),
        ("GTR{1/2/3/4/5/6}+G", None, False, False, "GTR{1/2/3/4/5/6}+G"),
    ],
)
def test_raxml_applied_moose_records_actual_selection_outcome(
    tmp_path, monkeypatch, requested_model, selected_model, moose_supported,
    moose_applied, expected_model
):
    alignment = tmp_path / "alignment" / "alignment.fasta"
    output = tmp_path / "tree" / "tree_original.newick"
    nexus = tmp_path / "tree" / "tree_original.nexus"
    _write_alignment(alignment)
    output.parent.mkdir()

    monkeypatch.setattr(
        tree_builder_service, "_check_raxml_feature",
        lambda _config, flag: moose_supported and flag == "--moose",
    )
    monkeypatch.setattr(
        tree_builder_service, "_run_moose",
        lambda alignment_path, *args: (selected_model, alignment_path),
    )

    def fake_run(cmd, **kwargs):
        prefix = cmd[cmd.index("--prefix") + 1]
        Path(prefix + ".raxml.bestTree").write_text("(SEQ1:0.1,SEQ2:0.1);\n")
        return 0, "", ""

    monkeypatch.setattr(tree_builder_service, "run_command", fake_run)
    effective, selector, metadata = tree_builder_service._run_raxml(
        alignment,
        output,
        nexus,
        TreeBuilderParams(
            method="raxml", model=requested_model, enable_bootstrap=False,
            moose_enabled=True,
        ),
        _config(),
        LOGGER,
    )

    assert metadata["parameters_requested"]["moose_enabled"] is True
    assert metadata["parameters_applied"]["moose_enabled"] is moose_applied
    assert (selector == "MOOSE") is moose_applied
    assert metadata["parameters_requested"]["model"] == requested_model
    assert metadata["parameters_applied"]["model"] == expected_model
    assert effective == expected_model


@pytest.mark.parametrize(
    ("requested_model", "moose_supported", "expected_model"),
    [
        ("WAG+G", False, "WAG+G"),
        ("WAG+G", True, "WAG+G"),
        ("", False, "LG+G"),
        ("", True, "LG+G"),
    ],
)
def test_raxml_aa_moose_failure_retains_validated_fallback(
    tmp_path, monkeypatch, requested_model, moose_supported, expected_model
):
    alignment = tmp_path / "alignment" / "alignment.fasta"
    output = tmp_path / "tree" / "tree_original.newick"
    nexus = tmp_path / "tree" / "tree_original.nexus"
    _write_amino_acid_alignment(alignment)
    output.parent.mkdir()

    monkeypatch.setattr(
        tree_builder_service,
        "_check_raxml_feature",
        lambda _config, flag: moose_supported and flag == "--moose",
    )
    monkeypatch.setattr(
        tree_builder_service,
        "_run_moose",
        lambda alignment_path, *args: (None, alignment_path),
    )

    def fake_run(cmd, **kwargs):
        prefix = cmd[cmd.index("--prefix") + 1]
        Path(prefix + ".raxml.bestTree").write_text("(SEQ1:0.1,SEQ2:0.1);\n")
        return 0, "", ""

    monkeypatch.setattr(tree_builder_service, "run_command", fake_run)
    effective, selector, metadata = tree_builder_service._run_raxml(
        alignment,
        output,
        nexus,
        TreeBuilderParams(
            method="raxml",
            model=requested_model,
            enable_bootstrap=False,
            moose_enabled=True,
        ),
        _config(),
        LOGGER,
    )

    assert selector is None
    assert effective == expected_model
    assert metadata["data_type"] == "AA"
    assert metadata["parameters_requested"]["model"] == requested_model
    assert metadata["parameters_applied"]["model"] == expected_model
    assert metadata["parameters_applied"]["moose_enabled"] is False
