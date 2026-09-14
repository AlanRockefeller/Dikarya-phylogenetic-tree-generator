"""A GenBank deposit and its iNaturalist record are one collection.

The observation number that ties them together is not in the FASTA anywhere:
MycoMap's DB39 export gives accession/taxon/voucher/location, and NCBI's header
gives the DEFINITION line. It is in the GenBank *record* -- PX860295 says
"isolate S. D. Russell iNat # 280384724" -- so the dedup has to read the
annotation before it can group the two, and the surviving tip has to keep both
identifiers or the link the user just saw disappears from the tree.

The lookup itself is stubbed here; these tests are about what the dedup does
with the answer, not about NCBI being reachable.
"""

import unittest

from app.services import sequence_dedup_service as dedup
from app.services.genbank_observation_service import (
    observation_reference_from_record,
)

# A concrete read, so the overlap comparison has something real to anchor on: a
# repeating motif lines up in several frames at once and scores as a mismatch.
SEQUENCE = (
    "TCCGTAGGTGAACCTGCGGAAGGATCATTATTGAATTTAGGGCACTTTCTGGCTTTGGCTGAGCCATCTC"
    "ACCCTTTGTGCACCATTTGTAGACCTTGGTGTGATAAGTTGTCTGTGCTTTTACACACATGTGCATGTTT"
    "GAGTGTCATTAAATTCTCAACCTCTAACAGTCTTTGAGGCAATTTGGATTTGGGGGTTTGCTGGCTTTAT"
    "ATGAGTCAGCTCCTCTTAAATGCATTAGCGGGATCTCTGCGTTTCAATGTGTAGGTGTTTTCTGCTTTAT"
    "TTGCTATATCCATGTGTCTATGTAAGTTTATATAGCTTCTAACCGTCTCTTGAAGAGTTTGGAGATTGAT"
    "CTTGGCTTTCCTCTTGCACAGGACAATTAAATGTCTTTGAAAGAAGTCCTTGCAGGTTTTGGTCTAAGAA"
)


def _fasta(*records):
    return "".join(f">{name}\n{sequence}\n" for name, sequence in records)


class GenBankObservationReferenceTests(unittest.TestCase):
    def test_reference_is_read_from_the_definition_line(self):
        record = {
            "definition": ("Panaeolus cinctulus isolate S. D. Russell iNat # "
                           "280384724 small subunit ribosomal RNA gene, partial"),
            "source_features": {},
            "blob": "",
        }
        self.assertEqual(observation_reference_from_record(record), "inat:280384724")

    def test_reference_is_read_from_an_isolate_qualifier(self):
        record = {
            "definition": "Panaeolus cinctulus internal transcribed spacer",
            "source_features": {"isolate": "S. D. Russell iNat # 280384724"},
            "blob": "",
        }
        self.assertEqual(observation_reference_from_record(record), "inat:280384724")

    def test_a_record_with_no_observation_number_resolves_to_nothing(self):
        record = {
            "definition": "Amanita muscaria voucher MICH 12345 ITS region",
            "source_features": {"specimen_voucher": "MICH 12345"},
            "blob": "Amanita muscaria voucher MICH 12345 ITS region",
        }
        self.assertEqual(observation_reference_from_record(record), "")


