"""Browser-side safety regressions for the iNaturalist observation finder."""

import shutil
import subprocess
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tests" / "js" / "inat_finder_variation_limit.test.js"


class InatFinderTests(unittest.TestCase):
    def test_browser_finder_regressions(self):
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
        self.assertIn("PASS iNat Finder browser regressions", proc.stdout)

    def test_template_loads_the_extracted_accessible_family_finder(self):
        template = (REPO / "app" / "templates" / "inat_finder.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('value="family"', template)
        self.assertIn("sm:grid-cols-2 lg:grid-cols-5", template)
        self.assertIn('value="taxon"', template)
        self.assertIn('id="taxon-choice-list"', template)
        self.assertIn('id="taxon-suggestions"', template)
        self.assertIn('id="taxon-pinned-label"', template)
        self.assertIn('aria-autocomplete="list"', template)
        self.assertIn(".finder-mode:focus-visible + label", template)
        self.assertNotIn('id="progress-panel" aria-live=', template)
        self.assertIn('id="progress-status" aria-live="polite"', template)
        self.assertIn("filename='js/inat_finder.js'", template)
        self.assertNotIn("<script>\n(() =>", template)
