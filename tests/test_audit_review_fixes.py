"""Regressions for the defects found by the PR #7 / #8 audit reviews.

One file per review would spread twenty unrelated one-line fixes across the
suite; they are grouped here by the subsystem they belong to instead, and each
test names the wrong behaviour it exists to prevent coming back.
"""

import json
import logging
import re
import subprocess
import sys
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from Bio import Phylo
from Bio.Phylo.BaseTree import Clade, Tree
from flask import Flask, g

import app.services.tree_io as tree_io
from app.services.tree_edit_service import (
    _collapse_unifurcations,
    load_tree_state,
    prune_taxa,
)
from app.services.tree_io import (
    newick_file_to_nexus,
    tree_to_newick_string,
    write_tree_file,
)


# ---------------------------------------------------------------------------
# A1 -- collapsing a unifurcation must not create a node with BOTH a name and
#       a confidence. There is no Newick representation for that, and
#       Biopython concatenates the two into an invented label.
# ---------------------------------------------------------------------------

def _unary_over(child, *, parent_name=None, parent_confidence=None,
                parent_length=0.3):
    """A three-taxon tree whose left branch is a unifurcation over `child`."""
    parent = Clade(branch_length=parent_length)
    parent.name = parent_name
    parent.confidence = parent_confidence
    parent.clades = [child]
    root = Clade()
    root.clades = [parent, Clade(branch_length=0.4, name="C")]
    return Tree(root)


def _annotated_split(*, name=None, confidence=None, branch_length=0.2):
    clade = Clade(branch_length=branch_length)
    clade.name = name
    clade.confidence = confidence
    clade.clades = [Clade(branch_length=0.1, name="A"),
                    Clade(branch_length=0.1, name="B")]
    return clade


def test_a_collapsed_name_does_not_join_an_existing_child_confidence():
    child = _annotated_split(confidence=95)
    tree = _unary_over(child, parent_name="CladeQ")

    _collapse_unifurcations(tree)

    survivor = next(c for c in tree.get_nonterminals() if c is not tree.root)
    assert survivor.confidence == 95
    assert survivor.name is None, "the child's own annotation must win"
    # Branch lengths still accumulate through the removed node.
    assert survivor.branch_length == pytest.approx(0.5)
    # And the result is serializable, which is the whole point.
    assert "CladeQ" not in tree_to_newick_string(tree)


def test_a_collapsed_confidence_does_not_join_an_existing_child_name():
    # IQ-TREE's dual SH-aLRT/UFBoot label is parsed as an internal *name*.
    child = _annotated_split(name="82.7/87")
    tree = _unary_over(child, parent_confidence=88)

    _collapse_unifurcations(tree)

    survivor = next(c for c in tree.get_nonterminals() if c is not tree.root)
    assert survivor.name == "82.7/87"
    assert survivor.confidence is None
    assert survivor.branch_length == pytest.approx(0.5)
    assert "82.7/87" in tree_to_newick_string(tree)


@pytest.mark.parametrize(
    ("parent_kwargs", "field", "expected"),
    [
        ({"parent_confidence": 88}, "confidence", 88),
        ({"parent_name": "CladeQ"}, "name", "CladeQ"),
    ],
)
def test_an_unannotated_child_still_inherits_the_collapsed_annotation(
    parent_kwargs, field, expected
):
    """The reason the transfer exists at all must keep working."""
    child = _annotated_split()
    tree = _unary_over(child, **parent_kwargs)

    _collapse_unifurcations(tree)

    survivor = next(c for c in tree.get_nonterminals() if c is not tree.root)
    assert getattr(survivor, field) == expected
    assert survivor.branch_length == pytest.approx(0.5)


def test_root_promotion_follows_the_same_exclusivity_rule():
    child = _annotated_split(confidence=95, branch_length=0.2)
    root = Clade(branch_length=0.1)
    root.name = "RootLabel"
    root.clades = [child]
    tree = Tree(root)

    _collapse_unifurcations(tree)

    assert tree.root.confidence == 95
    assert tree.root.name is None
    assert tree.root.branch_length == pytest.approx(0.3)


def test_the_helper_never_leaves_a_survivor_holding_both():
    from app.services.tree_edit_service import _carry_collapsed_annotation

    for removed_name, removed_conf, child_name, child_conf in [
        ("P", None, None, 95), (None, 88, "82.7/87", None),
        ("P", None, "child", None), (None, 88, None, 95),
    ]:
        removed = Clade()
        removed.name, removed.confidence = removed_name, removed_conf
        survivor = Clade()
        survivor.name, survivor.confidence = child_name, child_conf

        _carry_collapsed_annotation(removed, survivor)

        assert not (survivor.name is not None and survivor.confidence is not None)


def test_pruning_into_a_unifurcation_writes_a_parsable_tree(tmp_path):
    """End to end: the prune that used to invent a label such as CladeQ95."""
    tree_dir = tmp_path / "tree"
    tree_dir.mkdir()
    (tree_dir / "tree_original.newick").write_text(
        "(((A:0.1,B:0.1)95:0.2,X:0.5)CladeQ:0.3,C:0.4,D:0.5);"
    )

    state = load_tree_state(tmp_path)
    prune_taxa(tmp_path, state, ["X"])

    written = (tree_dir / "tree_pruned.newick").read_text()
    assert "CladeQ95" not in written and "95CladeQ" not in written
    reloaded = Phylo.read(StringIO(written), "newick")
    assert {tip.name for tip in reloaded.get_terminals()} == {"A", "B", "C", "D"}


# ---------------------------------------------------------------------------
# A2 -- every Newick *file* write goes through the guarded serializer.
# ---------------------------------------------------------------------------

def _both_annotated_tree():
    tree = Phylo.read(StringIO("((A:0.1,B:0.1)CladeAB:0.2,C:0.3);"), "newick")
    internal = next(c for c in tree.get_nonterminals() if c.name == "CladeAB")
    internal.confidence = 95
    return tree


def test_writing_a_newick_file_refuses_an_ambiguous_internal_node(tmp_path):
    target = tmp_path / "tree_pruned.newick"

    with pytest.raises(ValueError, match="both a name and a confidence"):
        write_tree_file(_both_annotated_tree(), target, "newick")

    # Nothing half-written: the render happens before the path is touched.
    assert not target.exists()


