"""Collapsing two records and putting them back must be lossless.

When observation dedup collapses a GenBank record and its iNaturalist record
into one tip, the survivor absorbs the other's identifier: the tip reads
"PX860295 iNat280384724 Panaeolus ..." so a user can trace it to both databases.
That relabelling has to be reversible, because the viewer offers "rebuild
including duplicates", and a rebuild that put the collapsed record back beside a
still-merged survivor would show the same identifier twice and leave a label
nobody ever wrote.

Both directions matter, and they take different code paths: which record
survives is decided by length (longest read wins), so either the GenBank record
or the observation record can be the one that absorbs the label.

A second pass over an already-deduped payload also has to accumulate rather than
replace: the worker runs dedup again with the NCBI annotation lookup enabled,
and overwriting import_filter_details would leave the viewer listing only the
second pass's removals while the first pass's records were already gone from the
FASTA with nothing to rebuild them from.
"""

import unittest

from app.api.routes import _restore_merged_labels
from app.services import sequence_dedup_service as dedup

SEQUENCE = (
    "TCCGTAGGTGAACCTGCGGAAGGATCATTATTGAATTTAGGGCACTTTCTGGCTTTGGCTGAGCCATCTC"
    "ACCCTTTGTGCACCATTTGTAGACCTTGGTGTGATAAGTTGTCTGTGCTTTTACACACATGTGCATGTTT"
    "GAGTGTCATTAAATTCTCAACCTCTAACAGTCTTTGAGGCAATTTGGATTTGGGGGTTTGCTGGCTTTAT"
    "ATGAGTCAGCTCCTCTTAAATGCATTAGCGGGATCTCTGCGTTTCAATGTGTAGGTGTTTTCTGCTTTAT"
    "TTGCTATATCCATGTGTCTATGTAAGTTTATATAGCTTCTAACCGTCTCTTGAAGAGTTTGGAGATTGAT"
    "CTTGGCTTTCCTCTTGCACAGGACAATTAAATGTCTTTGAAAGAAGTCCTTGCAGGTTTTGGTCTAAGAA"
)

GENBANK_LABEL = "PX860295 Panaeolus cinctulus South Carolina US"
OBSERVATION_LABEL = "iNat280384724 Panaeolus cinctulus Greenwood South Carolina US"


def _fasta(*records):
    return "".join(f">{name}\n{sequence}\n" for name, sequence in records)


class MergedLabelRoundTripTests(unittest.TestCase):
    def setUp(self):
        self._real_lookup = dedup._resolve_genbank_references

        def _resolve(records, references, accessions):
            filled = 0
            for index, accession in enumerate(accessions):
                if accession and not references[index] and accession == "PX860295":
                    references[index] = "inat:280384724"
                    filled += 1
            return filled

        dedup._resolve_genbank_references = _resolve
        self.addCleanup(
            setattr, dedup, "_resolve_genbank_references", self._real_lookup
        )

    def _round_trip(self, fasta):
        job_params = {"sequence": fasta, "sequence_metadata": []}
        removed_count = dedup.apply_observation_dedup(
            job_params, resolve_genbank_references=True
        )
        self.assertEqual(removed_count, 1)
        duplicates = job_params["import_filter_details"]["duplicates"]
        rebuilt = _restore_merged_labels(
            job_params["sequence"], duplicates["merged_labels"]
        )
        return job_params, duplicates, rebuilt

    def test_genbank_survives_and_the_observation_label_is_restored(self):
        # The GenBank read is longer, so it survives and absorbs iNat280384724.
        fasta = _fasta((GENBANK_LABEL, SEQUENCE),
                       (OBSERVATION_LABEL, SEQUENCE[20:-20]))

        job_params, duplicates, rebuilt = self._round_trip(fasta)

        self.assertIn(">PX860295 iNat280384724 Panaeolus", job_params["sequence"])
        self.assertEqual(duplicates["removed_records"][0]["name"], OBSERVATION_LABEL)
        # And the merged label unwinds to exactly the original.
        self.assertIn(f">{GENBANK_LABEL}", rebuilt)
        self.assertNotIn("iNat280384724", rebuilt)

    def test_the_observation_survives_and_the_accession_is_restored(self):
        # Same pair, opposite lengths: now the import is the longer read.
        fasta = _fasta((GENBANK_LABEL, SEQUENCE[20:-20]),
                       (OBSERVATION_LABEL, SEQUENCE))

        job_params, duplicates, rebuilt = self._round_trip(fasta)

        self.assertIn(">iNat280384724 PX860295 Panaeolus", job_params["sequence"])
        self.assertEqual(duplicates["removed_records"][0]["name"], GENBANK_LABEL)
        self.assertIn(f">{OBSERVATION_LABEL}", rebuilt)
        self.assertNotIn("PX860295", rebuilt)

    def test_the_removal_carries_both_names(self):
        fasta = _fasta((GENBANK_LABEL, SEQUENCE),
                       (OBSERVATION_LABEL, SEQUENCE[20:-20]))
        _job_params, duplicates, _rebuilt = self._round_trip(fasta)
        record = duplicates["removed_records"][0]
        self.assertEqual(record["merged_into_label"],
                         "PX860295 iNat280384724 Panaeolus cinctulus South Carolina US")
        self.assertEqual(record["kept_original_name"], GENBANK_LABEL)

    def test_a_rebuild_does_not_accumulate_merged_label_artifacts(self):
        """Restore, then dedup again: the label must not grow a second copy."""
        fasta = _fasta((GENBANK_LABEL, SEQUENCE),
                       (OBSERVATION_LABEL, SEQUENCE[20:-20]))
        job_params, duplicates, rebuilt = self._round_trip(fasta)

        restored = rebuilt.rstrip("\n") + "\n" + _fasta(
            (duplicates["removed_records"][0]["name"],
             duplicates["removed_records"][0]["sequence"])
        )
        second = {"sequence": restored, "sequence_metadata": []}
        dedup.apply_observation_dedup(second, resolve_genbank_references=True)

        # One merged tip, one of each identifier -- not "PX860295 iNat280384724
        # iNat280384724 ...".
        self.assertEqual(second["sequence"].count("PX860295"), 1)
        self.assertEqual(second["sequence"].count("iNat280384724"), 1)


