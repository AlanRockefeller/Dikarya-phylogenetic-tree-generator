"""Regression coverage for display-only cleanup of tree tip names."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
VIEWER = REPO / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"
SLICE_START = "    const UNQUOTED_PROVISIONAL_CODE_RE ="
SLICE_END = "    // Alan 8/15/26 - Curated font list"

HARNESS = """
const fs = require('fs');
const window = {};
// eslint-disable-next-line no-eval
eval(fs.readFileSync(process.argv[2], 'utf8'));
const names = JSON.parse(process.argv[3]);
console.log(JSON.stringify(names.map(name => cleanTipDisplayName(name))));
"""


class TreeViewerTipNameTests(unittest.TestCase):
    def test_provisional_codes_are_quoted_without_changing_other_names(self):
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node is not installed")
        source = VIEWER.read_text(encoding="utf-8")
        start = source.index(SLICE_START)
        extracted = source[start:source.index(SLICE_END, start)]
        names = [
            "iNat9402447 Pisolithus sp. AZ01 San Bernardino Co. CA US",
            "iNat18560302 Pisolithus sp. tinctorius-IN01 Morgan Co. IN US",
            "iNat1 Pisolithus sp. 'PNW01' Oregon US",
            "iNat2 Pisolithus sp. California US",
            "_R_iNat3 Pisolithus albus Hawaii US RiC 12",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "extracted.js"
            script.write_text(extracted, encoding="utf-8")
            harness = Path(tmp) / "harness.js"
            harness.write_text(HARNESS, encoding="utf-8")
            proc = subprocess.run(
                [node, str(harness), str(script), json.dumps(names)],
                capture_output=True, text=True, timeout=60,
            )
        if proc.returncode != 0:
            raise AssertionError(f"harness failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertEqual(json.loads(proc.stdout), [
            "iNat9402447 Pisolithus sp. 'AZ01' San Bernardino Co. CA US",
            "iNat18560302 Pisolithus sp. 'tinctorius-IN01' Morgan Co. IN US",
            "iNat1 Pisolithus sp. 'PNW01' Oregon US",
            "iNat2 Pisolithus sp. California US",
            "iNat3 Pisolithus albus Hawaii US",
        ])


if __name__ == "__main__":
    unittest.main()
