"""
Regression coverage for the Edited FASTA export.

Edited FASTA is the original UNALIGNED input FASTA with the tree viewer's
current state applied: pruned records dropped, renamed tips carrying their
current tree label as the whole header, nucleotide data untouched.

Also pins the boundary that motivated a separate helper: extract_pruned_fasta()
still feeds recomputation and must keep the ORIGINAL identifiers, never the
user-visible renamed ones.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.tree_edit_service import (  # noqa: E402
    HAS_BIOPYTHON,
    build_edited_fasta_text,
    extract_pruned_fasta,
    has_fasta_affecting_edits,
)

# Two plain headers and one header carrying a description after its first token.
SAMPLE_FASTA = """>ABC123 Amanita example
ACGTACGTAC
GTACGT
>DEF456
TTTTGGGGCC
>GHI789 Russula example
AAAACCCCGG
"""


def _parse(text):
    """Tiny FASTA parser: ordered list of (header, sequence-without-newlines)."""
    records = []
    header = None
    seq = []
    for line in text.splitlines():
        if line.startswith(">"):
            if header is not None:
                records.append((header, "".join(seq)))
            header = line[1:]
            seq = []
        elif header is not None:
            seq.append(line.strip())
    if header is not None:
        records.append((header, "".join(seq)))
    return records


@unittest.skipUnless(HAS_BIOPYTHON, "BioPython required")
class EditedFastaExportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fasta = Path(self._tmp.name) / "input_raw.fasta"
        self.fasta.write_text(SAMPLE_FASTA)
        self.original_bytes = self.fasta.read_bytes()

    def tearDown(self):
        self._tmp.cleanup()

    def _export(self, state):
        return _parse(build_edited_fasta_text(self.fasta, state))

    # --- availability -----------------------------------------------------

    def test_no_edits_is_not_fasta_affecting(self):
        self.assertFalse(has_fasta_affecting_edits({}))
        self.assertFalse(has_fasta_affecting_edits({"pruned_taxa": [], "renames": {}}))

    def test_display_only_edits_are_not_fasta_affecting(self):
        # Rerooting, rotating and selecting write these keys but change no sequence.
        state = {
            "pruned_taxa": [],
            "renames": {},
            "root": "DEF456",
            "root_mode": "TIP",
            "is_midpoint_rooted": True,
            "rotated_nodes": ["node-1"],
            "selection_sets": {"Default": ["DEF456"]},
        }
        self.assertFalse(has_fasta_affecting_edits(state))

    def test_prune_or_rename_is_fasta_affecting(self):
        self.assertTrue(has_fasta_affecting_edits({"pruned_taxa": ["DEF456"]}))
        self.assertTrue(has_fasta_affecting_edits({"renames": {"DEF456": "New name"}}))

    def test_noop_and_blank_renames_are_not_fasta_affecting(self):
        # rename_tip() records whatever it is given; a label renamed back to
        # itself must not leave the export permanently "available".
        self.assertFalse(has_fasta_affecting_edits({"renames": {"DEF456": "DEF456"}}))
        self.assertFalse(has_fasta_affecting_edits({"renames": {"DEF456": "  "}}))
        self.assertFalse(has_fasta_affecting_edits({"pruned_taxa": ["", "  "]}))

    # --- export contents --------------------------------------------------

    def test_no_edits_exports_every_record_unchanged(self):
        records = self._export({"pruned_taxa": [], "renames": {}})
        self.assertEqual(
            [h for h, _ in records],
            ["ABC123 Amanita example", "DEF456", "GHI789 Russula example"],
        )
        self.assertEqual(records[0][1], "ACGTACGTACGTACGT")

    def test_rename_only_rewrites_header_and_keeps_all_records(self):
        state = {
            "pruned_taxa": [],
            "renames": {"ABC123 Amanita example": "Amanita muscaria voucher ABC123"},
        }
        records = self._export(state)
        self.assertEqual(
            [h for h, _ in records],
            ["Amanita muscaria voucher ABC123", "DEF456", "GHI789 Russula example"],
        )
        # Sequence data must survive a rename untouched.
        self.assertEqual(records[0][1], "ACGTACGTACGTACGT")
        self.assertEqual(records[1][1], "TTTTGGGGCC")

    def test_rename_matching_first_token_only(self):
        # Tree tips are sometimes the first token rather than the full header.
        state = {"renames": {"ABC123": "Amanita muscaria voucher ABC123"}}
        records = self._export(state)
        self.assertEqual(records[0][0], "Amanita muscaria voucher ABC123")
        self.assertEqual(records[0][1], "ACGTACGTACGTACGT")

    def test_prune_only_drops_the_record_and_leaves_headers_alone(self):
        records = self._export({"pruned_taxa": ["DEF456"], "renames": {}})
        self.assertEqual(
            [h for h, _ in records],
            ["ABC123 Amanita example", "GHI789 Russula example"],
        )

    def test_prune_by_full_header_with_description(self):
        # Must not regress: a header with a description after its first token
        # is pruned when the tip name is the whole header.
        records = self._export({"pruned_taxa": ["ABC123 Amanita example"]})
        self.assertEqual([h for h, _ in records], ["DEF456", "GHI789 Russula example"])

    def test_prune_and_rename_together(self):
        state = {
            "pruned_taxa": ["GHI789 Russula example"],
            "renames": {"ABC123 Amanita example": "Amanita muscaria voucher ABC123"},
        }
        records = self._export(state)
        self.assertEqual(
            [h for h, _ in records],
            ["Amanita muscaria voucher ABC123", "DEF456"],
        )

    def test_export_matches_prune_only_sequence_set_when_no_renames(self):
        state = {"pruned_taxa": ["DEF456"], "renames": {}}
        pruned_out = Path(self._tmp.name) / "alignment_pruned.fasta"
        extract_pruned_fasta(self.fasta, state, pruned_out)

        legacy = {h.split()[0]: s for h, s in _parse(pruned_out.read_text())}
        edited = {h.split()[0]: s for h, s in self._export(state)}
        self.assertEqual(legacy, edited)

    def test_export_never_modifies_the_source_fasta(self):
        self._export({
            "pruned_taxa": ["DEF456"],
            "renames": {"ABC123 Amanita example": "Renamed"},
        })
        self.assertEqual(self.fasta.read_bytes(), self.original_bytes)

    def test_export_is_unaligned(self):
        # Nothing in the pipeline should introduce alignment gaps here.
        text = build_edited_fasta_text(self.fasta, {"renames": {"DEF456": "Renamed"}})
        self.assertNotIn("-", text)

    def test_pruning_everything_yields_empty_output(self):
        state = {"pruned_taxa": [
            "ABC123 Amanita example", "DEF456", "GHI789 Russula example",
        ]}
        self.assertEqual(build_edited_fasta_text(self.fasta, state), "")

    # --- recomputation must keep original identifiers ---------------------

    def test_extract_pruned_fasta_ignores_renames(self):
        """Recompute input keeps ORIGINAL headers even when tips are renamed."""
        state = {
            "pruned_taxa": [],
            "renames": {"ABC123 Amanita example": "Amanita muscaria voucher ABC123"},
        }
        out = Path(self._tmp.name) / "recompute_input.fasta"
        extract_pruned_fasta(self.fasta, state, out)
        headers = [h for h, _ in _parse(out.read_text())]
        self.assertIn("ABC123 Amanita example", headers)
        self.assertNotIn("Amanita muscaria voucher ABC123", headers)


if __name__ == "__main__":
    unittest.main()
