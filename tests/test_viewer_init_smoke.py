"""The tree viewer's browser bootstrap must actually run.

On 2026-08-24 a ``const`` in ``tree_viewer_controller.js`` was read sixteen
lines above its declaration. The whole controller lives inside a single
``DOMContentLoaded`` callback, so that temporal-dead-zone ReferenceError aborted
the entire bootstrap and ``/job/<id>/view`` sat on "loading" forever for every
visitor. It reached production because nothing in this suite executed that file:
``test_phylotree_invariants.py`` covers the phylotree bundle, not the controller
that drives it.

The behaviour is browser-only, so it is exercised by a node harness that loads
the *shipped* scripts in the order ``job_viewer.html`` loads them and fires the
bootstrap against a stub DOM. See ``tests/js/viewer_init_smoke.test.js``.

This proves the bootstrap RUNS. It does not prove a tree is drawn correctly -
the DOM is a stub, so a wrong SVG transform passes. That is what
``scripts/dikarya_viewer_smoke.py`` checks against the live site.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import dikarya_viewer_smoke as live_smoke

REPO = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).resolve().parent / "js" / "viewer_init_smoke.test.js"


def run_harness():
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    proc = subprocess.run(
        [node, str(HARNESS), str(REPO), "--json"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "the viewer init harness could not run:\n{}\n{}".format(
                proc.stdout, proc.stderr
            )
        )
    return json.loads(proc.stdout)


class ViewerInitSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {r["name"]: r for r in run_harness()}

    def _assert_ok(self, name):
        result = self.results.get(name)
        self.assertIsNotNone(
            result,
            "the harness did not report '{}'; it stopped early. Reported: {}".format(
                name, sorted(self.results)
            ),
        )
        self.assertTrue(result["ok"], "{}\n{}".format(name, result["detail"]))

    def test_every_viewer_script_evaluates(self):
        """Each shipped script must parse and run its top level."""
        loads = [n for n in self.results if n.startswith("load:")]
        self.assertTrue(loads, "the harness loaded no scripts at all")
        for name in loads:
            with self.subTest(script=name):
                self._assert_ok(name)

    def test_controller_registers_bootstrap(self):
        """A controller that wires up nothing would leave the page inert."""
        self._assert_ok("registers-DOMContentLoaded")

    def test_bootstrap_runs_without_throwing(self):
        """The regression that took /job/<id>/view down on 2026-08-24.

        Any init-time throw - TDZ, a typo, a bad destructure, a missing global -
        aborts the whole bootstrap and presents to the user as a page that loads
        forever. They are all caught here.
        """
        self._assert_ok("bootstrap-runs-without-throwing")


class ServedAssetIntegrityTests(unittest.TestCase):
    def test_cross_origin_static_looking_script_is_rejected(self):
        self.assertIsNone(
            live_smoke.local_path_for(
                "https://attacker.example/static/js/tree_viewer_controller.js",
                "https://dikarya.us",
            )
        )
        self.assertEqual(
            live_smoke.local_path_for(
                "https://dikarya.us/static/js/tree_viewer_controller.js?v=1",
                "https://dikarya.us",
            ),
            "app/static/js/tree_viewer_controller.js",
        )

    def test_mismatched_remote_bytes_are_never_mirrored_for_execution(self):
        with (
            tempfile.TemporaryDirectory() as repo_tmp,
            tempfile.TemporaryDirectory() as mirror_tmp,
        ):
            repo = Path(repo_tmp)
            rel = "app/static/js/example.js"
            trusted = repo / rel
            trusted.parent.mkdir(parents=True)
            trusted.write_bytes(b"globalThis.executed = 'trusted';\n")

            with patch.object(live_smoke, "REPO", repo):
                matched = live_smoke.mirror_verified_asset(
                    rel, b"globalThis.executed = 'remote';\n", Path(mirror_tmp)
                )

            self.assertFalse(matched)
            self.assertFalse((Path(mirror_tmp) / rel).exists())

    def test_matching_remote_bytes_are_mirrored_from_the_trusted_checkout(self):
        with (
            tempfile.TemporaryDirectory() as repo_tmp,
            tempfile.TemporaryDirectory() as mirror_tmp,
        ):
            repo = Path(repo_tmp)
            rel = "app/static/js/example.js"
            trusted = repo / rel
            trusted.parent.mkdir(parents=True)
            body = b"globalThis.executed = 'trusted';\n"
            trusted.write_bytes(body)

            with patch.object(live_smoke, "REPO", repo):
                matched = live_smoke.mirror_verified_asset(rel, body, Path(mirror_tmp))

            self.assertTrue(matched)
            self.assertEqual((Path(mirror_tmp) / rel).read_bytes(), body)


if __name__ == "__main__":
    unittest.main()
