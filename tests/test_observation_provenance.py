"""MO123456 is two different things, and dedup deletes records.

"MO" is a real two-letter INSDC accession prefix, so the compact token
``MO123456`` is simultaneously:

* the Mushroom Observer label Dikarya prints on tips -- 26,219 records on disk
  lead with it; and
* a syntactically valid GenBank nucleotide accession, 2 letters + 6 digits,
  which is the single most common accession shape there is.

Reading one as the other is not cosmetic. ``dedupe_by_observation`` *removes*
records that share an observation reference, so a GenBank accession misread as
an observation number would collapse two unrelated collections into one tip and
throw the other away.

The discriminator is provenance, not a network call. Every GenBank record
Dikarya fetches carries source/hit_source metadata saying so, and a version
suffix (``MO123456.1``) settles it even when it does not. Explicit Mushroom
Observer references -- ``mo:123456``, ``MO #123456``, a mushroomobserver.org
URL, the site's name spelled out -- have no accession shape at all and are
honoured from any source, including from inside a GenBank record's own
qualifiers.

Where provenance is genuinely undecidable the rule is to prefer a false
negative: two tips the user can merge by hand beats one tip and a deleted
record.
"""

import unittest

from app.services import sequence_dedup_service as dedup
from app.services.genbank_observation_service import (
    observation_reference_from_record,
)
from app.services.mycomap_service import (
    extract_mycomap_observation_reference,
    extract_mycomap_observation_references,
)

# A concrete read, so the overlap comparison has something real to anchor on.
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


class CompactMoTokenTests(unittest.TestCase):
    def test_the_compact_token_is_read_as_an_observation_by_default(self):
        self.assertEqual(
            extract_mycomap_observation_reference("MO123456"), "mo:123456"
        )

    def test_the_compact_token_can_be_suppressed(self):
        self.assertIsNone(
            extract_mycomap_observation_reference("MO123456", allow_compact_mo=False)
        )

    def test_every_explicit_form_survives_suppression(self):
        for text in (
            "mo:123456",
            "MO #123456",
            "MO-123456",
            "MO 123456",
            "https://mushroomobserver.org/123456",
            "mushroomobserver.org/obs/123456",
            "Mushroom Observer 123456",
            "Panaeolus cinctulus isolate Mushroom Observer #123456 ITS",
        ):
            self.assertEqual(
                extract_mycomap_observation_reference(text, allow_compact_mo=False),
                "mo:123456",
                text,
            )

    def test_inaturalist_references_are_unaffected(self):
        for text in ("iNat280384724", "inat:280384724",
                     "https://www.inaturalist.org/observations/280384724"):
            self.assertEqual(
                extract_mycomap_observation_reference(text, allow_compact_mo=False),
                "inat:280384724",
                text,
            )


class ProvenanceTests(unittest.TestCase):
    def test_genbank_metadata_marks_a_record_as_genbank(self):
        for metadata in ({"source": "genbank"}, {"hit_source": "ncbi"},
                         {"source": "mycomap", "hit_source": "ncbi"}):
            self.assertEqual(
                dedup.record_provenance("MO123456 Amanita", metadata), "genbank"
            )

    def test_observation_metadata_marks_a_record_as_an_observation(self):
        for metadata in ({"source": "mushroom_observer"},
                         {"source": "mycomap", "hit_source": "local"},
                         {"source": "inaturalist"},
                         {"observation_id": "123456"}):
            self.assertEqual(
                dedup.record_provenance("MO123456 Pluteus", metadata), "observation"
            )

    def test_a_version_suffix_settles_it_without_metadata(self):
        # Mushroom Observer labels have never carried one; GenBank accessions do.
        self.assertEqual(dedup.record_provenance("MO123456.1 Amanita", {}), "genbank")

    def test_a_bare_label_with_no_metadata_stays_unknown(self):
        self.assertEqual(dedup.record_provenance("MO123456 Pluteus", {}), "unknown")


