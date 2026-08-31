"""Correctness of the deterministic metrics behind "Analyze with Claude".

Every number in a review is computed here rather than by the model, so a wrong
metric is a wrong review that reads perfectly convincingly. These cover the
cases where the old code told the model something the files did not say.
"""

import gzip
import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import tree_analysis_service as service


FIXTURES = Path(__file__).parent / "fixtures"
VIEWER_JS = (
    Path(__file__).parent.parent / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _gzip_in_place(path: Path) -> Path:
    """Replace an artifact with its .gz form, as the reclamation job does."""
    target = path.with_name(path.name + ".gz")
    with open(path, "rb") as src, gzip.open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()
    return target


def test_claude_429_log_preserves_the_full_provider_message(monkeypatch, caplog):
    provider_message = (
        "You've hit your limit · resets 11:30 pm (UTC).\n"
        'Request id: "req_429_detail"'
    )
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout=json.dumps({
            "is_error": True,
            "subtype": "success",
            "api_error_status": 429,
            "terminal_reason": "api_error",
            "result": provider_message,
        }),
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed)

    with caplog.at_level(logging.WARNING, logger=service.logger.name):
        with pytest.raises(service.TreeAnalysisRateLimited):
            service._call_claude_cli({})

    expected = json.dumps(provider_message, ensure_ascii=False)
    assert f"provider_message={expected}" in caplog.text


# ---------------------------------------------------------------------------
# Polytomies and rooting
# ---------------------------------------------------------------------------

def test_root_trifurcation_is_not_counted_as_a_polytomy(tmp_path):
    newick = _write(tmp_path / "t.newick", "(A:0.1,B:0.1,(C:0.1,D:0.1)90:0.1);")

    summary, tips = service.summarize_tree(newick, "raxml")

    assert summary["file_root_degree"] == 3
    assert summary["non_root_polytomies"] == 0
    assert sorted(tips) == ["A", "B", "C", "D"]


def test_genuine_non_root_multifurcation_is_counted(tmp_path):
    newick = _write(
        tmp_path / "t.newick", "((A:0.1,B:0.1,C:0.1)80:0.2,D:0.1,E:0.1);"
    )

    summary, _ = service.summarize_tree(newick, "raxml")

    assert summary["file_root_degree"] == 3
    assert summary["non_root_polytomies"] == 1


def test_rooting_state_is_read_from_the_viewer_state_not_guessed(tmp_path):
    _write(tmp_path / "tree" / "tree_original.newick", "(A:0.1,B:0.1,C:0.1);")
    _write(
        tmp_path / "tree_state.json",
        json.dumps({"root_mode": "MIDPOINT", "root_target": None,
                    "is_midpoint_rooted": True}),
    )
    # No outgroup was submitted; the tree is still rooted.
    rooting = service._rooting_state(tmp_path, {"outgroup": None})

    assert rooting["state_known"] is True
    assert rooting["root_mode"] == "midpoint"
    assert rooting["is_midpoint_rooted"] is True
    assert rooting["submitted_outgroup"] is None
    assert "midpoint" in rooting["description"]


def test_rooting_state_reports_an_outgroup_target(tmp_path):
    _write(
        tmp_path / "tree_state.json",
        json.dumps({"root_mode": "OUTGROUP", "root": "Amanita_muscaria",
                    "is_midpoint_rooted": False}),
    )

    rooting = service._rooting_state(tmp_path, {"outgroup": "Amanita_muscaria"})

    assert rooting["root_mode"] == "outgroup"
    assert rooting["root_target"] == "Amanita_muscaria"
    assert rooting["is_midpoint_rooted"] is False


def test_rooting_state_is_unknown_rather_than_inferred_when_absent(tmp_path):
    rooting = service._rooting_state(tmp_path, {"outgroup": None})

    assert rooting["state_known"] is False
    assert rooting["root_mode"] == "unknown"
    assert "unknown" in rooting["description"]


# ---------------------------------------------------------------------------
# Branch lengths: missing is not zero
# ---------------------------------------------------------------------------

def test_missing_terminal_branch_length_is_not_reported_as_zero(tmp_path):
    # C has no branch length at all; D's is genuinely zero.
    newick = _write(tmp_path / "t.newick", "((A:0.4,B:0.2)90:0.1,C,D:0.0);")

    summary, _ = service.summarize_tree(newick, "raxml")

    assert summary["tips_missing_branch_length"] == 1
    assert summary["zero_length_terminal_branches"] == 1
    # The missing tip is kept out of every length statistic.
    assert summary["terminal_branch_length"]["max"] == 0.4
    assert summary["total_branch_length"] == pytest.approx(0.7)
    assert [row["name"] for row in summary["longest_terminal_branches"]] == [
        "A", "B", "D"
    ]


# ---------------------------------------------------------------------------
# Truncated lists carry their own totals
# ---------------------------------------------------------------------------

def test_outlier_count_is_computed_before_the_list_is_truncated(tmp_path):
    # Twenty tips far outside a tight bulk of a hundred short ones, so the
    # interquartile cut lands inside the bulk and every long tip is an outlier.
    short = ",".join(f"S{i}:{0.010 + i * 0.0001:.4f}" for i in range(100))
    long_tips = ",".join(f"L{i}:5.0" for i in range(20))
    newick = _write(tmp_path / "t.newick", f"({short},{long_tips});")

    summary, _ = service.summarize_tree(newick, "raxml")

    assert summary["outlier_tip_count"] == 20
    assert len(summary["outlier_long_branch_tips"]) == service.TOP_N


def test_identical_group_totals_are_reported_before_truncation():
    records = []
    # 15 groups of two identical sequences, so the TOP_N cap bites.
    for group in range(15):
        seq = "ACGT" * 5 + "A" * group + "-" * (15 - group)
        records.append((f"g{group}_a", seq))
        records.append((f"g{group}_b", seq))

    summary = service.summarize_alignment(records)

    assert summary["identical_sequence_group_count"] == 15
    assert summary["sequences_in_identical_groups_total"] == 30
    assert len(summary["identical_sequence_groups"]) == service.TOP_N


def test_identical_group_names_are_capped_and_flagged():
    seq = "ACGTACGTAC"
    records = [(f"seq{i}", seq) for i in range(9)]

    summary = service.summarize_alignment(records)

    group = summary["identical_sequence_groups"][0]
    assert group["count"] == 9
    assert len(group["names"]) == 6
    assert group["names_truncated"] is True


# ---------------------------------------------------------------------------
# Gap normalization
# ---------------------------------------------------------------------------

def test_dot_tilde_and_question_padding_group_as_identical_sequences():
    records = [
        ("dashes", "---ACGTACGT---"),
        ("dots", "...ACGTACGT..."),
        ("tildes", "~~~ACGTACGT~~~"),
        ("questions", "???ACGTACGT???"),
        ("different", "---ACGTACGA---"),
    ]

    summary = service.summarize_alignment(records)

    assert summary["identical_sequence_group_count"] == 1
    group = summary["identical_sequence_groups"][0]
    assert group["count"] == 4
    assert "different" not in group["names"]


# ---------------------------------------------------------------------------
# Sampled column metrics
# ---------------------------------------------------------------------------

def _sampled_summary(monkeypatch):
    # 40 columns over 4 sequences, with the budget forced low enough to sample.
    monkeypatch.setattr(service, "MAX_ALIGNMENT_CELLS", 40)
    block = "ACGTACGTAC" * 4
    records = [
        ("a", block), ("b", block), ("c", block.replace("A", "G")),
        ("d", block.replace("A", "G")),
    ]
    return service.summarize_alignment(records)


def test_sampled_column_metrics_never_publish_a_bare_count(monkeypatch):
    summary = _sampled_summary(monkeypatch)

    assert summary["column_sampling_applied"] is True
    assert summary["column_metrics_are_estimates"] is True
    assert summary["columns_scored"] < summary["columns"]
    for exact_key in (
        "parsimony_informative_columns",
        "columns_below_50_percent_occupancy",
        "all_gap_columns",
    ):
        assert exact_key not in summary, f"{exact_key} would read as an exact count"
    for estimated_key in (
        "parsimony_informative_columns_estimated",
        "columns_below_50_percent_occupancy_estimated",
        "all_gap_columns_estimated",
    ):
        assert estimated_key in summary
    assert summary["parsimony_informative_percent"] is not None