def test_a_refused_newick_write_does_not_truncate_an_existing_file(tmp_path):
    target = tmp_path / "tree_pruned.newick"
    target.write_text("((A:0.1,B:0.1)95:0.2,C:0.3);\n")
    before = target.read_text()

    with pytest.raises(ValueError):
        write_tree_file(_both_annotated_tree(), target, "newick")

    assert target.read_text() == before


def test_the_guarded_writer_still_keeps_absent_lengths_absent(tmp_path):
    """Routing through the guard must not change branch-length behaviour."""
    tree = Phylo.read(StringIO("((A:0.523456789,B:0.000000006)90:0.02,C:0.0,D);"),
                      "newick")
    target = tmp_path / "tree.newick"
    write_tree_file(tree, target, "newick")

    lengths = {c.name: c.branch_length
               for c in Phylo.read(str(target), "newick").get_terminals()}
    assert lengths["B"] == pytest.approx(6e-9, rel=1e-6)
    assert lengths["C"] == 0.0
    assert lengths["D"] is None
    # File and in-memory string agree.
    assert target.read_text().strip() == tree_to_newick_string(tree).strip()


# ---------------------------------------------------------------------------
# A3 -- tree_io's failure path referenced a logger the module did not define.
# ---------------------------------------------------------------------------

def test_a_failed_nexus_conversion_returns_false_instead_of_raising(tmp_path):
    # Duplicate terminal labels make write_nexus_tree raise, which is the path
    # that reached `logger.error` -- and used to die with NameError instead.
    source = tmp_path / "tree.newick"
    source.write_text("(A:0.1,A:0.1);")

    assert newick_file_to_nexus(source, tmp_path / "tree.nexus") is False


def test_tree_io_has_a_module_logger():
    assert isinstance(tree_io.logger, logging.Logger)
    assert tree_io.logger.name == "app.services.tree_io"


# ---------------------------------------------------------------------------
# A4/A5 -- orientation correction: the flag is coerced, and it never touches
#          input that is already aligned.
# ---------------------------------------------------------------------------

REVERSED_FASTA = (
    ">forward\nGATCGATCGATCGATCGATCGATCGATCGATC\n"
    ">reverse\nGATCGATCGATCGATCGATCGATCGATCGATC\n"
)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, True),            # absent stays enabled
        (True, True),
        (False, False),
        ("false", False),        # bool("false") is True -- the actual defect
        ("0", False),
        ("true", True),
    ],
)
def test_fix_orientation_is_coerced_not_bool_cast(stored, expected):
    from app.workers.tasks import _resolve_orientation_plan

    params = {} if stored is None else {"fix_orientation": stored}
    requested, _effective = _resolve_orientation_plan(params)
    assert requested is expected


@pytest.mark.parametrize("fix_orientation", [True, False, "true", "false"])
def test_already_aligned_input_is_never_orientation_corrected(fix_orientation):
    from app.workers.tasks import _resolve_orientation_plan

    _requested, effective = _resolve_orientation_plan({
        "alignment_method": "none", "fix_orientation": fix_orientation,
    })
    assert effective is False


@pytest.mark.parametrize("method", ["mafft", "muscle", "clustalo", "iqtree_builtin"])
def test_ordinary_alignment_methods_still_get_orientation_correction(method):
    from app.workers.tasks import _resolve_orientation_plan

    _requested, effective = _resolve_orientation_plan({"alignment_method": method})
    assert effective is True


def test_the_default_alignment_method_resolves_before_the_orient_step():
    """"default" must resolve the same way in ORIENT as it does in ALIGN."""
    from app.config import Config
    from app.workers.tasks import _resolve_orientation_plan, _resolved_alignment_method

    with patch.object(Config, "BEGINNER_DEFAULT_ALIGNER", "none"):
        assert _resolved_alignment_method({"alignment_method": "default"}) == "none"
        _requested, effective = _resolve_orientation_plan({})
        assert effective is False

    with patch.object(Config, "BEGINNER_DEFAULT_ALIGNER", "mafft"):
        assert _resolved_alignment_method({}) == "mafft"
        assert _resolve_orientation_plan({})[1] is True


def test_the_orient_step_leaves_prealigned_bytes_byte_for_byte_identical(tmp_path):
    """The FASTA the pipeline hands to the aligner is unchanged, byte for byte.

    A reverse-complemented record in an existing alignment is still a column of
    that alignment; flipping it destroys the correspondence that makes the file
    an alignment at all.
    """
    from app.workers.tasks import _check_and_maybe_fix_orientation, _resolve_orientation_plan

    aligned = (
        ">its_forward\n" + "GGAAGTAAAAGTCGTAACAAGGTTTCCGTAGGTGAA" + "\n"
        ">its_reverse\n" + "TTCACCTACGGAAACCTTGTTACGACTTTTACTTCC" + "\n"
    )
    path = tmp_path / "input_raw.fasta"
    path.write_text(aligned)
    original = path.read_bytes()

    for fix_orientation in (True, False, "true", "false"):
        params = {"alignment_method": "none", "fix_orientation": fix_orientation}
        _requested, effective = _resolve_orientation_plan(params)
        _check_and_maybe_fix_orientation(path, effective)
        assert path.read_bytes() == original, fix_orientation


def test_recompute_with_alignment_none_copies_the_input_unchanged(tmp_path):
    """Recompute reaches the aligner directly; it must not flip anything either."""
    from app.config import Config
    from app.models import AlignmentParams
    from app.services import alignment_service

    source = tmp_path / "alignment_pruned.fasta"
    source.write_text(
        ">a\nACGTACGTAC\n>b\nGTACGTACGT\n"
    )
    original = source.read_bytes()
    output = tmp_path / "alignment_pruned_aligned.fasta"

    for fix_orientation in (True, False):
        with patch.object(alignment_service, "fix_direction_with_mafft") as flipper:
            alignment_service.run_alignment(
                source, output,
                AlignmentParams(method="none", fix_orientation=fix_orientation),
                Config, logging.getLogger("test"),
            )
        flipper.assert_not_called()
        assert source.read_bytes() == original
        assert output.read_bytes() == original