class ObservationReferenceTests(unittest.TestCase):
    def test_an_unknown_compact_mo_token_is_not_an_observation(self):
        self.assertEqual(
            dedup.observation_reference("MO123456 Pluteus", {}), ""
        )

    def test_a_genbank_accession_beginning_with_mo_is_not_an_observation(self):
        self.assertEqual(
            dedup.observation_reference(
                "MO123456.1 Amanita muscaria voucher MICH 12345 ITS",
                {"source": "genbank"},
            ),
            "",
        )

    def test_a_versioned_mo_accession_is_not_an_observation_without_metadata(self):
        self.assertEqual(
            dedup.observation_reference("MO123456.1 Amanita muscaria ITS", {}), ""
        )

    def test_a_mushroom_observer_record_still_resolves(self):
        self.assertEqual(
            dedup.observation_reference(
                "MO123456 Pluteus New Harmony Indiana US",
                {"source": "mycomap", "hit_source": "local",
                 "internal_id": "MO123456"},
            ),
            "mo:123456",
        )

    def test_an_explicit_reference_resolves_even_on_a_genbank_record(self):
        self.assertEqual(
            dedup.observation_reference(
                "PX860295 Panaeolus cinctulus isolate Mushroom Observer 123456",
                {"source": "genbank", "accession": "PX860295"},
            ),
            "mo:123456",
        )

    def test_a_genbank_accession_beginning_with_mo_is_still_an_accession(self):
        self.assertEqual(
            dedup.record_accession("MO123456.1 Amanita muscaria",
                                   {"source": "genbank"}),
            "MO123456.1",
        )

    def test_a_mushroom_observer_label_is_never_reported_as_an_accession(self):
        # 19,661 MycoMap local hits carry MO###### as their internal_id. Reading
        # those as accessions sent observation numbers to NCBI efetch and
        # offered them to the label merge as "the accession this tip lacks".
        self.assertEqual(
            dedup.record_accession(
                "MO123456 Pluteus New Harmony Indiana US",
                {"source": "mycomap", "hit_source": "local",
                 "internal_id": "MO123456"},
            ),
            "",
        )


class DestructiveDedupTests(unittest.TestCase):
    def test_unknown_compact_mo_does_not_collapse_with_a_known_observation(self):
        fasta = _fasta(
            ("MO123456 unknown provenance", SEQUENCE),
            ("MO123456 known Mushroom Observer record", SEQUENCE[20:-20]),
        )
        metadata = [
            {},
            {"source": "mushroom_observer", "internal_id": "MO123456"},
        ]

        text, kept_metadata, removed = dedup.dedupe_by_observation(fasta, metadata)

        self.assertEqual(removed, [])
        self.assertEqual(text.count(">"), 2)
        self.assertEqual(len(kept_metadata), 2)

    def test_a_genbank_record_and_an_observation_are_not_collapsed_by_the_prefix(self):
        """The case the whole change exists for.

        GenBank accession MO123456 and Mushroom Observer observation 123456 are
        different collections whose identifiers merely look alike. Their
        sequences are similar enough to pass the difference test, so only the
        reference keeps them apart.
        """
        fasta = _fasta(
            ("MO123456.1 Amanita muscaria voucher MICH 12345", SEQUENCE),
            ("MO123456 Pluteus New Harmony Indiana US", SEQUENCE[20:-20]),
        )
        metadata = [
            {"name": "MO123456.1 Amanita muscaria voucher MICH 12345",
             "fasta_header": "MO123456.1 Amanita muscaria voucher MICH 12345",
             "source": "genbank", "accession": "MO123456.1"},
            {"name": "MO123456 Pluteus New Harmony Indiana US",
             "fasta_header": "MO123456 Pluteus New Harmony Indiana US",
             "source": "mycomap", "hit_source": "local", "internal_id": "MO123456"},
        ]

        text, kept_metadata, removed = dedup.dedupe_by_observation(fasta, metadata)

        self.assertEqual(removed, [])
        self.assertEqual(text.count(">"), 2)
        self.assertEqual(len(kept_metadata), 2)

    def test_two_records_of_one_mushroom_observer_observation_still_collapse(self):
        fasta = _fasta(
            ("MO123456 Pluteus New Harmony Indiana US", SEQUENCE),
            ("MO123456 Pluteus cervinus", SEQUENCE[20:-20]),
        )
        metadata = [
            {"name": "MO123456 Pluteus New Harmony Indiana US",
             "fasta_header": "MO123456 Pluteus New Harmony Indiana US",
             "source": "mycomap", "hit_source": "local"},
            {"name": "MO123456 Pluteus cervinus",
             "fasta_header": "MO123456 Pluteus cervinus",
             "source": "mushroom_observer"},
        ]

        _text, _kept, removed = dedup.dedupe_by_observation(fasta, metadata)

        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["observation_reference"], "mo:123456")