def test_unsampled_column_metrics_stay_exact():
    block = "ACGTACGTAC"
    records = [("a", block), ("b", block), ("c", block.replace("A", "G"))]

    summary = service.summarize_alignment(records)

    assert summary["column_sampling_applied"] is False
    assert summary["column_metrics_are_estimates"] is False
    assert summary["columns_scored"] == summary["columns"]
    assert isinstance(summary["parsimony_informative_columns"], int)
    assert "parsimony_informative_columns_estimated" not in summary


def test_estimated_count_scales_the_sample_to_the_whole_alignment():
    assert service._scaled_to_full_alignment(10, 20, 200) == 100
    assert service._scaled_to_full_alignment(0, 20, 200) == 0
    assert service._scaled_to_full_alignment(5, 0, 200) is None


# ---------------------------------------------------------------------------
# Support classification: backend and viewer must agree
# ---------------------------------------------------------------------------

def _support_cases():
    payload = json.loads(
        (FIXTURES / "support_classification_cases.json").read_text()
    )
    return payload["cases"]


@pytest.mark.parametrize("case", _support_cases(), ids=lambda c: c["name"])
def test_backend_support_classification(case):
    assert service._classify_support(
        list(case["values"]),
        case["has_dual"],
        case["tree_method"],
        case.get("alrt_only", False),
    ) == case["expected"]


def test_viewer_support_classification_matches_the_same_table():
    """Run the viewer's own rules over the shared fixture under node.

    The backend and the badge classifying one tree differently is the specific
    failure this pairing exists to prevent, so the fixture is executed against
    both rather than transcribed into a second table.
    """
    if shutil.which("node") is None:  # pragma: no cover - environment dependent
        pytest.skip("node is not installed")

    harness = f"""
        const fs = require('fs');
        // The viewer file is browser code: give it the globals it touches at
        // load time, then read the two functions it publishes on `window`.
        // Enough of a browser for the file to reach the end of its own IIFE.
        global.window = {{
            addEventListener() {{}},
            location: {{ search: '', href: 'http://localhost/' }},
            matchMedia: () => ({{ matches: false, addEventListener() {{}} }}),
        }};
        global.document = {{
            addEventListener() {{}},
            documentElement: {{ classList: {{ contains: () => false }} }},
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: () => ({{ style: {{}}, classList: {{ add() {{}} }} }}),
        }};
        const source = fs.readFileSync({str(VIEWER_JS)!r}, 'utf8');
        new Function('window', 'document', source)(global.window, global.document);

        const cases = JSON.parse(
            fs.readFileSync({str(FIXTURES / 'support_classification_cases.json')!r}, 'utf8')
        ).cases;
        const results = cases.map(c => window.classifySupportType(
            c.values, c.has_dual, c.tree_method, {{ alrtOnly: !!c.alrt_only }}
        ));
        process.stdout.write(JSON.stringify(results));
    """
    completed = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stderr

    got = json.loads(completed.stdout)
    # One read of the shared table, and an explicit length check before the
    # comparison: zip() stops at the shorter input, so a harness that returned
    # results for only the first few cases produced an empty `mismatches` and a
    # green test over a table that was never fully checked.
    cases = _support_cases()
    expected = [case["expected"] for case in cases]
    assert len(got) == len(expected), (
        f"the viewer returned {len(got)} results for {len(expected)} cases"
    )
    mismatches = [
        (case["name"], want, have)
        for case, want, have in zip(cases, expected, got, strict=True)
        if want != have
    ]
    assert not mismatches, f"viewer disagrees with the shared table: {mismatches}"


def test_raxml_bootstrap_below_one_is_not_called_a_posterior():
    assert service._classify_support([0.0, 1.0, 0.95], False, "raxml") == "BS"
    assert service._classify_support([0.0, 1.0, 0.95], False, "") == "PP"


def test_at_least_moderate_percent_is_cumulative(tmp_path):
    newick = _write(
        tmp_path / "t.newick",
        "(((A:0.1,B:0.1)99:0.1,(C:0.1,D:0.1)75:0.1)50:0.1,E:0.1,F:0.1);",
    )

    summary, _ = service.summarize_tree(newick, "raxml")

    assert summary["support_type"] == "BS"
    assert summary["support_nodes_scored"] == 3
    # 99 and 75 are both >= 70; 99 alone is >= 95.
    assert summary["at_least_moderate_percent"] == pytest.approx(66.67, abs=0.01)
    assert summary["strongly_supported_percent"] == pytest.approx(33.33, abs=0.01)
    assert "moderately_supported_percent" not in summary


# ---------------------------------------------------------------------------
# gzip-transparent artifact reads
# ---------------------------------------------------------------------------

def _minimal_job(tmp_path: Path) -> Path:
    job_dir = tmp_path / "job"
    _write(
        job_dir / "tree" / "tree_original.newick",
        "((A:0.4,B:0.2)90:0.1,C:0.3,D:0.15);",
    )
    _write(job_dir / "tree" / "tree_metadata.json",
           json.dumps({"method": "raxml", "model": "GTR+G", "bootstrap": 100}))
    _write(job_dir / "input_info.json",
           json.dumps({"aligner": "mafft", "outgroup": None}))
    _write(job_dir / "tree_state.json",
           json.dumps({"root_mode": "MIDPOINT", "is_midpoint_rooted": True}))
    _write(
        job_dir / "alignment" / "alignment_trimmed.fasta",
        ">A\nACGTACGTAA\n>B\nACGTACGTAG\n>C\nACGTTCGTAA\n>D\nACGTTCGTAG\n",
    )
    return job_dir


def test_review_context_is_identical_for_plain_and_gzipped_artifacts(tmp_path):
    plain = _minimal_job(tmp_path / "plain")
    gzipped = _minimal_job(tmp_path / "gz")
    for relative in (
        Path("tree") / "tree_original.newick",
        Path("tree") / "tree_metadata.json",
        Path("alignment") / "alignment_trimmed.fasta",
    ):
        _gzip_in_place(gzipped / relative)

    from_plain = service.build_context(plain)
    from_gz = service.build_context(gzipped)

    assert from_plain == from_gz
    # And the gzipped run really did read the metadata rather than defaulting.
    assert from_gz["pipeline"]["tree_method"] == "raxml"
    assert from_gz["tree"]["support_type"] == "BS"


def test_load_json_reads_a_gzipped_metadata_file(tmp_path):
    path = _write(tmp_path / "tree_metadata.json", json.dumps({"method": "iqtree"}))
    _gzip_in_place(path)

    assert service._load_json(path) == {"method": "iqtree"}


def test_load_json_still_returns_empty_for_malformed_content(tmp_path):
    _write(tmp_path / "broken.json", "{not json")

    assert service._load_json(tmp_path / "broken.json") == {}
    assert service._load_json(tmp_path / "absent.json") == {}


def test_absent_from_tree_metric_is_named_for_what_it_measures(tmp_path):
    job_dir = _minimal_job(tmp_path)
    # An alignment row with no tip in the tree.
    (job_dir / "alignment" / "alignment_trimmed.fasta").write_text(
        ">A\nACGTACGTAA\n>B\nACGTACGTAG\n>C\nACGTTCGTAA\n"
        ">D\nACGTTCGTAG\n>E\nACGTTCGTAC\n"
    )

    context = service.build_context(job_dir)

    assert context["alignment"]["alignment_sequences_absent_from_current_tree"] == 1
    assert "sequences_excluded_by_viewer_pruning" not in context["alignment"]
    assert context["tree"]["rooting"]["root_mode"] == "midpoint"


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------

def _valid_review():
    return {
        "overall_rating": "usable",
        "headline": "Broadly sound with one caveat.",
        "summary": "Paragraph one.\n\nParagraph two.",
        "strengths": ["Good occupancy."],
        "concerns": [
            {"severity": "medium", "title": "Gappy tail",
             "detail": "12% of columns fall below 50% occupancy."}
        ],
        "recommendations": ["Trim the ragged ends."],
        "sequences_to_inspect": [
            {"name": "KX123456", "reason": "Longest terminal branch."}
        ],
    }


def test_valid_review_passes_validation():
    validated = service._validate_review(_valid_review())

    assert validated["overall_rating"] == "usable"
    assert validated["concerns"][0]["severity"] == "medium"