# ---------------------------------------------------------------------------
# A7 -- a legacy IQ-TREE bootstrap must not make an old job unrecomputable.
# ---------------------------------------------------------------------------

class LegacyIqtreeBootstrapTests(unittest.TestCase):
    """New values obey the current rule; inherited ones are normalized."""

    def _recompute(self, stored, body):
        from app.api import routes

        job_dir = Path(self.tmp) / "job-1"
        (job_dir).mkdir(parents=True, exist_ok=True)
        (job_dir / "input_info.json").write_text(json.dumps(stored))

        enqueued = {}

        def _enqueue(job_id, params_dict, **kwargs):
            enqueued["params"] = params_dict
            return (job_id, True) if kwargs.get("return_created") else job_id

        db_job = SimpleNamespace(id="job-1", metrics={}, status="completed")
        app = Flask(__name__)
        app.add_url_rule("/job/<job_id>", endpoint="main.job_status",
                         view_func=lambda job_id: "")
        with app.test_request_context(method="POST", json=body), \
                patch.object(routes, "check_job_access",
                             return_value=(db_job, None, None)), \
                patch.object(routes.Config, "JOB_DIR", Path(self.tmp)), \
                patch.object(routes, "enqueue_recompute_job", side_effect=_enqueue), \
                patch.object(routes, "db", MagicMock()), \
                patch.object(routes, "active_recompute_snapshot_mtime",
                             return_value=None):
            g.request_id = "test-request"
            response = routes.recompute_tree_job.__wrapped__("job-1")
        return response, enqueued

    def setUp(self):
        import tempfile, shutil
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    LEGACY = {"tree_method": "iqtree", "bootstrap": 500,
              "alignment_method": "mafft", "trimming_method": "none"}

    def test_an_inherited_legacy_count_is_normalized_not_rejected(self):
        response, enqueued = self._recompute(self.LEGACY, {"async": True})

        status = response[1] if isinstance(response, tuple) else response.status_code
        self.assertEqual(status, 202)
        self.assertEqual(enqueued["params"]["bootstrap"], 1000)

    def test_an_explicitly_requested_legacy_count_is_still_rejected(self):
        response, enqueued = self._recompute(self.LEGACY, {"bootstrap": 500})

        body, status = response
        self.assertEqual(status, 400)
        self.assertIn("at least 1000", body.get_json()["error"])
        self.assertEqual(enqueued, {})

    def test_explicitly_valid_counts_are_accepted(self):
        for value in (0, 1000):
            with self.subTest(value=value):
                response, enqueued = self._recompute(
                    self.LEGACY, {"bootstrap": value})
                status = (response[1] if isinstance(response, tuple)
                          else response.status_code)
                self.assertEqual(status, 202)
                self.assertEqual(enqueued["params"]["bootstrap"], value)

    def test_a_newly_requested_iqtree_configuration_is_validated(self):
        # Switching an old RAxML job to IQ-TREE is a new request, not an
        # inheritance, so its bootstrap has to be legal for IQ-TREE.
        stored = dict(self.LEGACY, tree_method="raxml")
        response, enqueued = self._recompute(stored, {"tree_method": "iqtree"})

        _body, status = response
        self.assertEqual(status, 400)
        self.assertEqual(enqueued, {})

    def test_corrupted_inherited_state_is_reported_not_silently_run_at_1000(self):
        """The normalizer's `except ValueError: return 1000` reached here.

        A stored value that is not an old-but-valid count is corruption, and
        turning it into 1000 replicates would quietly run a tree the user never
        asked for. It has to surface as an error the user can act on.
        """
        for stored_value in ("banana", -5, None, [1000], 12.5):
            with self.subTest(stored=stored_value):
                stored = dict(self.LEGACY, bootstrap=stored_value)
                response, enqueued = self._recompute(stored, {"async": True})

                body, status = response
                self.assertEqual(status, 400)
                self.assertIn("non-negative integer",
                              body.get_json()["error"])
                self.assertEqual(enqueued, {})

    def test_a_disabled_inherited_bootstrap_still_recomputes_as_disabled(self):
        stored = dict(self.LEGACY, bootstrap=0)
        response, enqueued = self._recompute(stored, {"async": True})

        status = response[1] if isinstance(response, tuple) else response.status_code
        self.assertEqual(status, 202)
        self.assertEqual(enqueued["params"]["bootstrap"], 0)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (0, 0),          # UFBoot disabled; still disabled.
        (1, 1000),       # The whole legacy range lifts to the supported floor.
        (500, 1000),
        (999, 1000),
        ("500", 1000),   # The strict validator accepts integral text; so does this.
        (1000, 1000),
        (5000, 5000),
        (5000.0, 5000),  # An integral float is an exact count.
    ],
)
def test_a_legacy_but_valid_inherited_count_is_lifted(stored, expected):
    from app.services.tree_parameter_validation import (
        normalize_inherited_iqtree_ufboot_count as normalize,
    )

    assert normalize("iqtree", stored) == expected


@pytest.mark.parametrize(
    "stored",
    [
        -1, -5, "banana", "", "1e3", "12.5", True, False, None, {}, [],
        float("nan"), float("inf"), float("-inf"), 2.5,
    ],
)
def test_corrupted_inherited_state_still_raises_instead_of_meaning_1000(stored):
    """The defect: `except ValueError: return 1000`.

    Wrapping the strict validator turned every unusable stored value into a
    silent request for 1000 replicates -- a scientifically meaningful
    instruction the user never gave, generated from corruption. Only the
    old-but-valid 1-999 range is compatibility; everything else is an error.
    """
    from app.services.tree_parameter_validation import (
        normalize_inherited_iqtree_ufboot_count as normalize,
    )

    with pytest.raises(ValueError):
        normalize("iqtree", stored)


@pytest.mark.parametrize("stored", [500, -1, "banana", None, float("nan")])
def test_a_non_iqtree_bootstrap_is_never_touched_by_the_normalizer(stored):
    # RAxML stores the same field with different semantics, so neither the
    # lift nor the rejection applies to it.
    from app.services.tree_parameter_validation import (
        normalize_inherited_iqtree_ufboot_count as normalize,
    )

    assert normalize("raxml", stored) is stored


