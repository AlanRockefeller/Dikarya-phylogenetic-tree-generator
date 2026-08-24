"""Regression coverage for the structural invariants of Dikarya's phylotree.js.

``app/static/js/phylotree.js`` began as phylotree.js 2.2.1 but has been modified
locally, so it is production code rather than an untouched vendor drop. An
automated review of PR #5 (a synthetic PR that existed only to expose the bundle
to review) reported six defects in it. All six were real, and every one of them
is the same underlying failure: a structural mutation that leaves one of the
model's derived views describing a tree that no longer exists.

The behaviour lives entirely in the browser, so it is exercised by a node
harness that loads the *shipped* bundle - in the same order ``job_viewer.html``
loads it - and asserts against real trees. Those assertions cannot drift away
from what is actually served.

See ``tests/js/phylotree_invariants.test.js`` for the cases themselves.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).resolve().parent / "js" / "phylotree_invariants.test.js"

# One group per reported finding, so a regression names the finding it broke.
GROUPS = {
    "delete-below-root": "deleting a direct child of a bifurcating root",
    "add-child-parent": "addChild() maintaining the parent/child invariant",
    "newick-hidden-children": "Newick separators for hidden children",
    "invalid-newick-surfacing": "malformed Newick reaching the viewer failure path",
    "links-after-reroot": "cached links following a topology change",
    "branch-lengths": "branch lengths across a reroot",
    "reroot-listeners": "display listeners surviving a reroot",
    "binary-selection": "`this` inside the binary-selection callbacks",
}


def run_harness():
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    proc = subprocess.run(
        [node, str(HARNESS), str(REPO), "--json"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "the phylotree harness could not run:\n{}\n{}".format(
                proc.stdout, proc.stderr
            )
        )
    return json.loads(proc.stdout)


class PhylotreeInvariantTests(unittest.TestCase):
    """Every group of cases is reported as its own failure."""

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

    def test_deleting_below_a_bifurcating_root(self):
        self.assert_group("delete-below-root")

    def test_add_child_sets_the_parent(self):
        self.assert_group("add-child-parent")

    def test_newick_stays_valid_with_hidden_children(self):
        self.assert_group("newick-hidden-children")

    def test_invalid_newick_surfaces_as_a_load_failure(self):
        self.assert_group("invalid-newick-surfacing")

    def test_links_follow_the_current_topology(self):
        self.assert_group("links-after-reroot")

    def test_branch_lengths_survive_a_reroot(self):
        self.assert_group("branch-lengths")

    def test_listeners_survive_a_reroot(self):
        self.assert_group("reroot-listeners")

    def test_binary_selection_callbacks_are_bound(self):
        self.assert_group("binary-selection")

    def test_every_group_is_covered(self):
        """A group that stops running would otherwise pass silently."""
        ran = {r["group"] for r in self.results}
        self.assertEqual(ran, set(GROUPS), "harness groups drifted from this driver")


class HarnessIntegrityTests(unittest.TestCase):
    """The harness must be testing the shipped file, not a copy of it."""

    def test_it_loads_the_served_bundle_and_its_real_dependencies(self):
        harness = HARNESS.read_text(encoding="utf-8")
        for served in (
            "app/static/js/phylotree.js",
            "app/static/vendor/lodash-4.min.js",
            "app/static/vendor/underscore-1.13.6-min.js",
        ):
            self.assertIn(served, harness)
            self.assertTrue((REPO / served).is_file(), served)

    def test_it_reproduces_the_page_own_lodash_wiring(self):
        """The bundle wants Lodash as ``_$1``; job_viewer.html arranges that.

        If the template ever stops doing it, the neighbor-joining and distance
        helpers break in the browser while the harness carries on passing, so
        pin the two together.
        """
        viewer = (REPO / "app" / "templates" / "job_viewer.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("window._$1 = window._;", viewer)
        self.assertIn("ctx._$1 = ctx._;", HARNESS.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