class ObservationDedupTests(unittest.TestCase):
    def setUp(self):
        self._real_lookup = dedup._resolve_genbank_references

    def tearDown(self):
        dedup._resolve_genbank_references = self._real_lookup

    def _stub_lookup(self, mapping):
        """Answer as NCBI would for `mapping`, without going near the network."""
        def _resolve(records, references, accessions):
            filled = 0
            for index, accession in enumerate(accessions):
                if accession and not references[index] and accession in mapping:
                    references[index] = mapping[accession]
                    filled += 1
            return filled

        dedup._resolve_genbank_references = _resolve

    def test_genbank_and_inat_records_of_one_observation_collapse(self):
        self._stub_lookup({"PX860295": "inat:280384724"})
        fasta = _fasta(
            ("PX860295 Panaeolus cinctulus South Carolina US", SEQUENCE),
            ("iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
             SEQUENCE[20:-20]),
        )

        text, _metadata, removed = dedup.dedupe_by_observation(fasta)

        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["observation_reference"], "inat:280384724")
        # The longer read survives, not whichever came first.
        self.assertEqual(text.count(">"), 1)
        self.assertIn(">PX860295 iNat280384724 Panaeolus cinctulus South Carolina US",
                      text)

    def test_the_surviving_tip_keeps_the_accession_when_the_import_is_longer(self):
        self._stub_lookup({"PX860295": "inat:280384724"})
        fasta = _fasta(
            ("PX860295 Panaeolus cinctulus South Carolina US", SEQUENCE[20:-20]),
            ("iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
             SEQUENCE),
        )

        text, _metadata, removed = dedup.dedupe_by_observation(fasta)

        self.assertEqual(len(removed), 1)
        self.assertIn(
            ">iNat280384724 PX860295 Panaeolus cinctulus Greenwood South Carolina US",
            text,
        )
        # Both names travel with the removal so the rebuild-with-duplicates
        # action can put the original label back.
        self.assertEqual(
            removed[0]["kept_original_name"],
            "iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
        )

    def test_metadata_follows_the_merged_label(self):
        self._stub_lookup({"PX860295": "inat:280384724"})
        fasta = _fasta(
            ("PX860295 Panaeolus cinctulus South Carolina US", SEQUENCE),
            ("iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
             SEQUENCE[20:-20]),
        )
        metadata = [
            {"name": "PX860295 Panaeolus cinctulus South Carolina US",
             "fasta_header": "PX860295 Panaeolus cinctulus South Carolina US",
             "accession": "PX860295"},
            {"name": "iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
             "fasta_header": "iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
             "internal_id": "iNat280384724"},
        ]

        _text, kept_metadata, _removed = dedup.dedupe_by_observation(fasta, metadata)

        self.assertEqual(len(kept_metadata), 1)
        self.assertEqual(kept_metadata[0]["fasta_header"],
                         "PX860295 iNat280384724 Panaeolus cinctulus South Carolina US")
        # The label the record arrived with is still recoverable.
        self.assertEqual(kept_metadata[0]["raw_fasta_header"],
                         "PX860295 Panaeolus cinctulus South Carolina US")

    def test_two_different_observations_are_never_merged(self):
        self._stub_lookup({"PX860295": "inat:280384724",
                           "PX860296": "inat:999999999"})
        fasta = _fasta(
            ("PX860295 Panaeolus cinctulus South Carolina US", SEQUENCE),
            ("PX860296 Panaeolus cinctulus South Carolina US", SEQUENCE),
        )

        text, _metadata, removed = dedup.dedupe_by_observation(fasta)

        self.assertEqual(removed, [])
        self.assertEqual(text.count(">"), 2)

    def test_an_identifier_already_in_the_label_is_not_repeated(self):
        self._stub_lookup({"PX860295": "inat:280384724"})
        fasta = _fasta(
            ("PX860295 iNat280384724 Panaeolus cinctulus South Carolina US", SEQUENCE),
            ("iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
             SEQUENCE[20:-20]),
        )

        text, _metadata, removed = dedup.dedupe_by_observation(fasta)

        self.assertEqual(len(removed), 1)
        self.assertEqual(text.count("iNat280384724"), 1)

    def test_the_lookup_can_be_switched_off(self):
        def _explode(*args, **kwargs):
            raise AssertionError("NCBI must not be consulted")

        dedup._resolve_genbank_references = _explode
        fasta = _fasta(
            ("PX860295 Panaeolus cinctulus South Carolina US", SEQUENCE),
            ("iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
             SEQUENCE[20:-20]),
        )

        _text, _metadata, removed = dedup.dedupe_by_observation(
            fasta, resolve_genbank_references=False
        )

        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main()
