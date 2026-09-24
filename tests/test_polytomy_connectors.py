"""Polytomy connector pegs in the tree viewer.

A tip that sits directly on a multifurcation with an effectively zero-length
branch (at or below ZERO_LENGTH_POLYTOMY_EPSILON) gets a short display-only
connector so its label does not touch the vertical backbone. The behaviour is
all browser-side, so it is exercised by ``tests/js/polytomy_connectors.test.js``
against the shipped viewer and stylesheets.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tests" / "js" / "polytomy_connectors.test.js"


class PolytomyConnectorTests(unittest.TestCase):
    def test_polytomy_connector_harness(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        proc = subprocess.run(
            [node, str(HARNESS), str(REPO)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS polytomy connectors", proc.stdout)


if __name__ == "__main__":
    unittest.main()
