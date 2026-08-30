"""Browser-side safety regressions for the iNaturalist observation finder."""

import shutil
import subprocess
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tests" / "js" / "inat_finder_variation_limit.test.js"


class InatFinderTests(unittest.TestCase):
    def test_variation_generation_is_bounded_before_work_begins(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        proc = subprocess.run(
            [node, str(HARNESS), str(REPO)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS iNat Finder variation limit", proc.stdout)