def test_literal_newline_unescaping_is_preserved():
    review = _valid_review()
    review["summary"] = "Paragraph one.\\n\\nParagraph two."

    validated = service._validate_review(review)

    assert validated["summary"] == "Paragraph one.\n\nParagraph two."


def test_unknown_rating_is_rejected_rather_than_shown():
    review = _valid_review()
    review["overall_rating"] = "excellent"

    with pytest.raises(service.TreeAnalysisError, match="unknown overall rating"):
        service._validate_review(review)


@pytest.mark.parametrize("field,value", [
    ("headline", ["not", "a", "string"]),
    ("headline", ""),
    ("summary", 42),
    ("strengths", "should be a list"),
    ("strengths", [{"nested": "object"}]),
    ("recommendations", None),
    ("concerns", {"severity": "high"}),
    ("sequences_to_inspect", "none"),
])
def test_invalid_field_types_are_rejected(field, value):
    review = _valid_review()
    review[field] = value

    with pytest.raises(service.TreeAnalysisError):
        service._validate_review(review)


def test_invalid_concern_severity_is_rejected():
    review = _valid_review()
    review["concerns"][0]["severity"] = "critical"

    with pytest.raises(service.TreeAnalysisError, match="unknown severity"):
        service._validate_review(review)


def test_concern_missing_a_field_is_rejected():
    review = _valid_review()
    review["concerns"] = [{"severity": "low", "title": "Only a title"}]

    with pytest.raises(service.TreeAnalysisUpstreamError, match="concern"):
        service._validate_review(review)


def test_sequence_to_inspect_with_a_non_string_name_is_rejected():
    review = _valid_review()
    review["sequences_to_inspect"] = [{"name": 12345, "reason": "Long branch."}]

    with pytest.raises(
        service.TreeAnalysisUpstreamError, match="sequences_to_inspect"
    ):
        service._validate_review(review)


def test_missing_required_key_is_still_rejected():
    review = _valid_review()
    del review["recommendations"]

    with pytest.raises(service.TreeAnalysisError, match="missing"):
        service._validate_review(review)


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------

def test_schema_version_participates_in_the_cache_fingerprint(monkeypatch):
    context = {"alignment": {"sequences": 3}, "tree": {"tips": 3}}
    before = service.fingerprint(context)
    monkeypatch.setattr(service, "REVIEW_SCHEMA_VERSION",
                        service.REVIEW_SCHEMA_VERSION + 1)

    assert service.fingerprint(context) != before


# ---------------------------------------------------------------------------
# Long-branch outliers over positive lengths only
# ---------------------------------------------------------------------------

def test_zero_dominated_tree_still_finds_its_one_long_branch(tmp_path):
    # The commonest shape in this system: a cluster of identical sequences on
    # zero-length branches, a handful of near-zero ones, and one obvious
    # outlier. With the zeros in the sample Q1 and Q3 are both 0, the cut
    # collapses to 0, and a cut of 0 is discarded as meaningless -- so the
    # 40x branch was reported as no outlier at all.
    zeros = ",".join(f"Z{i}:0.0" for i in range(20))
    tiny = ",".join(f"T{i}:0.00000001" for i in range(5))
    newick = _write(tmp_path / "t.newick", f"({zeros},{tiny},LONG:2.0);")

    summary, _ = service.summarize_tree(newick, "raxml")

    assert summary["terminal_branches_with_positive_length"] == 6
    assert summary["outlier_tip_count"] == 1
    assert [row["name"] for row in summary["outlier_long_branch_tips"]] == ["LONG"]
    assert summary["outlier_branch_threshold"] > 0
    assert "positive length" in summary["outlier_rule"]


def test_tightly_clustered_tiny_branches_do_not_all_become_outliers(tmp_path):
    # An interquartile range of zero over tiny positive values makes every
    # slightly longer branch an "outlier". The 5x-median floor is what stops a
    # tree of near-identical sequences producing ninety false suspects.
    bulk = ",".join(f"B{i}:0.000000001" for i in range(90))
    slightly_longer = ",".join(f"S{i}:0.000000003" for i in range(10))
    newick = _write(tmp_path / "t.newick", f"({bulk},{slightly_longer});")

    summary, _ = service.summarize_tree(newick, "raxml")

    assert summary["outlier_tip_count"] == 0
    assert summary["outlier_branch_threshold"] == pytest.approx(5e-9, rel=1e-6)


def test_fewer_than_four_positive_branches_publishes_no_threshold(tmp_path):
    newick = _write(tmp_path / "t.newick", "(A:0.0,B:0.0,C:0.1,D:0.2);")

    summary, _ = service.summarize_tree(newick, "raxml")

    assert summary["outlier_branch_threshold"] is None
    assert summary["outlier_tip_count"] == 0
    assert "not computed" in summary["outlier_rule"]


def test_longest_terminal_branches_carry_their_share_of_the_tree(tmp_path):
    newick = _write(tmp_path / "t.newick", "((A:1.0,B:1.0)90:1.0,C:1.0,D:1.0);")

    summary, _ = service.summarize_tree(newick, "raxml")

    # Four terminal branches of 1.0 plus one internal branch of 1.0.
    assert summary["total_branch_length"] == pytest.approx(5.0)
    assert summary["longest_terminal_branches_listed"] == 4
    assert summary["longest_terminal_branches_share_of_total_percent"] == pytest.approx(80.0)


def test_tiny_branch_lengths_survive_rounding(tmp_path):
    newick = _write(tmp_path / "t.newick", "(A:0.000000006,B:0.000000007,C:0.5);")

    summary, _ = service.summarize_tree(newick, "raxml")

    lengths = {row["name"]: row["branch_length"] for row in summary["longest_terminal_branches"]}
    assert lengths["A"] == pytest.approx(6e-9)
    assert summary["terminal_branch_length"]["min"] == pytest.approx(6e-9)


# ---------------------------------------------------------------------------
# Rooting: only explicit state licenses a statement
# ---------------------------------------------------------------------------

def test_state_written_by_a_rename_carries_no_rooting_and_is_not_unrooted(tmp_path):
    # Every prune and rename rewrites tree_state.json, and older states predate
    # the rooting keys. Reading a missing root_mode as "unrooted" told the
    # reviewer a midpoint-rooted tree was unrooted.
    _write(
        tmp_path / "tree_state.json",
        json.dumps({"current_tree": "pruned", "renames": {"A": "Amanita sp."},
                    "pruned_taxa": ["B"]}),
    )

    rooting = service._rooting_state(tmp_path, {"outgroup": None})

    assert rooting["state_known"] is True
    assert rooting["rooting_known"] is False
    assert rooting["root_mode"] == "unknown"
    assert "unspecified" in rooting["description"]
    assert "unrooted" in rooting["description"]  # ...as an instruction not to say it


def test_blank_root_mode_after_a_failed_reapply_is_unknown(tmp_path):
    # recompute writes root_mode "" and is_midpoint_rooted False when it could
    # not reapply the previous rooting. That is "we do not know", not "unrooted".
    _write(
        tmp_path / "tree_state.json",
        json.dumps({"root_mode": "", "root_target": None, "is_midpoint_rooted": False}),
    )

    rooting = service._rooting_state(tmp_path, {"outgroup": None})

    assert rooting["rooting_known"] is False
    assert rooting["root_mode"] == "unknown"


def test_midpoint_flag_alone_establishes_midpoint_rooting(tmp_path):
    # The auto-rooting fallbacks record their own mode name and leave the
    # midpoint flag set; that flag is explicit state and may be trusted.
    _write(
        tmp_path / "tree_state.json",
        json.dumps({"root_mode": "none", "is_midpoint_rooted": True}),
    )

    rooting = service._rooting_state(tmp_path, {"outgroup": None})

    assert rooting["rooting_known"] is True
    assert rooting["root_mode"] == "midpoint"


def test_explicitly_unrooted_state_is_reported_as_unrooted(tmp_path):
    _write(
        tmp_path / "tree_state.json",
        json.dumps({"root_mode": "unrooted", "is_midpoint_rooted": False}),
    )

    rooting = service._rooting_state(tmp_path, {"outgroup": None})

    assert rooting["rooting_known"] is True
    assert rooting["root_mode"] == "unrooted"
    assert "explicitly unrooted" in rooting["description"]