def test_the_normalizer_and_the_validator_agree_wherever_both_succeed():
    from app.services.tree_parameter_validation import (
        normalize_inherited_iqtree_ufboot_count as normalize,
        validate_iqtree_ufboot_count as validate,
    )

    for value in (0, 1000, 5000, "0", "2000"):
        assert normalize("iqtree", value) == validate("iqtree", value)
    # And they part company only over 1-999, which is the entire compatibility
    # surface this helper exists to provide.
    for value in (1, 500, 999):
        with pytest.raises(ValueError):
            validate("iqtree", value)
        assert normalize("iqtree", value) == 1000


# ---------------------------------------------------------------------------
# B9/B10 -- one timeout parser, and bounded Redis commands in idempotency.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [None, "", "abc", float("nan"), float("inf"), float("-inf"), 0, -1, True, {}],
)
def test_unusable_timeout_configuration_falls_back_to_the_default(value):
    from app.services.subprocess_utils import (
        resolve_positive_number, resolve_time_limit_hours,
    )

    assert resolve_positive_number(value, 8.0) == 8.0
    assert resolve_time_limit_hours(value, 8.0) == 8.0


@pytest.mark.parametrize(("value", "expected"), [(6, 6.0), ("6", 6.0), (0.5, 0.5)])
def test_usable_timeout_configuration_is_honoured(value, expected):
    from app.services.subprocess_utils import resolve_positive_number

    assert resolve_positive_number(value, 8.0) == expected


@pytest.mark.parametrize(
    ("attr", "tool"),
    [("MAFFT_TIME_LIMIT_HOURS", "MAFFT"), ("TRIMAL_TIME_LIMIT_HOURS", "trimAl")],
)
def test_a_non_finite_tool_limit_cannot_reach_the_subprocess_runner(attr, tool):
    from app.config import Config
    from app.services.subprocess_utils import (
        configured_tool_time_limit_hours, configured_tool_timeout_seconds,
    )

    with patch.object(Config, attr, float("inf")):
        hours = configured_tool_time_limit_hours(Config, tool)
        assert hours == pytest.approx(
            {"MAFFT": 8.0, "trimAl": 4.0}[tool]
        )
        # int(inf * 3600) raises OverflowError; the fallback prevents it.
        assert configured_tool_timeout_seconds(Config, tool) > 0


# The environment boundary itself. `resolve_positive_number` only ever sees a
# value that survived Config import, and every one of these settings used to be
# defined as `float(os.environ.get(...))` -- so `TRIMAL_TIME_LIMIT_HOURS=abc`
# raised ValueError while importing app.config and the resolver never ran. These
# tests read the attribute out of a *freshly imported* Config in a subprocess
# with the variable set, which is the only way to cover that.

_TIMEOUT_ENV_DEFAULTS = {
    "GENERAL_JOB_TIME_LIMIT_HOURS": 8.0,
    "MAFFT_TIME_LIMIT_HOURS": 8.0,
    "MUSCLE_TIME_LIMIT_HOURS": 8.0,
    "CLUSTALO_TIME_LIMIT_HOURS": 8.0,
    "IQTREE_ALIGNMENT_TIME_LIMIT_HOURS": 8.0,
    "TRIMAL_TIME_LIMIT_HOURS": 4.0,
    "BMGE_TIME_LIMIT_HOURS": 4.0,
    "RAXML_TIME_LIMIT_HOURS": 15.0,
    "IQTREE_TIME_LIMIT_HOURS": 15.0,
    "MRBAYES_TIME_LIMIT_HOURS": 15.0,
    "FASTTREE_TIME_LIMIT_HOURS": 6.0,
    "IDEMPOTENCY_REDIS_TIMEOUT_SECONDS": 5.0,
    "EVENT_REDIS_CONNECT_TIMEOUT_SECONDS": 2.0,
    "EVENT_REDIS_SOCKET_TIMEOUT_SECONDS": 2.0,
    "CLAUDE_REVIEW_TIMEOUT_SECONDS": 240.0,
}


