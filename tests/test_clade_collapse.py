"""Clade collapse and the single-level Undo, on the browser side.

Two things are pinned here:

  * the user-facing wording, which is the whole point of the rename -- "Collapse
    Subtree" read to users like it deleted something, and the label lives in the
    locally-modified copy of phylotree.js where nothing else would catch a
    revert;
  * the behaviour, via ``tests/js/clade_collapse_undo.test.js``, which runs the
    SHIPPED bundle in node so the assertions cannot drift from what is served.

Each harness group is reported as its own failure so a regression names what it
broke.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).resolve().parent / "js" / "clade_collapse_undo.test.js"
PHYLOTREE = REPO / "app" / "static" / "js" / "phylotree.js"
VIEWER = REPO / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"
TEMPLATE = REPO / "app" / "templates" / "job_viewer.html"

GROUPS = {
    "stable-clade-id": "identifying a clade by its descendant tips",
    "collapse-single": "collapsing and expanding one clade",
    "collapse-bulk": "collapsing several selected clades at once",
    "collapse-overlap": "normalizing hierarchically overlapping selections",
    "collapse-root": "keeping the root out of bulk collapse",
    "collapse-undo": "undoing a collapse",
    "collapse-nondestructive": "collapse leaving persisted state alone",
    "keyboard": "Ctrl/Cmd+Z",
}


def run_harness():
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    try:
        proc = subprocess.run(
            [node, str(HARNESS), str(REPO), "--json"],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            "the collapse harness exceeded the 60-second timeout:\n{}\n{}".format(
                exc.stdout or "", exc.stderr or ""
            )
        ) from exc
    if proc.returncode != 0:
        raise AssertionError(
            "the collapse harness could not run:\n{}\n{}".format(proc.stdout, proc.stderr)
        )
    return json.loads(proc.stdout)


class CollapseBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = run_harness()

    def assert_group(self, group):
        cases = [r for r in self.results if r["group"] == group]
        self.assertTrue(cases, "no cases ran for {!r}".format(group))
        failed = [r for r in cases if not r["ok"]]
        if failed:
            self.fail(
                "{}:\n\n".format(GROUPS[group])
                + "\n\n".join("{name}\n    {error}".format(**r) for r in failed)
            )

    def test_a_clade_is_identified_by_its_tips(self):
        self.assert_group("stable-clade-id")

    def test_single_collapse_and_expand(self):
        self.assert_group("collapse-single")

    def test_bulk_collapse_of_selected_clades(self):
        self.assert_group("collapse-bulk")

    def test_overlapping_selections_normalize(self):
        self.assert_group("collapse-overlap")

    def test_the_root_is_never_bulk_collapsed(self):
        self.assert_group("collapse-root")

    def test_collapse_is_undoable(self):
        self.assert_group("collapse-undo")

    def test_collapse_persists_nothing(self):
        self.assert_group("collapse-nondestructive")

    def test_the_undo_hotkey(self):
        self.assert_group("keyboard")

    def test_every_group_is_covered(self):
        """A group that stops running would otherwise pass silently."""
        ran = {r["group"] for r in self.results}
        self.assertEqual(ran, set(GROUPS), "harness groups drifted from this driver")

    def test_async_rejection_is_reported_under_the_named_test(self):
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node is not installed")
        proc = subprocess.run(
            [
                node, str(HARNESS), str(REPO), "--json",
                "--self-test-async-failure",
            ],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads(proc.stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["name"], "a rejected async assertion keeps its test name"
        )
        self.assertFalse(rows[0]["ok"])
        self.assertIn("intentional async assertion failure", rows[0]["error"])
        self.assertNotEqual(rows[0]["group"], "harness")


class StableCladeIdAgreementTests(unittest.TestCase):
    """The viewer and the backend must name the same clade the same way.

    The browser resolves a right-clicked or selected clade to
    ``internal:<fnv1a>`` over its sorted descendant tips, and prune / rotate /
    reroot look the clade up by that string. A drift between the two
    implementations would not fail loudly; it would silently address a
    different clade, so the same literals are pinned on both sides. The JS half
    lives in the ``stable-clade-id`` group of the harness.
    """

    def test_the_reference_hashes_are_what_python_produces(self):
        from app.services.tree_edit_service import (
            _stable_internal_node_id_from_names as clade_id,
        )
        self.assertEqual(clade_id(["A", "B"]), "internal:9e2f3271")
        self.assertEqual(clade_id(["B", "A"]), "internal:9e2f3271")


class TerminologyTests(unittest.TestCase):
    """"Clade", not "subtree", wherever the viewer offers to fold one away."""

    def test_the_context_menu_says_clade(self):
        source = PHYLOTREE.read_text(encoding="utf-8")
        self.assertIn('"Collapse Clade"', source)
        self.assertIn('"Expand Clade"', source)
        # Checked per line rather than over the whole 15k-line bundle, so a
        # failure names the offending line instead of printing the file.
        offenders = [
            line.strip()
            for line in source.splitlines()
            if "Subtree" in line and (".text(" in line or "Collapse" in line)
        ]
        self.assertEqual(offenders, [], "the old wording is still user-visible")

    def test_the_bulk_actions_say_clades(self):
        source = VIEWER.read_text(encoding="utf-8")
        self.assertIn("Collapse Selected Clades", source)
        self.assertIn("Expand Selected Clades", source)
        self.assertIn("Clade${", source, "the counted bulk label is missing")

    def test_the_help_modal_explains_collapse_prune_recompute_and_undo(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        for phrase in (
            "Collapse Clade",
            "Expand Clade",
            "Collapse&nbsp;N&nbsp;Clades",
            "Ctrl/Cmd + Z",
            'id="btn-undo"',
        ):
            self.assertIn(phrase, html, phrase)
        # The three operations users conflate must each be named in the guide.
        for heading in ("Collapse a clade", "Prune", "Recompute", "Undo"):
            self.assertIn(">{}<".format(heading), html, heading)


class AssetVersionTests(unittest.TestCase):
    """Static assets are served straight from the working tree with a 30-day
    cache, so a behaviour change that does not bump ?v= reaches nobody."""

    def test_the_changed_bundles_are_cache_busted_together(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        for asset in (
            "js/phylotree.js",
            "js/tree_viewer_phylotree_v2.js",
            "js/tree_viewer_api.js",
            "js/tree_viewer_controller.js",
        ):
            self.assertRegex(
                html, r"{}'\) \}}\}}\?v=\d+".format(asset.replace(".", r"\.")),
                "{} is served without a cache-busting version".format(asset),
            )


if __name__ == "__main__":
    unittest.main()