def test_known_rooting_states_are_flagged_as_known(tmp_path):
    _write(tmp_path / "tree_state.json",
           json.dumps({"root_mode": "OUTGROUP", "root": "Amanita_muscaria"}))

    assert service._rooting_state(tmp_path, {})["rooting_known"] is True


def test_missing_state_file_is_unknown_not_unrooted(tmp_path):
    rooting = service._rooting_state(tmp_path, {"outgroup": None})

    assert rooting["rooting_known"] is False
    assert rooting["state_known"] is False


# ---------------------------------------------------------------------------
# The artificial Newick root is not an extra split
# ---------------------------------------------------------------------------

def test_artificial_binary_root_is_one_split_not_two(tmp_path):
    # The two children of a binary root describe the same bipartition. Counted
    # separately they doubled the edge and, because only one child carries the
    # label, invented an unsupported internal node on every ordinary file.
    newick = _write(
        tmp_path / "t.newick", "((A:0.1,B:0.1)90:0.05,(C:0.1,D:0.1):0.07);"
    )

    summary, _ = service.summarize_tree(newick, "raxml")

    assert summary["artificial_root_edge_merged"] is True
    assert summary["internal_nodes"] == 1
    assert summary["support_nodes_scored"] == 1
    assert summary["internal_nodes_without_support"] == 0
    # One edge of 0.05 + 0.07, not two edges of 0.05 and 0.07.
    assert summary["internal_branch_length"]["median"] == pytest.approx(0.12)


def test_unrooted_trifurcating_root_keeps_every_split(tmp_path):
    newick = _write(
        tmp_path / "t.newick",
        "(((A:0.1,B:0.1)99:0.1,(C:0.1,D:0.1)75:0.1)50:0.1,E:0.1,F:0.1);",
    )

    summary, _ = service.summarize_tree(newick, "raxml")

    assert summary["artificial_root_edge_merged"] is False
    assert summary["internal_nodes"] == 3
    assert summary["support_nodes_scored"] == 3


def test_root_child_that_is_a_tip_is_left_alone(tmp_path):
    # Outgroup-rooted on a single tip: only one root child is internal, so
    # there is nothing to merge and nothing to drop.
    newick = _write(tmp_path / "t.newick", "(OUT:0.5,((A:0.1,B:0.1)90:0.1,C:0.1)80:0.2);")

    summary, _ = service.summarize_tree(newick, "raxml")

    assert summary["artificial_root_edge_merged"] is False
    assert summary["internal_nodes"] == 2
    assert summary["support_nodes_scored"] == 2


def test_internal_nodes_missing_a_branch_length_are_counted_separately(tmp_path):
    newick = _write(
        tmp_path / "t.newick",
        "(((A:0.1,B:0.1)90,(C:0.1,D:0.1)80:0.1)70:0.1,E:0.1,F:0.1);",
    )

    summary, _ = service.summarize_tree(newick, "raxml")

    assert summary["internal_nodes_missing_branch_length"] == 1
    assert summary["zero_length_internal_branches"] == 0
    assert summary["near_zero_internal_branches"] == 0


def test_near_zero_internal_branches_use_a_stated_tolerance(tmp_path):
    newick = _write(
        tmp_path / "t.newick",
        "(((A:0.1,B:0.1)90:0.0000001,(C:0.1,D:0.1)80:0.5)70:0.2,E:0.1,F:0.1);",
    )

    summary, _ = service.summarize_tree(newick, "raxml")

    assert summary["near_zero_internal_branches"] == 1
    assert summary["zero_length_internal_branches"] == 0
    assert summary["near_zero_branch_length_tolerance"] == service.NEAR_ZERO_BRANCH_LENGTH


def test_support_is_stratified_by_subtending_branch_length(tmp_path):
    # Weak support inside an unresolved cluster of identical sequences is a
    # different finding from a weakly supported backbone, and the model must not
    # have to do this partition itself.
    newick = _write(
        tmp_path / "t.newick",
        "(((A:0.1,B:0.1)50:0.0000001,(C:0.1,D:0.1)99:0.5)97:0.2,E:0.1,F:0.1);",
    )

    summary, _ = service.summarize_tree(newick, "raxml")
    stratified = summary["support_by_subtending_branch_length"]

    assert stratified["tolerance"] == service.NEAR_ZERO_BRANCH_LENGTH
    assert stratified["near_zero_branches"]["internal_nodes"] == 1
    assert stratified["near_zero_branches"]["strongly_supported_percent"] == 0.0
    assert stratified["longer_branches"]["internal_nodes"] == 2
    assert stratified["longer_branches"]["strongly_supported_percent"] == 100.0
    assert stratified["branches_without_length"] == 0
    # A single-value scale has no joint rule to publish.
    assert "jointly_well_supported_percent" not in stratified["longer_branches"]


def test_dual_support_stratification_uses_the_joint_rule(tmp_path):
    # SH-aLRT 20 / UFBoot 99 is NOT a well supported clade, and the scalar
    # statistics see only the UFBoot half. The partition has to publish the
    # joint figure or it reports a resolved backbone that the tree does not have.
    newick = _write(
        tmp_path / "t.newick",
        "(((A:0.1,B:0.1)20/99:0.5,(C:0.1,D:0.1)95/99:0.5)90/98:0.2,E:0.1,F:0.1);",
    )

    summary, _ = service.summarize_tree(newick, "iqtree")
    longer = summary["support_by_subtending_branch_length"]["longer_branches"]

    assert summary["support_type"] == "ALRT_UFBOOT"
    assert longer["internal_nodes"] == 3
    assert longer["nodes_with_dual_support"] == 3
    # The UFBoot half alone calls every one of them strong.
    assert longer["strongly_supported_percent"] == 100.0
    assert longer["jointly_well_supported_percent"] == pytest.approx(66.67, abs=0.01)
    assert longer["jointly_well_supported_rule"] == service.DUAL_SUPPORT_RULE


# ---------------------------------------------------------------------------
# Column denominators
# ---------------------------------------------------------------------------

def _occupancy_records():
    # Column 0: six residues, all A       -> invariant, >=4 residues
    # Column 1: two residues, both A      -> invariant, but informs nothing
    # Column 2: six residues, 3 A / 3 G   -> parsimony-informative, >=4 residues
    return [
        ("s1", "AAA"), ("s2", "AAA"), ("s3", "A-A"),
        ("s4", "A-G"), ("s5", "A-G"), ("s6", "A-G"),
    ]


def test_nearly_empty_columns_get_their_own_denominator():
    summary = service.summarize_alignment(_occupancy_records())

    assert summary["columns_with_at_least_4_unambiguous_residues"] == 2
    assert summary["invariant_column_percent"] == pytest.approx(66.67, abs=0.01)
    assert summary["invariant_percent_of_columns_with_at_least_4_unambiguous_residues"] == 50.0
    assert summary["parsimony_informative_percent"] == pytest.approx(33.33, abs=0.01)
    assert summary[
        "parsimony_informative_percent_of_columns_with_at_least_4_unambiguous_residues"
    ] == 50.0


def test_ambiguous_only_columns_are_outside_the_strict_denominator():
    # Column 1 is occupied by four residues but every one of them is an N, so it
    # carries no state: it can neither establish invariance nor resolve a split
    # and must not count towards the stricter denominator.
    records = [
        ("s1", "ANA"), ("s2", "ANA"), ("s3", "ANG"),
        ("s4", "ANG"), ("s5", "A-G"), ("s6", "A-G"),
    ]

    summary = service.summarize_alignment(records)

    assert summary["columns_with_at_least_4_unambiguous_residues"] == 2
    # The all-N column still counts as occupied: ordinary occupancy is unchanged.
    assert summary["mean_column_occupancy_percent"] == pytest.approx(88.89, abs=0.01)
    assert summary[
        "invariant_percent_of_columns_with_at_least_4_unambiguous_residues"
    ] == 50.0
    assert summary[
        "parsimony_informative_percent_of_columns_with_at_least_4_unambiguous_residues"
    ] == 50.0


def test_four_residue_column_count_is_estimated_when_sampled(monkeypatch):
    monkeypatch.setattr(service, "MAX_ALIGNMENT_CELLS", 40)
    block = "ACGTACGTAC" * 4
    records = [("a", block), ("b", block), ("c", block.replace("A", "G")),
               ("d", block.replace("A", "G"))]

    summary = service.summarize_alignment(records)

    assert "columns_with_at_least_4_unambiguous_residues" not in summary
    assert summary["columns_with_at_least_4_unambiguous_residues_estimated"] is not None