def _config_timeouts(env):
    """Import app.config in a clean process and read the timeout attributes."""
    import os as _os

    child_env = dict(_os.environ)
    # A stale .env or inherited value would mask what the test is setting.
    for name in _TIMEOUT_ENV_DEFAULTS:
        child_env.pop(name, None)
    child_env.update({k: str(v) for k, v in env.items()})
    child_env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)

    program = (
        "import json;from app.config import Config;"
        "print(json.dumps({name: getattr(Config, name) for name in "
        + repr(sorted(_TIMEOUT_ENV_DEFAULTS))
        + "}))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", program], env=child_env,
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            "importing Config exceeded the 60-second subprocess timeout; "
            "stdout={!r} stderr={!r}".format(exc.stdout, exc.stderr)
        ) from exc
    assert result.returncode == 0, (
        "importing Config must not be fatal for a malformed timeout: "
        + result.stderr[-2000:]
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_timeout_configuration_defaults_are_what_the_docs_claim():
    assert _config_timeouts({}) == _TIMEOUT_ENV_DEFAULTS


@pytest.mark.parametrize(
    "raw", ["abc", "", "   ", "8h", "nan", "inf", "-inf", "0", "-4", "1,5"],
)
def test_a_malformed_timeout_environment_value_is_not_fatal_at_import(raw):
    """The enforced policy: fall back, do not fail startup.

    Gunicorn does not import the application until the first request arrives,
    so a ValueError here produced a unit that restarted "successfully" and then
    500ed on every request. Dikarya's other environment readers (`bool_env`,
    `budget_env`) already fall back to their documented default for an
    unusable value; timeouts now do the same.
    """
    env = {name: raw for name in _TIMEOUT_ENV_DEFAULTS}
    assert _config_timeouts(env) == _TIMEOUT_ENV_DEFAULTS


def test_a_usable_timeout_environment_value_is_still_honoured():
    values = {name: str(index + 1)
              for index, name in enumerate(sorted(_TIMEOUT_ENV_DEFAULTS))}
    resolved = _config_timeouts(values)
    assert resolved == {name: float(value) for name, value in values.items()}
    # Fractional hours are a legitimate operator choice.
    assert _config_timeouts({"TRIMAL_TIME_LIMIT_HOURS": "0.25"})[
        "TRIMAL_TIME_LIMIT_HOURS"] == 0.25


def test_the_config_reader_and_the_runtime_resolver_agree():
    """Two parsers, one policy. They must not disagree about a value."""
    import os as _os

    from app.config import timeout_env
    from app.services.subprocess_utils import resolve_positive_number

    cases = ["abc", "", "nan", "inf", "-inf", "0", "-4", "6", "0.5", "1e3"]
    for raw in cases:
        with patch.dict(_os.environ, {"DIKARYA_TIMEOUT_PROBE": raw}):
            from_env = timeout_env("DIKARYA_TIMEOUT_PROBE", 8.0)
        assert from_env == resolve_positive_number(raw, 8.0), raw
    # An unset variable is the same as an unusable one: the default.
    _os.environ.pop("DIKARYA_TIMEOUT_PROBE", None)
    assert timeout_env("DIKARYA_TIMEOUT_PROBE", 8.0) == 8.0


def test_a_malformed_trimal_limit_still_produces_a_usable_rq_deadline():
    """End to end: the config boundary through to the enqueue budget."""
    resolved = _config_timeouts({"TRIMAL_TIME_LIMIT_HOURS": "abc",
                                 "GENERAL_JOB_TIME_LIMIT_HOURS": "nan"})
    from app.config import Config
    from app.workers.queue import resolve_job_timeout

    with patch.multiple(
        Config,
        GENERAL_JOB_TIME_LIMIT_HOURS=resolved["GENERAL_JOB_TIME_LIMIT_HOURS"],
        TRIMAL_TIME_LIMIT_HOURS=resolved["TRIMAL_TIME_LIMIT_HOURS"],
        RAXML_TIME_LIMIT_HOURS=15.0,
        MAFFT_TIME_LIMIT_HOURS=8.0,
        BEGINNER_DEFAULT_ALIGNER="mafft",
        DEFAULT_TRIMMING_METHOD="trimal_gappy",
    ):
        assert resolve_job_timeout({"tree_method": "raxml"}) == (
            f"{int((8 + 8 + 4 + 15) * 3600) + 600}s"
        )


def test_idempotency_redis_bounds_command_round_trips():
    from app.api_v1 import idempotency

    captured = {}

    def _from_url(url, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    with patch("redis.from_url", side_effect=_from_url):
        idempotency._redis()

    assert captured["socket_connect_timeout"] > 0
    # The defect: SET/GET/SETEX on a request path with no read ceiling at all.
    assert captured["socket_timeout"] > 0


def test_idempotency_redis_timeout_falls_back_on_bad_configuration():
    from app.api_v1 import idempotency
    from app.config import Config

    captured = {}
    with patch.object(Config, "IDEMPOTENCY_REDIS_TIMEOUT_SECONDS", float("inf")), \
            patch("redis.from_url", side_effect=lambda url, **kw: captured.update(kw)):
        idempotency._redis()

    assert captured["socket_timeout"] == (
        idempotency.DEFAULT_REDIS_COMMAND_TIMEOUT_SECONDS
    )


# ---------------------------------------------------------------------------
# C13 -- the NCBI fallback FASTA fetch.
# ---------------------------------------------------------------------------

class FallbackFastaRecoveryTests(unittest.TestCase):
    ACCESSIONS = ["OR800001", "OR800002"]

    def _fetch(self, response=None, exception=None):
        from app.services import blast_service

        reported = []

        def _request(method, url, **kwargs):
            if exception is not None:
                raise exception
            return response

        with patch.object(blast_service, "_fetch_genbank_xml_batch", return_value=[]), \
                patch.object(blast_service, "_ncbi_request", side_effect=_request), \
                patch.object(
                    blast_service, "_report_unresolved_accessions",
                    side_effect=lambda failed, recovered: reported.append(
                        (list(failed), recovered))):
            text = blast_service.fetch_fasta_for_accessions(self.ACCESSIONS)
        return text, reported

    def test_a_nonempty_fallback_is_a_recovery(self):
        body = "".join(f">{acc}.1 Fungus\nACGT\n" for acc in self.ACCESSIONS)
        text, reported = self._fetch(SimpleNamespace(status_code=200, text=body))

        self.assertIn("OR800001", text)
        self.assertEqual(reported, [])

    def test_an_empty_two_hundred_is_not_a_recovery(self):
        # The defect: `continue` on every 200 dropped these accessions out of
        # the output *and* out of the unresolved report.
        text, reported = self._fetch(SimpleNamespace(status_code=200, text="   \n"))

        self.assertEqual(text.strip(), "")
        self.assertEqual(reported, [(self.ACCESSIONS, 0)])

    def test_a_non_two_hundred_is_reported(self):
        _text, reported = self._fetch(SimpleNamespace(status_code=502, text="oops"))

        self.assertEqual(reported, [(self.ACCESSIONS, 0)])

    def test_a_transport_exception_is_reported(self):
        _text, reported = self._fetch(exception=RuntimeError("connection reset"))

        self.assertEqual(reported, [(self.ACCESSIONS, 0)])

    def test_a_partial_chunk_reports_only_what_was_missing(self):
        body = ">OR800001.1 Fungus\nACGT\n"
        text, reported = self._fetch(SimpleNamespace(status_code=200, text=body))

        self.assertIn("OR800001", text)
        # The recovered count is this chunk's own, not len(final_lines).
        self.assertEqual(reported, [(["OR800002"], 1)])

    def test_a_bare_accession_matches_the_version_ncbi_returned(self):
        body = "".join(f">{acc}.2 Fungus\nACGT\n" for acc in self.ACCESSIONS)
        _text, reported = self._fetch(SimpleNamespace(status_code=200, text=body))

        self.assertEqual(reported, [])


# ---------------------------------------------------------------------------
# C12 -- GenBank location classification (cache semantics).
# ---------------------------------------------------------------------------

class GenBankCachedEmptyLocationTests(unittest.TestCase):
    def setUp(self):
        from app.services import genbank_location_service as svc
        self.svc = svc
        svc._location_cache.clear()
        self.addCleanup(svc._location_cache.clear)

    def _lookup(self, accessions, parsed):
        with patch.object(self.svc, "_fetch_annotation_xml", return_value="<xml/>"), \
                patch.object(self.svc, "_parse_genbank_xml", return_value=parsed):
            return self.svc.lookup_locations(accessions)

    def test_a_cached_empty_location_still_classifies_as_missing(self):
        parsed = {"by_acc": {"OR807397": {"accession": "OR807397",
                                          "version": "OR807397.1",
                                          "source_features": {}}}}
        self._lookup(["OR807397"], parsed)
        # Second call: the empty answer is now cached, so nothing is fetched.
        with patch.object(self.svc, "_fetch_annotation_xml") as fetch:
            locations, missing, unavailable = self.svc.lookup_locations(["OR807397"])
        fetch.assert_not_called()

        self.assertEqual(locations, {})
        self.assertEqual(missing, ["OR807397"])
        self.assertEqual(unavailable, [])

    def test_every_requested_accession_lands_in_exactly_one_bucket(self):
        parsed = {"by_acc": {
            "OR807397": {"accession": "OR807397", "version": "OR807397.1",
                         "source_features": {"geo_loc_name": "USA: Arizona"}},
            "OR807398": {"accession": "OR807398", "version": "OR807398.1",
                         "source_features": {}},
        }}
        requested = ["OR807397", "OR807398", "OR807399"]
        locations, missing, unavailable = self._lookup(requested, parsed)

        classified = set(missing) | set(unavailable) | {
            acc for acc in requested if acc in locations
        }
        self.assertEqual(classified, set(requested))
        self.assertEqual(sorted(missing), ["OR807398", "OR807399"])

    def test_a_version_alias_is_resolved_before_being_called_missing(self):
        parsed = {"by_acc": {"OR807397": {
            "accession": "OR807397", "version": "OR807397.1",
            "source_features": {"geo_loc_name": "USA: Arizona"}}}}
        locations, missing, unavailable = self._lookup(["OR807397.2"], parsed)

        self.assertEqual(locations["OR807397.2"], "USA: Arizona")
        self.assertEqual((missing, unavailable), ([], []))

    def test_a_failed_batch_is_still_unavailable_not_missing(self):
        with patch.object(self.svc, "_fetch_annotation_xml", return_value=None):
            locations, missing, unavailable = self.svc.lookup_locations(["OR807397"])

        self.assertEqual((locations, missing), ({}, []))
        self.assertEqual(unavailable, ["OR807397"])


# ---------------------------------------------------------------------------
# E16 -- every pre-auth rate-limit bucket is charged.
# ---------------------------------------------------------------------------

class PreAuthRateLimitTests(unittest.TestCase):
    def test_the_hour_bucket_is_charged_even_when_the_minute_bucket_refuses(self):
        from app.api_v1 import auth

        hits = []

        class _Strategy:
            def hit(self, item, *identifiers):
                hits.append(item)
                return False  # the first bucket refuses

        limiter = SimpleNamespace(limiter=_Strategy())
        app = Flask(__name__)
        with app.test_request_context(), \
                patch.dict("sys.modules"), \
                patch("app.extensions.limiter", limiter):
            allowed = auth._pre_auth_lookup_allowed()

        self.assertFalse(allowed)
        self.assertEqual(len(hits), len(auth._pre_auth_lookup_limit_items()))

    def test_a_broken_limiter_backend_fails_open_and_is_logged(self):
        from app.api_v1 import auth

        class _Strategy:
            def hit(self, item, *identifiers):
                raise RuntimeError("redis down")

        limiter = SimpleNamespace(limiter=_Strategy())
        app = Flask(__name__)
        with app.test_request_context(), \
                patch("app.extensions.limiter", limiter), \
                patch.object(auth.logger, "warning") as warn:
            self.assertTrue(auth._pre_auth_lookup_allowed())

        self.assertTrue(warn.called)
        self.assertIn("pre_auth_lookup_limiter_unavailable", warn.call_args[0])


# ---------------------------------------------------------------------------
# E17 -- v1 serialized booleans match what OpenAPI promises.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (True, True), (False, False),
        ("false", False), ("true", True), ("0", False), ("1", True),
    ],
)
@pytest.mark.parametrize("field", ["trim_terminal_overhangs", "fix_orientation"])
def test_v1_job_params_serialize_booleans_as_booleans(stored, expected, field):
    from app.api_v1 import jobs

    job = SimpleNamespace(
        id="job-1", status="completed", created_at=None, updated_at=None,
        input_type="pasted_sequence", metrics={},
    )
    with patch.object(jobs, "_load_input_info", return_value={field: stored}), \
            patch.object(jobs, "url_for", side_effect=lambda *a, **kw: "/x"):
        params = jobs.serialize_job(job)["params"]

    assert params[field] is expected