class AccumulatedDetailsTests(unittest.TestCase):
    def _removal(self, name, kept, original=None):
        record = {"name": name, "sequence": "ACGT", "duplicate_of": kept,
                  "observation_reference": "inat:1", "difference_count": 0,
                  "reason": "duplicate_observation_record", "reason_label": "x"}
        if original:
            record["merged_into_label"] = kept
            record["kept_original_name"] = original
        return record

    def test_a_second_pass_adds_to_the_list_rather_than_replacing_it(self):
        job_params = {}
        dedup.record_dedup_details(job_params, [self._removal("a", "keep")])
        dedup.record_dedup_details(job_params, [self._removal("b", "keep")])

        duplicates = job_params["import_filter_details"]["duplicates"]
        self.assertEqual(duplicates["removed_count"], 2)
        self.assertEqual([r["name"] for r in duplicates["removed_records"]],
                         ["a", "b"])

    def test_the_same_removal_reported_twice_is_listed_once(self):
        job_params = {}
        dedup.record_dedup_details(job_params, [self._removal("a", "keep")])
        dedup.record_dedup_details(job_params, [self._removal("a", "keep")])

        duplicates = job_params["import_filter_details"]["duplicates"]
        self.assertEqual(duplicates["removed_count"], 1)

    def test_distinct_records_with_the_same_header_are_both_preserved(self):
        job_params = {}
        first = self._removal("same header", "keep")
        second = self._removal("same header", "keep")
        second["sequence"] = "TGCA"

        dedup.record_dedup_details(job_params, [first])
        dedup.record_dedup_details(job_params, [second])

        duplicates = job_params["import_filter_details"]["duplicates"]
        self.assertEqual(duplicates["removed_count"], 2)
        self.assertEqual(
            [record["sequence"] for record in duplicates["removed_records"]],
            ["ACGT", "TGCA"],
        )

    def test_a_tip_relabelled_twice_unwinds_to_its_first_label(self):
        """Two collapses onto one survivor, across two passes.

        The rebuild has to restore the label the tip started with, not the
        intermediate merged form it had after the first pass.
        """
        job_params = {}
        dedup.record_dedup_details(
            job_params, [self._removal("a", "PX1 iNat1", original="PX1")]
        )
        dedup.record_dedup_details(
            job_params,
            [self._removal("b", "PX1 iNat1 iNat2", original="PX1 iNat1")],
        )

        merged = job_params["import_filter_details"]["duplicates"]["merged_labels"]
        self.assertEqual(merged["PX1 iNat1 iNat2"], "PX1")
        self.assertEqual(merged["PX1 iNat1"], "PX1")


if __name__ == "__main__":
    unittest.main()