# ---------------------------------------------------------------------------
# Sampling is a property of the step, not of the cell ceiling
# ---------------------------------------------------------------------------

def test_step_of_one_over_the_cell_ceiling_is_not_sampling(monkeypatch):
    monkeypatch.setattr(service, "MAX_ALIGNMENT_CELLS", 100)

    indices, sampled = service._column_indices(15, 10)

    assert indices == list(range(15))
    assert sampled is False


def test_every_column_scored_publishes_exact_counts_even_past_the_ceiling(monkeypatch):
    monkeypatch.setattr(service, "MAX_ALIGNMENT_CELLS", 100)
    records = [(f"s{i}", "ACGTACGTACGTACG") for i in range(10)]

    summary = service.summarize_alignment(records)

    assert summary["columns_scored"] == 15
    assert summary["column_metrics_are_estimates"] is False
    assert isinstance(summary["parsimony_informative_columns"], int)


def test_column_sampling_still_applies_when_the_step_exceeds_one(monkeypatch):
    monkeypatch.setattr(service, "MAX_ALIGNMENT_CELLS", 40)

    indices, sampled = service._column_indices(40, 4)

    assert sampled is True
    assert len(indices) < 40


# ---------------------------------------------------------------------------
# Overlap and gap composition
# ---------------------------------------------------------------------------

def test_pairwise_overlap_reports_pairs_with_nothing_to_compare():
    records = [
        ("front", "ACGT----"),
        ("back", "----ACGT"),
        ("full", "ACGTACGT"),
    ]

    summary = service.summarize_alignment(records)
    overlap = summary["pairwise_overlap"]

    assert overlap["pairs_compared"] == 3
    assert overlap["pairs_sampled"] is False
    assert overlap["pairs_with_no_comparable_columns"] == 1
    assert overlap["pairs_below_100_overlap_columns"] == 3
    assert overlap["overlap_columns"]["min"] == 0
    assert overlap["overlap_columns"]["max"] == 4
    # The identity figure rests only on the two pairs that overlap at all.
    assert summary["mean_pairwise_identity_percent"] == 100.0


def test_terminal_padding_and_internal_gaps_are_not_the_same_problem():
    records = [
        ("padded", "---ACGT---"),
        ("interior", "ACG--TACGT"),
    ]

    summary = service.summarize_alignment(records)
    rows = {row["name"]: row for row in summary["gappiest_sequences"]}

    assert rows["padded"]["terminal_gap_percent"] == 60.0
    assert rows["padded"]["internal_gap_percent"] == 0.0
    assert rows["interior"]["terminal_gap_percent"] == 0.0
    assert rows["interior"]["internal_gap_percent"] == 20.0
    assert summary["mean_terminal_gap_percent"] == 30.0
    assert summary["mean_internal_gap_percent"] == 10.0
    assert [row["name"] for row in summary["most_internally_gapped_sequences"]] == [
        "interior"
    ]


def test_an_all_gap_row_is_all_terminal_gaps():
    summary = service.summarize_alignment([("empty", "------"), ("real", "ACGTAC")])
    row = next(r for r in summary["gappiest_sequences"] if r["name"] == "empty")

    assert row["terminal_gap_percent"] == 100.0
    assert row["internal_gap_percent"] == 0.0


# ---------------------------------------------------------------------------
# Tree method normalization and IQ-TREE support semantics
# ---------------------------------------------------------------------------

def test_effective_bootstrap_uses_the_shared_method_normalization():
    assert "not run" in service._effective_bootstrap("FastTree2", 1000)
    assert "not run" in service._effective_bootstrap("Fast Tree", 1000)
    assert "not run" in service._effective_bootstrap("Neighbour-Joining", 100)
    # IQ-TREE really does run the replicates it is given.
    assert service._effective_bootstrap("IQ-TREE 2", 1000) == 1000


def test_iqtree_single_support_is_ufboot_not_classical_bootstrap(tmp_path):
    newick = _write(
        tmp_path / "t.newick",
        "(((A:0.1,B:0.1)88:0.1,(C:0.1,D:0.1)96:0.1)72:0.1,E:0.1,F:0.1);",
    )

    summary, _ = service.summarize_tree(newick, "iqtree")

    assert summary["support_type"] == "UFBOOT"
    assert summary["strong_support_threshold"] == 95.0
    # No conventional middle band: 88 is not "moderate bootstrap support".
    assert summary["moderate_support_threshold"] is None
    assert summary["at_least_moderate_percent"] is None
    assert summary["strongly_supported_percent"] == pytest.approx(33.33, abs=0.01)
    assert "not the classical bootstrap" in summary["support_scale_note"].lower()


def test_iqtree_alrt_only_support_is_not_classified_as_bootstrap(tmp_path):
    newick = _write(
        tmp_path / "t.newick",
        "(((A:0.1,B:0.1)88:0.1,(C:0.1,D:0.1)96:0.1)72:0.1,E:0.1,F:0.1);",
    )

    summary, _ = service.summarize_tree(newick, "iqtree", True)

    assert summary["support_type"] == "ALRT"
    assert summary["strong_support_threshold"] == 80.0
    assert "SH-aLRT" in summary["support_scale_note"]


def test_iqtree_dual_labels_keep_the_dual_scale(tmp_path):
    newick = _write(
        tmp_path / "t.newick",
        "(((A:0.1,B:0.1)85/97:0.1,(C:0.1,D:0.1)70/88:0.1)90/99:0.1,E:0.1,F:0.1);",
    )

    summary, _ = service.summarize_tree(newick, "iqtree")

    assert summary["support_type"] == "ALRT_UFBOOT"
    assert summary["dual_support_summary"]["nodes_scored"] == 3
    assert summary["dual_support_summary"]["nodes_meeting_both_thresholds"] == 2


def test_alrt_only_is_detected_from_the_recorded_replicate_counts():
    metadata = {"method": "iqtree", "alrt_replicates": 1000, "bootstrap": 0}

    assert service._iqtree_alrt_only(metadata, {}) is True
    assert service._iqtree_alrt_only({**metadata, "bootstrap": 1000}, {}) is False
    assert service._iqtree_alrt_only({"method": "raxml", "alrt_replicates": 1000}, {}) is False
    assert service._iqtree_alrt_only({}, {"tree_method": "iqtree", "alrt_replicates": 1000}) is True


# ---------------------------------------------------------------------------
# Alignment provenance: what this alignment is, and what it is not
# ---------------------------------------------------------------------------

def _fasta(names_to_sequences):
    return "".join(f">{name}\n{seq}\n" for name, seq in names_to_sequences.items())


def _provenance_job(tmp_path: Path, *, files, input_info, pruned_tree=False,
                    recomputed=False, tips="ABCD") -> Path:
    job_dir = tmp_path / "job"
    newick = "((" + ":0.1,".join(tips[:2]) + ":0.1)90:0.1," + ":0.1,".join(tips[2:]) + ":0.1);"
    _write(job_dir / "tree" / "tree_original.newick", newick)
    if pruned_tree:
        _write(job_dir / "tree" / "tree_pruned.newick", newick)
    if recomputed:
        _write(job_dir / "tree" / "tree_pruned_metadata.json",
               json.dumps({"method": "raxml", "model": "GTR+G", "bootstrap": 100}))
    _write(job_dir / "tree" / "tree_metadata.json",
           json.dumps({"method": "raxml", "model": "GTR+G", "bootstrap": 100}))
    _write(job_dir / "input_info.json", json.dumps(input_info))
    _write(job_dir / "tree_state.json",
           json.dumps({"root_mode": "MIDPOINT", "is_midpoint_rooted": True}))
    for name, records in files.items():
        _write(job_dir / "alignment" / name, _fasta(records))
    return job_dir


_FOUR_ROWS = {"A": "ACGTACGTAA", "B": "ACGTACGTAG", "C": "ACGTTCGTAA", "D": "ACGTTCGTAG"}