@pytest.mark.parametrize("field", ["trim_terminal_overhangs", "fix_orientation"])
def test_v1_job_params_keep_null_for_a_legacy_job(field):
    from app.api_v1 import jobs

    job = SimpleNamespace(
        id="job-1", status="completed", created_at=None, updated_at=None,
        input_type="pasted_sequence", metrics={},
    )
    with patch.object(jobs, "_load_input_info", return_value={}), \
            patch.object(jobs, "url_for", side_effect=lambda *a, **kw: "/x"):
        params = jobs.serialize_job(job)["params"]

    assert params[field] is None


@pytest.mark.parametrize("field", ["trim_terminal_overhangs", "fix_orientation"])
def test_v1_job_params_fall_back_to_metrics(field):
    from app.api_v1 import jobs

    job = SimpleNamespace(
        id="job-1", status="completed", created_at=None, updated_at=None,
        input_type="pasted_sequence", metrics={field: "false"},
    )
    with patch.object(jobs, "_load_input_info", return_value={}), \
            patch.object(jobs, "url_for", side_effect=lambda *a, **kw: "/x"):
        params = jobs.serialize_job(job)["params"]

    assert params[field] is False


# ---------------------------------------------------------------------------
# E19 -- recording an enqueue failure must not break the error path.
# ---------------------------------------------------------------------------

