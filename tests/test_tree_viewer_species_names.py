"""The viewer's quoted-epithet parser, run as the browser runs it.

`speciesQuotedEpithet()` reduces the several ways a provisional name travels --
Amanita sp. 'albemarlensis', Amanita "albemarlensis", curly quotes from a
spreadsheet -- to one epithet, so those tips count as one species when the
viewer suggests clade annotations. Informal codes (Russula "sp-IN67") keep
their case, because that is what distinguishes two species in these trees.

The pattern is now built once outside the function instead of on every call.
That is only safe while the semantics are identical, which is what these pin
down: the shipped function is extracted from tree_viewer_controller.js and
executed under Node rather than re-implemented here.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONTROLLER = REPO / "app" / "static" / "js" / "tree_viewer_controller.js"

# Anchored on code, not comment prose. Covers the hoisted constant and the
# function that uses it.
SLICE_START = "    const SPECIES_QUOTE_CHARS ="
SLICE_END = "    function speciesRankMarker(token) {"

HARNESS = """
const fs = require('fs');
// eslint-disable-next-line no-eval
eval(fs.readFileSync(process.argv[2], 'utf8'));
const out = JSON.parse(process.argv[3]).map(token => speciesQuotedEpithet(token));
console.log(JSON.stringify(out));
"""

CASES = [
    # (token, expected epithet or None)
    ("'albemarlensis'", "albemarlensis"),
    ('"albemarlensis"', "albemarlensis"),
    ("‘albemarlensis’", "albemarlensis"),
    ("“albemarlensis”", "albemarlensis"),
    # Lowercased, so the two spellings of one provisional name collapse.
    ("'Albemarlensis'", "albemarlensis"),
    ('"ALBEMARLENSIS"', "albemarlensis"),
    # A numeric epithet is an informal code and is kept exactly as written.
    ('"sp-IN67"', "sp-IN67"),
    ('"moseri-CA01"', "moseri-CA01"),
    ('"IN67"', "IN67"),
    # Mixed quote characters at the two ends still parse.
    ("‘albemarlensis\"", "albemarlensis"),
    # Not quoted at all.
    ("albemarlensis", None),
    # Unbalanced.
    ("'albemarlensis", None),
    ("albemarlensis'", None),
    # Empty and single-character epithets are not names.
    ("''", None),
    ("'a'", None),
    # Must start with a letter.
    ("'1abc'", None),
    ("'-abc'", None),
    # Interior whitespace is not one token.
    ("'two words'", None),
    # Non-token input.
    ("", None),
]


def _run(tokens):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    source = CONTROLLER.read_text(encoding="utf-8")
    start = source.index(SLICE_START)
    extracted = source[start:source.index(SLICE_END, start)]
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "extracted.js"
        script.write_text(extracted, encoding="utf-8")
        harness = Path(tmp) / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), str(script), json.dumps(tokens)],
            capture_output=True, text=True, timeout=60,
        )
    if proc.returncode != 0:
        raise AssertionError(f"harness failed:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout)


class SpeciesQuotedEpithetTests(unittest.TestCase):
    def test_every_documented_case_is_unchanged(self):
        tokens = [token for token, _ in CASES]
        for (token, expected), actual in zip(CASES, _run(tokens)):
            with self.subTest(token=token):
                self.assertEqual(actual, expected)

    def test_repeated_calls_return_the_same_answer(self):
        """A hoisted RegExp with a /g flag would alternate via lastIndex.

        This is the one way sharing an instance can change behaviour, so it is
        checked directly rather than inferred from the source.
        """
        results = _run(["'albemarlensis'"] * 4 + ['"sp-IN67"'] * 4)
        self.assertEqual(results, ["albemarlensis"] * 4 + ["sp-IN67"] * 4)

    def test_the_pattern_is_built_once(self):
        source = CONTROLLER.read_text(encoding="utf-8")
        self.assertIn("const SPECIES_QUOTED_EPITHET_RE = new RegExp(", source)
        self.assertEqual(source.count("SPECIES_QUOTED_EPITHET_RE"), 2)
        self.assertEqual(
            source.count("function speciesQuotedEpithet(token) {"), 1
        )
        # No /g flag on the shared instance: it would carry lastIndex between
        # calls and make every second lookup of the same token fail.
        declaration = source[source.index("const SPECIES_QUOTED_EPITHET_RE"):]
        declaration = declaration[:declaration.index(");") + 2]
        self.assertNotIn("'g'", declaration)
        self.assertNotIn('"g"', declaration)


if __name__ == "__main__":
    unittest.main()