def test_trimming_method_none_reports_no_columns_removed(tmp_path):
    # With trimming off the pipeline copies alignment_raw.fasta forward under
    # the trimmed name, so a before/after comparison is comparing a file with
    # itself and any "columns removed" figure describes nothing.
    job_dir = _provenance_job(
        tmp_path,
        files={"alignment_raw.fasta": _FOUR_ROWS, "alignment_trimmed.fasta": _FOUR_ROWS},
        input_info={"trimming_method": "none", "outgroup": None},
    )

    context = service.build_context(job_dir)
    alignment = context["alignment"]

    assert context["pipeline"]["trimming_ran"] is False
    assert "columns_removed_by_trimming" not in alignment
    assert "columns_before_trimming" not in alignment
    assert alignment["alignment_is_tree_builder_input"] is True


def test_real_trimming_reports_the_columns_it_removed(tmp_path):
    job_dir = _provenance_job(
        tmp_path,
        files={
            "alignment_raw.fasta": {name: seq + "--" for name, seq in _FOUR_ROWS.items()},
            "alignment_trimmed.fasta": _FOUR_ROWS,
        },
        input_info={"trimming_details": {"method": "trimal"}, "outgroup": None},
    )

    alignment = service.build_context(job_dir)["alignment"]

    assert alignment["columns_before_trimming"] == 12
    assert alignment["columns_removed_by_trimming"] == 2
    assert alignment["trimming_measured_from"] == "alignment_raw.fasta"


def test_recomputed_tree_is_measured_against_its_own_alignment(tmp_path):
    # After a recompute the displayed tree came from the realigned pruned set.
    # Measuring alignment_trimmed.fasta instead described the alignment of a
    # tree the viewer is no longer showing.
    job_dir = _provenance_job(
        tmp_path,
        files={
            "alignment_raw.fasta": dict(_FOUR_ROWS, E="ACGTTCGTAC", F="ACGTTCGTAT"),
            "alignment_trimmed.fasta": dict(_FOUR_ROWS, E="ACGTTCGTAC", F="ACGTTCGTAT"),
            "alignment_pruned_aligned.fasta": _FOUR_ROWS,
            "alignment_pruned_trimmed.fasta": _FOUR_ROWS,
        },
        input_info={"trimming_details": {"method": "trimal"}, "outgroup": None},
        pruned_tree=True,
        recomputed=True,
    )

    context = service.build_context(job_dir)

    assert context["alignment"]["source_file"] == "alignment_pruned_trimmed.fasta"
    assert context["alignment"]["alignment_is_tree_builder_input"] is True
    assert context["tree"]["rebuilt_by_recompute"] is True
    assert context["pipeline"]["tree_rebuilt_after_pruning"] is True
    assert context["alignment"]["trimming_measured_from"] == "alignment_pruned_aligned.fasta"


def test_a_realigned_alignment_is_not_called_a_trimmed_one(tmp_path):
    # alignment_pruned_aligned.fasta is the realigned-but-untrimmed set. Being
    # the file the review reads does not make it a trimming product, and the
    # difference between it and alignment_raw.fasta is realignment, not trimAl.
    job_dir = _provenance_job(
        tmp_path,
        files={
            "alignment_raw.fasta": dict(_FOUR_ROWS, E="ACGTTCGTAC"),
            "alignment_trimmed.fasta": dict(_FOUR_ROWS, E="ACGTTCGTAC"),
            "alignment_pruned_aligned.fasta": _FOUR_ROWS,
        },
        input_info={"trimming_details": {"method": "trimal"}, "outgroup": None},
        pruned_tree=True,
        recomputed=True,
    )

    alignment = service.build_context(job_dir)["alignment"]

    assert alignment["source_file"] == "alignment_pruned_aligned.fasta"
    assert alignment["alignment_is_trim_output"] is False
    assert "columns_removed_by_trimming" not in alignment


def test_builder_alignment_larger_than_the_displayed_tree_says_so(tmp_path):
    job_dir = _provenance_job(
        tmp_path,
        files={
            "alignment_raw.fasta": dict(_FOUR_ROWS, E="ACGTTCGTAC", F="ACGTTCGTAT"),
            "alignment_trimmed.fasta": dict(_FOUR_ROWS, E="ACGTTCGTAC", F="ACGTTCGTAT"),
        },
        input_info={"trimming_method": "none", "outgroup": None},
    )

    alignment = service.build_context(job_dir)["alignment"]

    assert alignment["sequences_in_builder_alignment"] == 6
    assert alignment["sequences"] == 4
    assert alignment["alignment_restricted_to_current_tips"] is True
    assert alignment["alignment_sequences_absent_from_current_tree"] == 2
    assert "4 of 6" in alignment["scope_note"]
    assert "do not present these numbers as what the tree builder saw" in (
        alignment["scope_note"].lower()
    )


def test_tree_tips_with_no_alignment_row_are_counted(tmp_path):
    job_dir = _provenance_job(
        tmp_path,
        files={"alignment_trimmed.fasta": {"A": "ACGTACGTAA", "B": "ACGTACGTAG",
                                           "C": "ACGTTCGTAA"}},
        input_info={"trimming_method": "none", "outgroup": None},
    )

    alignment = service.build_context(job_dir)["alignment"]

    # D is on the tree but not in the alignment file.
    assert alignment["tree_tips_unmatched_in_alignment"] == 1
    assert alignment["sequences_in_current_tree"] == 4
    assert alignment["alignment_names_matched_tree"] is True


def test_unrestricted_builder_alignment_says_nothing_was_recalculated(tmp_path):
    job_dir = _provenance_job(
        tmp_path,
        files={"alignment_trimmed.fasta": _FOUR_ROWS},
        input_info={"trimming_method": "none", "outgroup": None},
    )

    alignment = service.build_context(job_dir)["alignment"]

    assert alignment["alignment_restricted_to_current_tips"] is False
    assert "still in the tree" in alignment["scope_note"]


def test_suspect_tips_arrive_with_their_own_alignment_numbers(tmp_path):
    job_dir = _provenance_job(
        tmp_path,
        files={"alignment_trimmed.fasta": {
            "A": "---ACGTAC-",  # padded at both ends plus one interior gap
            "B": "ACGTACGTAG",
            "C": "ACGTTCGTAA",
            "D": "ACGTTCGTAN",
        }},
        input_info={"trimming_method": "none", "outgroup": None},
    )

    tree = service.build_context(job_dir)["tree"]
    rows = {row["name"]: row for row in tree["longest_terminal_branches"]}

    assert rows["A"]["ungapped_length"] == 6
    assert rows["A"]["terminal_gap_percent"] == 40.0
    assert rows["A"]["internal_gap_percent"] == 0.0
    assert rows["D"]["ambiguity_percent"] > 0
    # A and B sit under the supported node, so their parent's support is known.
    assert rows["A"]["parent_support"] == 90.0


def test_alrt_only_job_reaches_the_review_as_sh_alrt(tmp_path):
    job_dir = _provenance_job(
        tmp_path,
        files={"alignment_trimmed.fasta": _FOUR_ROWS},
        input_info={"trimming_method": "none", "tree_method": "iqtree",
                    "alrt_replicates": 1000, "bootstrap": 0, "outgroup": None},
    )
    _write(job_dir / "tree" / "tree_metadata.json",
           json.dumps({"method": "iqtree", "alrt_replicates": 1000, "bootstrap": 0}))

    context = service.build_context(job_dir)

    assert context["tree"]["support_type"] == "ALRT"
    assert context["pipeline"]["iqtree_support_mode"] == "sh_alrt_only"
    assert context["pipeline"]["tree_method_normalized"] == "iqtree"


def test_viewer_and_review_resolve_the_same_tree_method(tmp_path):
    job_dir = _provenance_job(
        tmp_path,
        files={"alignment_trimmed.fasta": _FOUR_ROWS},
        input_info={"trimming_method": "none", "tree_method": "raxml", "outgroup": None},
        pruned_tree=True,
        recomputed=True,
    )
    # The recompute rebuilt with a different builder than the original run.
    _write(job_dir / "tree" / "tree_pruned_metadata.json",
           json.dumps({"method": "iqtree", "alrt_replicates": 1000, "bootstrap": 0}))

    resolved = service.resolve_tree_support_context(job_dir)
    context = service.build_context(job_dir)

    assert resolved["tree_method"] == "iqtree"
    assert resolved["alrt_only"] is True
    assert context["pipeline"]["tree_method"] == "iqtree"
    assert context["tree"]["support_type"] == service._classify_support(
        [88.0], False, resolved["tree_method"], resolved["alrt_only"]
    )