class RebuildEnqueueFailureTests(unittest.TestCase):
    """Drive the real `rebuild_with_duplicates` through its double failure.

    The earlier coverage grepped app/api/routes.py for "db.session.rollback()"
    and separately re-enacted the control flow in the test. Both can pass while
    the route itself is broken, so this exercises the production function and
    asserts on what it does.

    The scenario: the new job row commits, `enqueue_job` raises, recording the
    "failed" status raises on its own commit, and the session must be rolled
    back so the outer handler can still use it -- with the ORIGINAL enqueue
    error, not PendingRollbackError and not the bookkeeping failure, reaching
    the error handler.
    """

    SOURCE_PARAMS = {
        "input_type": "sequence",
        "tree_method": "raxml",
        "alignment_method": "mafft",
        "trimming_method": "none",
        "sequence": ">A\nACGT\n",
        "import_filter_details": {
            "duplicates": {
                "removed_records": [
                    {"name": "B dup", "sequence": "ACGTT",
                     "metadata": {"name": "B dup"}},
                ],
            },
        },
    }

    def setUp(self):
        import shutil
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "job-1").mkdir()
        (self.tmp / "job-1" / "input_info.json").write_text(
            json.dumps(self.SOURCE_PARAMS)
        )

    def _run(self, *, enqueue_error, failure_commit_error=None):
        from app.api import routes

        commits = []
        record = SimpleNamespace(status="queued", metrics={}, user_id=None)

        class _Session:
            def __init__(self):
                self.rollbacks = 0

            def add(self, obj):
                commits.append("add")

            def commit(self):
                commits.append("commit")
                if (failure_commit_error is not None
                        and commits.count("commit") == 2):
                    raise failure_commit_error

            def rollback(self):
                self.rollbacks += 1
                commits.append("rollback")

        session = _Session()
        db = SimpleNamespace(session=session)
        handled = {}

        def _server_error(exc, where=""):
            handled["exception"] = exc
            return ({"status": "error"}, 500)

        def _job(**kwargs):
            record.__dict__.update(kwargs)
            return record

        db_job = SimpleNamespace(id="job-1", user_id=7, status="completed")
        app = Flask(__name__)
        with app.test_request_context(method="POST", json={}), \
                patch.object(routes, "check_job_access",
                             return_value=(db_job, None, None)), \
                patch.object(routes.Config, "JOB_DIR", self.tmp), \
                patch.object(routes, "Job", _job), \
                patch.object(routes, "db", db), \
                patch.object(routes, "prepare_phylo_job_params",
                             side_effect=lambda params: None), \
                patch.object(routes, "enqueue_job",
                             side_effect=enqueue_error), \
                patch.object(routes, "_server_error",
                             side_effect=_server_error):
            g.request_id = "test-request"
            response = routes.rebuild_with_duplicates.__wrapped__("job-1")
        return SimpleNamespace(response=response, commits=commits,
                               session=session, record=record,
                               handled=handled)

    def test_a_failing_failure_record_is_rolled_back_and_reraises_the_original(self):
        original = RuntimeError("redis is down")
        bookkeeping = RuntimeError("session is dirty")

        result = self._run(enqueue_error=original,
                           failure_commit_error=bookkeeping)

        # The row was committed, the failure record was attempted, and its own
        # commit failure was rolled back rather than left poisoning the session.
        self.assertEqual(result.commits, ["add", "commit", "commit", "rollback"])
        self.assertEqual(result.session.rollbacks, 1)
        # The enqueue error is what the user's error handler sees -- not
        # PendingRollbackError and not the bookkeeping failure.
        self.assertIs(result.handled["exception"], original)
        self.assertEqual(result.response[1], 500)

    def test_a_recordable_enqueue_failure_marks_the_row_failed(self):
        original = RuntimeError("redis is down")

        result = self._run(enqueue_error=original)

        # Second commit succeeds here, so nothing is rolled back...
        self.assertEqual(result.commits, ["add", "commit", "commit"])
        self.assertEqual(result.session.rollbacks, 0)
        # ... the row records why it will never run ...
        self.assertEqual(result.record.status, "failed")
        self.assertEqual(result.record.metrics["enqueue_error"], "RuntimeError")
        self.assertIn("never started", result.record.metrics["error"])
        # ... and the original failure still reaches the handler.
        self.assertIs(result.handled["exception"], original)

    def test_a_successful_enqueue_is_untouched_by_any_of_this(self):
        enqueued = {}

        def _enqueue(job_params, job_id=None, prepare=True):
            enqueued["job_id"] = job_id
            enqueued["sequences"] = job_params["sequence"].count(">")

        result = self._run(enqueue_error=_enqueue)

        body, status = result.response
        self.assertEqual(status, 202)
        payload = body.get_json()
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["restored_count"], 1)
        self.assertEqual(payload["job_id"], enqueued["job_id"])
        # The restored duplicate really is in the submitted FASTA.
        self.assertEqual(enqueued["sequences"], 2)
        self.assertEqual(result.record.status, "queued")
        self.assertEqual(result.session.rollbacks, 0)
        # Nothing reached the error handler.
        self.assertEqual(result.handled, {})


# ---------------------------------------------------------------------------
# E20 -- HTTPError responses are closed.
# ---------------------------------------------------------------------------

