"""An annotation save clears only the selection that created it.

The save is a network round trip, so the user can select a different group
while it is in flight. The completion handler used to clear whatever was
selected when it resumed, throwing that newer selection away.

The behaviour lives entirely in the browser bundle, so it is checked by running
the shipped lines: ``tests/js/annotation_selection_race.test.js`` slices the
real guard out of ``tree_viewer_controller.js`` and executes it. Each group is
reported separately so a regression names what it broke.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).resolve().parent / "js" / "annotation_selection_race.test.js"

GROUPS = {
    "clears-the-selection-it-annotated": "clearing the selection that was annotated",
    "ordering-is-irrelevant": "treating selection order as meaningless",
    "keeps-a-newer-selection-made-during-the-save":
        "preserving a selection made while the save was in flight",
    "a-superset-is-a-different-selection": "an extended selection being a different one",
    "a-subset-is-a-different-selection": "a narrowed selection being a different one",
    "a-selection-cleared-during-the-save-stays-cleared":
        "not re-clearing an already-empty selection",
    "an-edit-never-touches-the-selection": "an edit leaving the selection alone",
    "an-older-viewer-without-the-accessor-still-clears":
        "the fallback when the viewer cannot report its selection",
    "set-equality-ignores-duplicates-and-order": "the set comparison itself",
}


class AnnotationSelectionRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node is not installed")
        proc = subprocess.run(
            [node, str(HARNESS), str(REPO), "--json"],
            capture_output=True, text=True, timeout=60,
        )
        if not proc.stdout.strip():
            raise AssertionError(
                "the annotation harness could not run:\n{}\n{}".format(
                    proc.stdout, proc.stderr
                )
            )
        cls.results = json.loads(proc.stdout)

    def assert_group(self, group):
        result = self.results.get(group)
        self.assertIsNotNone(result, "no cases ran for {!r}".format(group))
        if not result["ok"]:
            self.fail("{}:\n    {}".format(GROUPS[group], result["error"]))

    def test_the_annotated_selection_is_cleared(self):
        self.assert_group("clears-the-selection-it-annotated")

    def test_reordering_is_not_a_different_selection(self):
        self.assert_group("ordering-is-irrelevant")

    def test_a_newer_selection_survives_the_save(self):
        self.assert_group("keeps-a-newer-selection-made-during-the-save")

    def test_a_superset_is_a_different_selection(self):
        self.assert_group("a-superset-is-a-different-selection")

    def test_a_subset_is_a_different_selection(self):
        self.assert_group("a-subset-is-a-different-selection")

    def test_an_empty_selection_is_not_re_cleared(self):
        self.assert_group("a-selection-cleared-during-the-save-stays-cleared")

    def test_an_edit_leaves_the_selection_alone(self):
        self.assert_group("an-edit-never-touches-the-selection")

    def test_a_viewer_without_the_accessor_keeps_the_old_behaviour(self):
        self.assert_group("an-older-viewer-without-the-accessor-still-clears")

    def test_the_set_comparison(self):
        self.assert_group("set-equality-ignores-duplicates-and-order")

    def test_every_group_is_covered(self):
        """A group that stops running would otherwise pass silently."""
        self.assertEqual(set(self.results), set(GROUPS),
                         "harness groups drifted from this driver")


if __name__ == "__main__":
    unittest.main()