# ---------------------------------------------------------------------------
# Response validation: a malformed reply is an upstream failure
# ---------------------------------------------------------------------------

def test_validation_failures_are_upstream_errors():
    review = _valid_review()
    review["overall_rating"] = "excellent"

    with pytest.raises(service.TreeAnalysisUpstreamError):
        service._validate_review(review)
    # ...and still caught by anything handling the base class.
    assert issubclass(service.TreeAnalysisUpstreamError, service.TreeAnalysisError)


def test_headline_longer_than_the_schema_allows_is_rejected():
    review = _valid_review()
    review["headline"] = "x" * (service.HEADLINE_MAX_CHARACTERS + 1)

    with pytest.raises(service.TreeAnalysisError, match="headline"):
        service._validate_review(review)


def test_headline_at_the_limit_is_accepted():
    review = _valid_review()
    review["headline"] = "x" * service.HEADLINE_MAX_CHARACTERS

    assert service._validate_review(review)["headline"]


def test_named_sequence_not_a_tip_is_dropped_from_the_review():
    review = _valid_review()
    review["sequences_to_inspect"] = [
        {"name": "Amanita muscaria KX1", "reason": "Longest terminal branch."}
    ]

    # Matching is the same loose normalization used to line tips up with FASTA
    # headers, so an underscore spelling still resolves.
    assert service._validate_review(review, {"Amanita_muscaria_KX1"})

    # A name this tree does not carry is dropped, not fatal: the review was
    # already paid for out of the daily allowance before the call, so voiding
    # it over one unusable row spent a review and rendered nothing.
    validated = service._validate_review(
        dict(review, sequences_to_inspect=[
            {"name": "Amanita muscaria KX1", "reason": "Longest terminal branch."}
        ]),
        {"Russula_emetica_KX2"},
    )
    assert validated["sequences_to_inspect"] == []


def test_a_strong_rating_may_not_carry_a_high_severity_concern():
    review = _valid_review()
    review["overall_rating"] = "strong"
    review["concerns"][0]["severity"] = "high"

    with pytest.raises(service.TreeAnalysisError, match="strong"):
        service._validate_review(review)


def test_an_unreliable_rating_needs_a_high_severity_reason():
    review = _valid_review()
    review["overall_rating"] = "unreliable"

    with pytest.raises(service.TreeAnalysisError, match="unreliable"):
        service._validate_review(review)

    review["concerns"].append(
        {"severity": "high", "title": "No signal", "detail": "12 informative columns."}
    )
    assert service._validate_review(review)["overall_rating"] == "unreliable"


def test_a_usable_rating_may_carry_a_localized_high_severity_concern():
    review = _valid_review()
    review["concerns"][0]["severity"] = "high"

    assert service._validate_review(review)["overall_rating"] == "usable"


def test_renamed_tips_are_acceptable_under_either_name(tmp_path):
    job_dir = _provenance_job(
        tmp_path,
        files={"alignment_trimmed.fasta": _FOUR_ROWS},
        input_info={"trimming_method": "none", "outgroup": None},
    )
    _write(
        job_dir / "tree_state.json",
        json.dumps({"root_mode": "MIDPOINT", "is_midpoint_rooted": True,
                    "renames": {"A": "Amanita muscaria AR001"}}),
    )

    displayed = set()
    context = service.build_context(job_dir, displayed_names_out=displayed)

    assert context["tree"]["viewer_renames_original_to_displayed"] == {
        "A": "Amanita muscaria AR001"
    }
    # The model is told to use the displayed name, but a review quoting the
    # Newick label is still naming a real tip.
    assert {"A", "Amanita muscaria AR001", "B", "C", "D"} <= displayed


def test_the_prompt_rename_cap_does_not_narrow_the_accepted_names(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "PROMPT_RENAME_LIMIT", 2)
    job_dir = _provenance_job(
        tmp_path,
        files={"alignment_trimmed.fasta": _FOUR_ROWS},
        input_info={"trimming_method": "none", "outgroup": None},
    )
    _write(
        job_dir / "tree_state.json",
        json.dumps({"root_mode": "MIDPOINT", "is_midpoint_rooted": True,
                    "renames": {"A": "one", "B": "two", "C": "three", "D": "four"}}),
    )

    displayed = set()
    context = service.build_context(job_dir, displayed_names_out=displayed)

    assert len(context["tree"]["viewer_renames_original_to_displayed"]) == 2
    # Every renamed tip is still a name a review may legitimately use.
    assert {"one", "two", "three", "four"} <= displayed
    assert service._validate_review(
        dict(_valid_review(),
             sequences_to_inspect=[{"name": "four", "reason": "Long branch."}]),
        displayed,
    )


# ---------------------------------------------------------------------------
# Topology digest, provenance and alignment excerpts
#
# These three are what the review is told about the tree beyond its aggregate
# numbers: which tips group together, where each sequence came from, and -- the
# one place raw residues are sent -- a narrow window around the worst interior
# gaps. Each has a failure mode that produces a confident, wrong review rather
# than an error, so they are pinned here.
# ---------------------------------------------------------------------------


def _newick_tree(text):
    """Parse a Newick string the way summarize_tree does."""
    import io

    from Bio import Phylo

    return Phylo.read(io.StringIO(text), "newick")


def test_topology_digest_reports_outermost_supported_clades():
    tree = _newick_tree("(((a:1,b:1)100:1,(c:1,d:1)100:1)40:1,(e:1,f:1)100:1);")
    names = service._tip_names_by_clade(tree)
    digest = service._topology_digest(tree, names, 70.0)

    assert digest["basis"] == "strong_support"
    assert {tuple(sorted(g["tip_names"])) for g in digest["groups"]} == {
        ("a", "b"), ("c", "d"), ("e", "f"),
    }
    assert digest["tips_not_in_any_listed_group"] == 0


def test_topology_digest_does_not_nest_a_supported_clade_inside_another():
    # The inner (a,b) is strongly supported but its parent already is, so
    # reporting both would state the same membership twice.
    tree = _newick_tree("(((a:1,b:1)100:1,c:1)99:1,(d:1,e:1)10:1);")
    digest = service._topology_digest(
        tree, service._tip_names_by_clade(tree), 70.0
    )

    assert len(digest["groups"]) == 1
    assert sorted(digest["groups"][0]["tip_names"]) == ["a", "b", "c"]
    # d and e are in no supported clade, and are reported as unplaced rather
    # than quietly folded into one.
    assert digest["tips_not_in_any_listed_group"] == 2


def test_an_oversized_supported_group_is_reopened(monkeypatch):
    """A tree whose deepest split is supported must not reduce to one group.

    This is the 2409-tip FastTree case from the job archive: every strongly
    supported clade nested inside one that held all but a handful of the tips,
    so the digest was true and useless.
    """
    monkeypatch.setattr(service, "MAX_CLADE_GROUPS", 6)
    tree = _newick_tree(
        "((((a:1,b:1)100:1,(c:1,d:1)100:1)100:1,((e:1,f:1)100:1,"
        "(g:1,h:1)100:1)100:1)100:1,z:1);"
    )
    digest = service._topology_digest(
        tree, service._tip_names_by_clade(tree), 70.0
    )

    assert len(digest["groups"]) > 1
    assert max(g["tips"] for g in digest["groups"]) < 8


def test_topology_digest_falls_back_to_shape_and_says_so():
    tree = _newick_tree("(((a:1,b:1)10:1,(c:1,d:1)12:1)8:1,(e:1,f:1)9:1);")
    digest = service._topology_digest(
        tree, service._tip_names_by_clade(tree), 70.0
    )

    assert digest["basis"] == "topology_only"
    assert "not supported groupings" in digest["definition"]
    # Nothing is claimed as supported on a tree where nothing is.
    assert digest["outermost_strongly_supported_clades_total"] == 0
    assert digest["tips_not_in_any_listed_group"] == 0


def test_a_dual_labelled_node_needs_both_halves_to_group_tips():
    # SH-aLRT 20 / UFBoot 99: the UFBoot half alone would call this supported,
    # which is exactly the disagreement the dual rule exists to catch.
    tree = _newick_tree("((a:1,b:1)20/99:1,(c:1,d:1)95/99:1);")
    digest = service._topology_digest(
        tree, service._tip_names_by_clade(tree), None
    )

    grouped = {name for g in digest["groups"] for name in g["tip_names"]}
    assert grouped == {"c", "d"}