def test_an_upstream_oauth_error_response_is_closed():
    import urllib.error

    from app.services import inaturalist_oauth_service as svc

    closed = []

    class _Error(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("https://example.invalid/token", 502,
                             "bad gateway", {}, None)

        def read(self):
            return b'{"error":"invalid_grant","client_secret":"s3cret"}'

        def close(self):
            closed.append(True)

    error = _Error()
    with patch.object(svc.logger, "warning") as warn:
        svc._log_upstream_error("POST", "https://example.invalid/token", error)

    assert closed == [True]
    # Sanitization is unchanged: no body content reaches the log.
    logged = " ".join(str(part) for part in warn.call_args[0])
    assert "s3cret" not in logged and "invalid_grant" not in logged


def test_a_body_that_cannot_be_read_is_still_closed():
    import urllib.error

    from app.services import inaturalist_oauth_service as svc

    closed = []

    class _Error(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("https://example.invalid/token", 500,
                             "boom", {}, None)

        def read(self):
            raise OSError("connection reset")

        def close(self):
            closed.append(True)

    with patch.object(svc.logger, "warning"):
        svc._log_upstream_error("POST", "https://example.invalid/token", _Error())

    assert closed == [True]


# ---------------------------------------------------------------------------
# E21/E22 -- the log digest checkpoint and timezone-independent timestamps.
# ---------------------------------------------------------------------------

def _digest_module():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "dikarya_log_digest.py"
    spec = importlib.util.spec_from_file_location("dikarya_log_digest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "content", ["{ not json", json.dumps({"version": 1}), ""],
)
def test_mark_reviewed_can_replace_an_unreadable_checkpoint(tmp_path, content):
    digest = _digest_module()
    path = tmp_path / ".log-review-checkpoint.json"
    path.write_text(content)

    # Reads stay strict...
    with pytest.raises(ValueError):
        digest.read_review_checkpoint(path)

    # ...but the documented recovery operation can actually recover.
    boundary = digest.parse_window_timestamp("2026-08-22T18:03:00Z")
    digest.write_review_checkpoint(path, boundary)

    assert digest.read_review_checkpoint(path) == boundary


def test_a_valid_checkpoint_still_cannot_move_backwards(tmp_path):
    digest = _digest_module()
    path = tmp_path / ".log-review-checkpoint.json"
    digest.write_review_checkpoint(
        path, digest.parse_window_timestamp("2026-08-22T18:03:00Z"))

    with pytest.raises(ValueError, match="backwards"):
        digest.write_review_checkpoint(
            path, digest.parse_window_timestamp("2026-08-21T18:03:00Z"))


def test_a_valid_checkpoint_moves_forwards(tmp_path):
    digest = _digest_module()
    path = tmp_path / ".log-review-checkpoint.json"
    digest.write_review_checkpoint(
        path, digest.parse_window_timestamp("2026-08-21T18:03:00Z"))
    later = digest.parse_window_timestamp("2026-08-22T18:03:00Z")
    digest.write_review_checkpoint(path, later)

    assert digest.read_review_checkpoint(path) == later


def test_log_timestamps_are_utc_whatever_tz_the_process_runs_in():
    """A log line's timestamp must mean what the digest reads it as.

    The digest treats a timestamp carrying no offset as UTC. logging.Formatter
    defaults to time.localtime, so under a non-UTC TZ every emitted line was
    offset from what every reader assumed.
    """
    script = (
        "import logging, time, os\n"
        "os.environ['TZ'] = 'America/Denver'\n"
        "time.tzset()\n"
        "from app.services.log_context import ContextFormatter, utc_formatter\n"
        "record = logging.LogRecord('n', logging.INFO, 'p', 1, 'm', None, None)\n"
        "record.created = 1_756_000_000.0\n"
        "for formatter in (ContextFormatter('%(asctime)s'), utc_formatter('%(asctime)s')):\n"
        "    print(formatter.format(record))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    import time as _time

    expected = _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime(1_756_000_000.0))
    for line in proc.stdout.strip().splitlines():
        assert line.startswith(expected), (line, expected)


def test_worker_staleness_classification_is_unaffected_by_a_non_utc_tz():
    """The digest's own age arithmetic must not move with the local zone."""
    script = (
        "import json, os, time, importlib.util, sys\n"
        "os.environ['TZ'] = 'America/Denver'\n"
        "time.tzset()\n"
        "sys.path.insert(0, '.')\n"
        "spec = importlib.util.spec_from_file_location('d', 'scripts/dikarya_log_digest.py')\n"
        "d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)\n"
        "now = d.parse_window_timestamp('2026-08-22T18:00:00Z')\n"
        "then = d.parse_window_timestamp('2026-08-22T12:00:00Z')\n"
        "print(json.dumps({'age_hours': (now - then).total_seconds() / 3600}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["age_hours"] == 6.0


# ---------------------------------------------------------------------------
# F23 -- a custom voucher label never runs off the sheet.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("width", "height", "margin_left", "margin_top"),
    [
        (8.5, 1.0, 4.0, 0.5),     # wider than the printable extent
        (2.625, 5.0, 0.25, 8.0),  # taller than the printable extent
        (8.5, 11.0, 4.0, 4.0),    # both
    ],
)
def test_an_oversized_custom_label_is_clamped_to_the_page(
    width, height, margin_left, margin_top
):
    from app.main import routes

    layout = routes._voucher_layout_from_form({
        "label_size": "custom",
        "custom_width": str(width), "custom_height": str(height),
        "custom_columns": "3", "custom_rows": "10",
        "custom_margin_left": str(margin_left),
        "custom_margin_top": str(margin_top),
        "custom_gap_x": "0", "custom_gap_y": "0",
    })
    preset = layout["preset"]

    assert preset["margin_left"] + preset["label_width"] <= 8.5 + 1e-9
    assert preset["margin_top"] + preset["label_height"] <= 11 + 1e-9
    # The grid still holds at least one cell, and it fits.
    assert preset["columns"] >= 1 and preset["rows"] >= 1
    used_width = (preset["margin_left"] + preset["columns"] * preset["label_width"]
                  + (preset["columns"] - 1) * preset["gap_x"])
    used_height = (preset["margin_top"] + preset["rows"] * preset["label_height"]
                   + (preset["rows"] - 1) * preset["gap_y"])
    assert used_width <= 8.5 + 1e-9
    assert used_height <= 11 + 1e-9


def test_the_default_custom_layout_is_unchanged():
    from app.main import routes

    preset = routes._voucher_layout_from_form({"label_size": "custom"})["preset"]

    assert preset["label_width"] == pytest.approx(2.625)
    assert preset["label_height"] == pytest.approx(1)
    assert preset["columns"] == 3
    assert preset["rows"] == 10


# ---------------------------------------------------------------------------
# F24 -- the voucher starting number must be all digits.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123", (123, 3)),
        ("  0042  ", (42, 4)),      # surrounding whitespace is fine
        ("007", (7, 3)),            # leading zeros keep their width
        ("ABC123", (1, 3)),         # not a number: fall back, do not extract
        ("123ABC", (1, 3)),
        ("12 34", (1, 3)),
        ("", (1, 3)),
    ],
)
def test_the_server_start_number_requires_the_whole_field_to_be_digits(raw, expected):
    from app.main import routes

    _prefix, start, width = routes._voucher_number_parts({"start_number": raw})
    assert (start, width) == expected


def test_the_browser_start_number_parser_is_anchored():
    html = (Path(__file__).resolve().parents[1]
            / "app" / "templates" / "voucher_labels.html").read_text()

    assert "rawStart.match(/^[0-9]+$/)" in html
    assert "rawStart.match(/\\d+/)" not in html