class ConflictingGenBankReferenceTests(unittest.TestCase):
    def test_one_reference_repeated_across_fields_is_not_a_conflict(self):
        record = {
            "definition": "Panaeolus cinctulus isolate iNat # 280384724 ITS",
            "source_features": {
                "isolate": "S. D. Russell iNat 280384724",
                "note": "iNaturalist.org #280384724;",
            },
            "blob": "",
        }
        self.assertEqual(observation_reference_from_record(record), "inat:280384724")

    def test_two_different_observations_in_one_record_are_refused(self):
        # First-match-wins would have picked the definition line and deleted a
        # record on the strength of it.
        record = {
            "definition": "Panaeolus cinctulus isolate iNat # 280384724 ITS",
            "source_features": {"isolate": "S. D. Russell iNat 999999999"},
            "blob": "",
        }
        with self.assertLogs("app.services.genbank_observation_service",
                             level="WARNING") as logs:
            self.assertEqual(observation_reference_from_record(record), "")
        self.assertIn("genbank_reference_ambiguous", logs.output[0])

    def test_an_inat_and_an_mo_reference_in_one_record_are_refused(self):
        record = {
            "definition": "Panaeolus cinctulus isolate iNat # 280384724 ITS",
            "source_features": {"note": "Mushroom Observer 123456"},
            "blob": "",
        }
        with self.assertLogs("app.services.genbank_observation_service",
                             level="WARNING"):
            self.assertEqual(observation_reference_from_record(record), "")

    def test_two_observations_in_ONE_field_are_refused(self):
        """Ambiguity does not need two fields to disagree.

        A single /note naming two observations is exactly as undecidable as two
        fields naming different ones, and taking the first would group the
        record with one of them on no evidence -- then delete whatever it
        collided with.
        """
        record = {
            "definition": "Panaeolus cinctulus ITS region",
            "source_features": {
                "note": "sequenced from iNat 280384724; compare iNat 999999999",
            },
            "blob": "",
        }
        with self.assertLogs("app.services.genbank_observation_service",
                             level="WARNING") as logs:
            self.assertEqual(observation_reference_from_record(record), "")
        self.assertIn("genbank_reference_ambiguous", logs.output[0])

    def test_two_observations_in_ONE_definition_line_are_refused(self):
        record = {
            "definition": ("Panaeolus cinctulus isolate iNat # 280384724 "
                           "voucher for iNat # 999999999 ITS"),
            "source_features": {},
            "blob": "",
        }
        with self.assertLogs("app.services.genbank_observation_service",
                             level="WARNING"):
            self.assertEqual(observation_reference_from_record(record), "")

    def test_two_observations_in_the_blob_are_refused(self):
        # The blob is the whole record run together, so it is the candidate
        # most likely to name two different things.
        record = {
            "definition": "Amanita muscaria ITS region",
            "source_features": {},
            "blob": "Amanita muscaria ITS; iNat 280384724; see also iNat 999999999",
        }
        with self.assertLogs("app.services.genbank_observation_service",
                             level="WARNING") as logs:
            self.assertEqual(observation_reference_from_record(record), "")
        self.assertIn("source=blob", logs.output[0])

    def test_an_inat_and_an_mo_reference_in_one_field_are_refused(self):
        record = {
            "definition": "Panaeolus cinctulus ITS",
            "source_features": {
                "isolate": "iNat 280384724 / Mushroom Observer 123456",
            },
            "blob": "",
        }
        with self.assertLogs("app.services.genbank_observation_service",
                             level="WARNING"):
            self.assertEqual(observation_reference_from_record(record), "")

    def test_one_observation_written_several_ways_in_one_field_is_not_a_conflict(self):
        # Three patterns match this single occurrence; the same number is the
        # same observation however it is spelled.
        record = {
            "definition": "Panaeolus cinctulus ITS",
            "source_features": {
                "note": "iNaturalist.org #280384724; iNat # 280384724; inat:280384724",
            },
            "blob": "",
        }
        self.assertEqual(observation_reference_from_record(record), "inat:280384724")

    def test_the_blob_is_only_consulted_when_the_trusted_fields_say_nothing(self):
        # An incidental number in a citation or a primer name must not outrank a
        # clean /isolate.
        record = {
            "definition": "Amanita muscaria ITS region",
            "source_features": {"isolate": "iNat 280384724"},
            "blob": "Amanita muscaria ITS region iNat 999999999 direct submission",
        }
        self.assertEqual(observation_reference_from_record(record), "inat:280384724")

    def test_the_blob_still_rescues_a_record_with_nothing_structured(self):
        record = {
            "definition": "Amanita muscaria ITS region",
            "source_features": {},
            "blob": "Amanita muscaria ITS region; iNaturalist observation 280384724",
        }
        self.assertEqual(observation_reference_from_record(record), "inat:280384724")

    def test_a_genbank_accession_beginning_with_mo_never_reads_as_an_observation(self):
        record = {
            "definition": "Amanita muscaria ITS region",
            "source_features": {"specimen_voucher": "MO123456"},
            "blob": "Amanita muscaria ITS region MO123456",
        }
        self.assertEqual(observation_reference_from_record(record), "")

    def test_a_record_that_names_an_observation_explicitly_is_still_linked(self):
        record = {
            "definition": "Panaeolus cinctulus ITS region",
            "source_features": {"isolate": "Mushroom Observer #123456"},
            "blob": "",
        }
        self.assertEqual(observation_reference_from_record(record), "mo:123456")