def test_a_single_tip_tree_produces_no_groups():
    digest = service._topology_digest(
        _newick_tree("(a:1);"), {}, 70.0
    )
    assert digest["groups"] == []


def test_provenance_omits_blast_metrics_that_were_never_computed():
    index = service._provenance_index({
        "sequence_metadata": [
            {"name": "a", "source": "user", "identity": 0.0,
             "blast_metrics_available": False},
            {"name": "b", "source": "mycomap", "identity": 97.5,
             "query_cover": 100.0, "blast_metrics_available": True},
        ]
    })

    # A leftover 0.0 would read as a sequence sharing no identity with anything.
    assert "identity" not in index["a"]
    assert index["b"]["identity"] == 97.5


def test_provenance_is_indexed_under_every_spelling_of_the_name():
    index = service._provenance_index({
        "sequence_metadata": [{
            "name": "KY099600 Caliciopsis pinea",
            "fasta_header": "KY099600_Caliciopsis_pinea",
            "accession": "KY099600",
            "taxon": "Caliciopsis pinea",
        }]
    })

    for spelling in (
        "KY099600 Caliciopsis pinea", "KY099600_Caliciopsis_pinea", "KY099600"
    ):
        assert index[service._normalize_name(spelling)]["taxon"] == "Caliciopsis pinea"


def test_provenance_summary_describes_only_tips_still_in_the_tree():
    index = service._provenance_index({
        "sequence_metadata": [
            {"name": "a", "source": "user"},
            {"name": "b", "source": "mycomap"},
            {"name": "pruned", "source": "mycomap"},
        ]
    })
    summary = service._provenance_summary(index, ["a", "b"])

    assert summary["sequences_with_metadata"] == 2
    assert summary["by_source"] == {"user": 1, "mycomap": 1}


def test_provenance_summary_names_low_identity_references():
    index = service._provenance_index({
        "sequence_metadata": [
            {"name": "close", "identity": 99.4, "blast_metrics_available": True},
            {"name": "distant", "identity": 84.0, "taxon": "Amanita muscaria",
             "accession": "XX000001", "blast_metrics_available": True},
        ]
    })
    summary = service._provenance_summary(index, ["close", "distant"])

    assert summary["references_below_90_percent_identity_total"] == 1
    assert summary["references_below_90_percent_identity"][0]["identity"] == 84.0


def test_a_job_without_provenance_says_so_rather_than_inviting_a_guess():
    summary = service._provenance_summary({}, ["a", "b"])
    assert summary["sequences_with_metadata"] == 0
    assert "Do not infer it" in summary["note"]


@pytest.mark.parametrize(
    "label",
    ["Amanita", "Tyromyces sp. DLL2010", "Amanita cf. muscaria",
     "Environmental Sample", "Uncultured Sebacina", ""],
)
def test_labels_that_name_nothing_are_not_split_taxa(label):
    """Undetermined and placeholder labels in two clades mean nothing.

    Keying this on the genus reported, of a dataset that was entirely
    Tyromyces, that Tyromyces appeared in seven groups.
    """
    assert service._determinate_taxon(label) is None


def test_a_determinate_binomial_in_two_groups_is_reported():
    structure = {
        "basis": "strong_support",
        "groups": [
            {"id": "C1", "tip_names": ["a", "b"], "tip_names_truncated": False},
            {"id": "C2", "tip_names": ["c"], "tip_names_truncated": False},
        ],
    }
    index = {
        "a": {"taxon": "Cortinarius croceus"},
        "b": {"taxon": "Cortinarius thiersii"},
        "c": {"taxon": "Cortinarius croceus"},
    }
    split = service._labels_split_across_clades(structure, index)

    assert split == [
        {"taxon_label": "Cortinarius croceus", "groups": ["C1", "C2"],
         "group_count": 2}
    ]


def test_split_labels_are_not_computed_on_a_shape_only_digest():
    structure = {
        "basis": "topology_only",
        "groups": [
            {"id": "C1", "tip_names": ["a"], "tip_names_truncated": False},
            {"id": "C2", "tip_names": ["b"], "tip_names_truncated": False},
        ],
    }
    index = {"a": {"taxon": "Cortinarius croceus"},
             "b": {"taxon": "Cortinarius croceus"}}

    assert service._labels_split_across_clades(structure, index) == []


def test_split_labels_ignore_a_group_whose_members_were_truncated():
    # An absence from a sampled member list is not evidence of absence.
    structure = {
        "basis": "strong_support",
        "groups": [
            {"id": "C1", "tip_names": ["a"], "tip_names_truncated": True},
            {"id": "C2", "tip_names": ["b"], "tip_names_truncated": False},
        ],
    }
    index = {"a": {"taxon": "Cortinarius croceus"},
             "b": {"taxon": "Cortinarius croceus"}}

    assert service._labels_split_across_clades(structure, index) == []


def test_the_largest_internal_gap_ignores_terminal_padding():
    # 12 leading gaps, an interior run of 4, then 8 trailing.
    sequence = "------------ACGT----ACGT--------"
    assert service._largest_internal_gap_run(sequence) == (16, 20)


def test_a_row_that_is_all_gaps_has_no_internal_gap_run():
    assert service._largest_internal_gap_run("--------") is None


def test_an_excerpt_shows_the_flagged_row_against_clean_neighbours():
    clean = "ACGT" * 40
    flagged = clean[:60] + "-" * 20 + clean[80:]
    records = [
        ("flagged", flagged),
        ("clean1", clean),
        ("clean2", clean),
        ("clean3", clean),
    ]
    rows = [service._sequence_row(name, seq) for name, seq in records]
    excerpts = service.build_alignment_excerpt(records, rows)

    assert len(excerpts) == 1
    excerpt = excerpts[0]
    assert excerpt["flagged_sequence"] == "flagged"
    assert excerpt["largest_internal_gap_columns"] == 20
    assert [row["role"] for row in excerpt["rows"]][0] == "flagged"
    assert {row["role"] for row in excerpt["rows"][1:]} == {"contrast"}
    # Every row is the same window of the same alignment.
    assert len({len(row["residues"]) for row in excerpt["rows"]}) == 1
    assert excerpt["columns_shown"] <= service.EXCERPT_MAX_COLUMNS
    assert excerpt["first_column"] >= 1


def test_a_clean_alignment_produces_no_excerpt():
    clean = "ACGT" * 40
    records = [("a", clean), ("b", clean), ("c", clean)]
    rows = [service._sequence_row(name, seq) for name, seq in records]

    assert service.build_alignment_excerpt(records, rows) == []


def test_an_excerpt_window_is_capped_on_a_huge_deletion():
    clean = "ACGT" * 500
    flagged = clean[:100] + "-" * 1200 + clean[1300:]
    records = [("flagged", flagged)] + [
        (f"clean{i}", clean) for i in range(3)
    ]
    rows = [service._sequence_row(name, seq) for name, seq in records]
    excerpt = service.build_alignment_excerpt(records, rows)[0]

    assert excerpt["columns_shown"] <= service.EXCERPT_MAX_COLUMNS
    assert excerpt["columns_in_alignment"] == 2000


def test_excerpts_are_bounded_in_number_and_do_not_repeat_a_region():
    clean = "ACGT" * 100
    records = [("clean%d" % i, clean) for i in range(4)]
    # Five separate offenders, all with their gap in the same place.
    for i in range(5):
        records.append((f"flagged{i}", clean[:100] + "-" * 30 + clean[130:]))
    rows = [service._sequence_row(name, seq) for name, seq in records]
    excerpts = service.build_alignment_excerpt(records, rows)

    assert len(excerpts) == 1
    assert len(excerpts) <= service.EXCERPT_MAX_WINDOWS


def test_an_excerpt_is_dropped_when_no_neighbour_covers_the_window():
    # A single flagged row with nothing to compare it against says nothing
    # about register, which is the only judgement an excerpt exists to support.
    clean = "ACGT" * 40
    flagged = clean[:60] + "-" * 20 + clean[80:]
    padded = "-" * 160
    records = [("flagged", flagged), ("padded", padded)]
    rows = [service._sequence_row(name, seq) for name, seq in records]

    assert service.build_alignment_excerpt(records, rows) == []
