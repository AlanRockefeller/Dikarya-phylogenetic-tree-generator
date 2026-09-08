"""A merged record answers to every accession that was folded into it.

Rebuilding the queue's duplicate keys indexed only the primary identifier, so a
MycoMap accession that had already been collapsed into another record ("NR_123 /
AB123") matched nothing when it came back from a later BLAST at a different trim
length -- neither its identifier, because the key carried only NR_123, nor its
sequence, because the trim differs -- and the viewer drew a second tip for a
record that was already in the queue.

The behaviour is browser-only, so these run the *shipped* script out of
sequence_entry.html through a node harness. See tests/js/queue_duplicate_merge.test.js.
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

# Anchored on code rather than comment prose, so re-wording a comment cannot
# quietly stop the test from extracting the real implementation.
SLICE_START = "    function normalizeDedupLocation(value, preserveLocality = false) {"
SLICE_END = "    function removeSequence(index) {"


def extracted_script():
    html = TEMPLATE.read_text(encoding="utf-8")
    start = html.index(SLICE_START)
    return html[start:html.index(SLICE_END, start)]


def run_batches(batches):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "extracted.js"
        path.write_text(extracted_script(), encoding="utf-8")
        proc = subprocess.run(
            [node, str(JS_DIR / "queue_duplicate_merge.test.js"), str(path),
             json.dumps(batches)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    if proc.returncode != 0:
        raise AssertionError(
            f"queue_duplicate_merge.test.js failed:\n{proc.stdout}\n{proc.stderr}"
        )
    return json.loads(proc.stdout)


def mycomap(name, sequence, **extra):
    record = {
        "name": name,
        "sequence": sequence,
        "source": "mycomap",
        "hit_source": "ncbi",
        "location": "California, US",
    }
    record.update(extra)
    return record


SEQ = "ACGTACGTACGTACGTACGT"


class MergedAccessionDedupTests(unittest.TestCase):
    def test_a_merged_accession_is_recognized_when_it_comes_back_trimmed(self):
        """The reported case, in the order it was reported."""
        result = run_batches([
            [mycomap("AB123 Amanita muscaria", SEQ),
             mycomap("NR_123 Amanita muscaria", SEQ)],
            # The same GenBank record from a second BLAST, trimmed longer.
            [mycomap("AB123 Amanita muscaria", SEQ + "TTTT")],
        ])
        self.assertEqual(result["added_per_batch"], [1, 0])
        self.assertEqual(len(result["queue"]), 1)
        entry = result["queue"][0]
        self.assertEqual(entry["merged_ids"], ["NR_123", "AB123"])
        # The longer read wins, as it does for any same-record re-fetch.
        self.assertEqual(entry["sequence"], SEQ + "TTTT")

    def test_the_primary_accession_still_collapses_a_later_refetch(self):
        result = run_batches([
            [mycomap("AB123 Amanita muscaria", SEQ),
             mycomap("NR_123 Amanita muscaria", SEQ)],
            [mycomap("NR_123 Amanita muscaria", SEQ + "GG")],
        ])
        self.assertEqual(result["added_per_batch"], [1, 0])
        self.assertEqual(len(result["queue"]), 1)

    def test_a_different_record_is_still_queued_separately(self):
        result = run_batches([
            [mycomap("AB123 Amanita muscaria", SEQ),
             mycomap("NR_123 Amanita muscaria", SEQ)],
            [mycomap("XY999 Amanita pantherina", "TTGGCCAATTGGCCAATTGG")],
        ])
        self.assertEqual(result["added_per_batch"], [1, 1])
        self.assertEqual(len(result["queue"]), 2)

    def test_records_without_merged_ids_dedupe_as_before(self):
        """An unmerged record has no merged_ids and keys on its own id alone."""
        result = run_batches([
            [mycomap("AB123 Amanita muscaria", SEQ)],
            [mycomap("AB123 Amanita muscaria", SEQ + "A"),
             mycomap("CD456 Russula", "GGGGCCCCAAAATTTTGGGG")],
        ])
        self.assertEqual(result["added_per_batch"], [1, 1])
        self.assertEqual(len(result["queue"]), 2)

    def test_a_non_mycomap_record_is_not_keyed_by_accession(self):
        """Identifier keys are a MycoMap rule; ordinary imports keep sequence keys."""
        plain = {
            "name": "AB123 Amanita muscaria",
            "sequence": SEQ,
            "location": "California, US",
        }
        other = dict(plain, sequence=SEQ + "A", name="AB123 Amanita muscaria")
        result = run_batches([[plain], [other]])
        self.assertEqual(result["added_per_batch"], [1, 1])


if __name__ == "__main__":
    unittest.main()
