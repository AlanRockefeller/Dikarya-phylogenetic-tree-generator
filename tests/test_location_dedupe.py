"""A location is a field, not a substring.

Both the queue row and the FASTA header asked "does this text already say where
the specimen came from?" with a bare `.includes()`. A two-letter location is a
substring of ordinary mycological words -- "OR" sits inside "Cortinarius", "CA"
inside "Cantharellus", "IN" inside "Inocybe" -- so an Oregon record of a
Cortinarius was treated as already carrying its location and silently lost it
from both the queue and the submitted FASTA.

The behaviour is browser-only, so these run the *shipped* script out of
sequence_entry.html through a node harness rather than a Python re-creation of
it. See tests/js/location_dedupe.test.js.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
JS_DIR = Path(__file__).resolve().parent / "js"
TEMPLATE = REPO / "app" / "templates" / "sequence_entry.html"

# Slices of the template the harness executes, each anchored on code rather
# than on comment prose so re-wording a comment cannot quietly stop the test
# from extracting the real implementation.
SLICES = (
    # The shared helper.
    ("    function normalizeLocationText(value) {", "    let sequenceQueue = [];"),
    # The queue/display render path.
    ("    function updateQueueDisplay() {", "    function updateButtonVisuals(btn, isValid) {"),
    # The FASTA-header construction path.
    ("    const LOCATION_AFTER_ORGANISM_HIT_SOURCES = new Set(",
     "    function buildFastaFromQueue() {"),
)


def extracted_script():
    html = TEMPLATE.read_text(encoding="utf-8")
    parts = []
    for start_marker, end_marker in SLICES:
        start = html.index(start_marker)
        parts.append(html[start:html.index(end_marker, start)])
    return "\n".join(parts)


def run_cases(cases):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "extracted.js"
        path.write_text(extracted_script(), encoding="utf-8")
        proc = subprocess.run(
            [node, str(JS_DIR / "location_dedupe.test.js"), str(path),
             json.dumps(cases)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    if proc.returncode != 0:
        raise AssertionError(
            f"location_dedupe.test.js failed:\n{proc.stdout}\n{proc.stderr}"
        )
    return json.loads(proc.stdout)


def seq(name, organism="", location="", **extra):
    record = {
        "name": name,
        "organism": organism,
        "location": location,
        "sequence": "ACGT",
        "hit_source": "inat_observation",
    }
    record.update(extra)
    return record


class LocationBoundaryTests(unittest.TestCase):
    """Both paths get the same cases, because both had the same bug."""

    def _run(self, records):
        rows = run_cases(records)
        self.assertEqual(len(rows), len(records))
        return rows

    def test_a_short_location_inside_a_longer_word_is_still_appended(self):
        """The reported case: location OR, organism Cortinarius."""
        records = [
            seq("iNat123456", "Cortinarius", "OR"),
            seq("iNat123457", "Cantharellus", "CA"),
            seq("iNat123458", "Inocybe", "IN"),
            seq("iNat123459", "Ukrainian collection", "UK"),
        ]
        for record, row in zip(records, self._run(records)):
            with self.subTest(organism=record["organism"]):
                self.assertEqual(row["shown"], record["location"])
                self.assertTrue(
                    row["header"].endswith(" " + record["location"]),
                    f"{row['header']!r} lost its location",
                )

    def test_a_location_already_present_as_a_trailing_field_is_not_duplicated(self):
        records = [
            seq("iNat123456 Cortinarius OR", "", "OR"),
            seq("iNat123456", "Cortinarius OR", "OR"),
            seq("iNat999 Amanita muscaria Pike Co. MS US", "", "Pike Co. MS US"),
        ]
        for record, row in zip(records, self._run(records)):
            with self.subTest(name=record["name"]):
                self.assertEqual(row["shown"], "")
                self.assertEqual(row["header"].count(record["location"]), 1)

    def test_a_location_present_as_an_interior_field_is_not_duplicated(self):
        """Not only a suffix: an observation number can follow the place."""
        records = [seq("iNat999 Amanita OR RiC 30", "", "OR")]
        row = self._run(records)[0]
        self.assertEqual(row["shown"], "")
        self.assertEqual(row["header"], "iNat999 Amanita OR RiC 30")

    def test_case_variation_counts_as_the_same_location(self):
        records = [
            seq("iNat1 Amanita or", "", "OR"),
            seq("iNat2 Amanita PIKE CO. MS US", "", "Pike Co. MS US"),
            seq("iNat3 Amanita", "", "or"),
        ]
        rows = self._run(records)
        self.assertEqual(rows[0]["shown"], "")
        self.assertEqual(rows[1]["shown"], "")
        # The third has no location in its header at all, so it is still added.
        self.assertEqual(rows[2]["shown"], "or")

    def test_repeated_and_surrounding_whitespace_is_normalized_on_both_sides(self):
        records = [
            seq("iNat1 Amanita Pike Co. MS US", "", "  Pike   Co.  MS   US  "),
            seq("iNat2  Amanita   Pike  Co.  MS  US", "", "Pike Co. MS US"),
            seq("iNat3 Amanita", "", "   OR   "),
        ]
        rows = self._run(records)
        self.assertEqual(rows[0]["shown"], "")
        self.assertEqual(rows[1]["shown"], "")
        # Normalized before appending, never pasted in with its padding.
        self.assertEqual(rows[2]["shown"], "OR")
        self.assertEqual(rows[2]["header"], "iNat3 Amanita OR")

    def test_underscore_and_pipe_separated_headers_are_field_boundaries(self):
        """FASTA headers separate fields with underscores as often as spaces."""
        records = [
            seq("iNat1_Amanita_OR", "", "OR"),
            seq("iNat2|Amanita|OR", "", "OR"),
        ]
        for record, row in zip(records, self._run(records)):
            with self.subTest(name=record["name"]):
                self.assertEqual(row["shown"], "")

    def test_an_empty_location_shows_and_appends_nothing(self):
        records = [seq("iNat1", "Amanita muscaria", ""),
                   seq("iNat2", "Amanita muscaria", "   ")]
        for record, row in zip(records, self._run(records)):
            with self.subTest(location=repr(record["location"])):
                self.assertEqual(row["shown"], "")
                self.assertEqual(row["header"],
                                 f"{record['name']} Amanita muscaria")

    def test_a_location_with_regex_metacharacters_is_matched_literally(self):
        """Place names carry dots and parentheses; they are not a pattern."""
        records = [
            seq("iNat1 Amanita Pike Co. MS US", "", "Pike Co. MS US"),
            seq("iNat2 Amanita PikeXCo. MS US", "", "Pike Co. MS US"),
            seq("iNat3 Amanita Washington (state) US", "", "Washington (state) US"),
        ]
        rows = self._run(records)
        self.assertEqual(rows[0]["shown"], "")
        # "." must not match the "X": that record does not carry this location.
        self.assertEqual(rows[1]["shown"], "Pike Co. MS US")
        self.assertEqual(rows[2]["shown"], "")

    def test_non_observation_imports_keep_their_header_untouched(self):
        """Only observation importers append the location; that is unchanged."""
        records = [seq("KT334709 Russula", "", "OR", hit_source="ncbi", source="mycomap")]
        row = self._run(records)[0]
        self.assertEqual(row["header"], "KT334709 Russula")
        # It is still *displayed* in the queue, which is not importer-scoped.
        self.assertEqual(row["shown"], "OR")


class SharedHelperTests(unittest.TestCase):
    """One helper backs both paths, so the two cannot drift apart."""

    def test_neither_path_still_uses_a_bare_substring_test(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn(
            "!`${name} ${organism}`.toLocaleLowerCase().includes(locationText",
            html,
        )
        self.assertNotIn(
            "!header.toLocaleLowerCase().includes(location.toLocaleLowerCase())",
            html,
        )

    def test_one_helper_is_defined_once_and_used_by_both(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(html.count("function headerAlreadyHasLocation("), 1)
        self.assertEqual(html.count("headerAlreadyHasLocation("), 3)


if __name__ == "__main__":
    unittest.main()