class IdentifierPresenceTests(unittest.TestCase):
    """One identifier is not another just because it is a substring."""

    def test_a_longer_id_does_not_contain_a_shorter_one(self):
        self.assertFalse(
            dedup._contains_identifier("iNat2803847241 Panaeolus", "iNat280384724")
        )

    def test_the_same_id_is_found(self):
        self.assertTrue(
            dedup._contains_identifier("iNat280384724 Panaeolus", "iNat280384724")
        )

    def test_punctuation_and_spacing_variants_still_match(self):
        for name in (
            "PX860295 iNat280384724 Panaeolus cinctulus",
            'Lepiota "clypeolaria-IN02" iNat 280384724',
            "Lepiota clypeolaria-IN02 iNat #280384724",
            "Panaeolus (iNat_280384724) South Carolina",
        ):
            self.assertTrue(
                dedup._contains_identifier(name, "iNat280384724"), name
            )

    def test_an_accession_with_a_version_already_carries_the_bare_form(self):
        self.assertTrue(dedup._contains_identifier("PX860295.1 Panaeolus", "PX860295"))

    def test_an_accession_inside_a_longer_number_does_not_count(self):
        self.assertFalse(dedup._contains_identifier("PX8602951 Panaeolus", "PX860295"))

    def test_a_missing_identifier_is_reported_missing(self):
        self.assertFalse(
            dedup._contains_identifier("Panaeolus cinctulus", "PX860295")
        )

    def test_an_empty_identifier_is_never_present(self):
        self.assertFalse(dedup._contains_identifier("PX860295", ""))

    def test_the_merge_adds_an_id_whose_prefix_was_already_there(self):
        """The bug the substring test caused, end to end.

        Two neighbouring observations, 280384724 and 2803847241. The surviving
        tip carried the longer one, so the shorter one read as "already
        present" and was silently dropped from the merged label.
        """
        merged = dedup._merge_identifier_into_name(
            "iNat2803847241 Panaeolus cinctulus", "iNat280384724"
        )
        self.assertIn("iNat2803847241", merged)
        self.assertIn("iNat280384724 ", merged + " ")
        self.assertEqual(merged.count("iNat"), 2)

    def test_an_id_genuinely_already_present_is_not_repeated(self):
        merged = dedup._merge_identifier_into_name(
            "PX860295 iNat280384724 Panaeolus", "iNat280384724"
        )
        self.assertEqual(merged.count("iNat280384724"), 1)


class ReferenceExtractionConsistencyTests(unittest.TestCase):
    """The singular and plural extractors must not drift apart.

    The singular form short-circuits on the first matching pattern (it is on
    the per-record hot path); the plural runs them all. They share the same
    pattern tuples, and the plural preserves the singular's priority ordering,
    so the first element of one is always the answer of the other.
    """

    CASES = (
        "",
        "iNat280384724",
        "iNat 280384724 and iNat 999999999",
        "Mushroom Observer 123456 and iNat 280384724",
        "MO123456",
        "mo:123456",
        "MO #123456 iNat 280384724",
        "https://www.inaturalist.org/observations/280384724",
        "https://mushroomobserver.org/123456",
        "Panaeolus cinctulus voucher MICH 12345 ITS region",
        "iNaturalist.org #280384724; iNat # 280384724",
    )

    def test_the_first_plural_result_is_the_singular_result(self):
        for text in self.CASES:
            for allow in (True, False):
                plural = extract_mycomap_observation_references(
                    text, allow_compact_mo=allow
                )
                singular = extract_mycomap_observation_reference(
                    text, allow_compact_mo=allow
                )
                self.assertEqual(
                    plural[0] if plural else None, singular, (text, allow)
                )

    def test_distinct_references_are_returned_in_priority_order(self):
        self.assertEqual(
            extract_mycomap_observation_references(
                "Mushroom Observer 123456 and iNat 280384724"
            ),
            ["inat:280384724", "mo:123456"],
        )

    def test_a_repeated_reference_appears_once(self):
        self.assertEqual(
            extract_mycomap_observation_references(
                "iNat 280384724 iNat 280384724 inat:280384724"
            ),
            ["inat:280384724"],
        )

    def test_suppressing_the_compact_form_drops_only_that(self):
        self.assertEqual(
            extract_mycomap_observation_references(
                "MO123456 and mo:999999", allow_compact_mo=False
            ),
            ["mo:999999"],
        )


if __name__ == "__main__":
    unittest.main()
