"""What Dikarya accepts as a GenBank accession, and what it refuses.

Three separate questions, settled separately:

**Syntax.** INSDC nucleotide accessions have a small set of fixed shapes. The
large-scale families -- WGS (assembly contigs), TSA (assembled transcripts) and
TLS (Targeted Locus Study) -- share one structure and the accession string does
not say which family it is, so Dikarya accepts the syntax rather than trying to
tell them apart. That is deliberate: NCBI runs TLS projects for ITS/ITS2 and the
ribosomal loci, so an individual TLS record is very often exactly the kind of
targeted locus sequence this application exists for.

Per NCBI (https://www.ncbi.nlm.nih.gov/genbank/wgs/) and the December 2018
INSDC expansion, the real shapes are a 4-letter project code plus a 2-digit
assembly version plus 6-8 contig digits, and a 6-letter project code plus a
2-digit assembly version plus 7-9 contig digits. The intermediate digit counts
(4+9, 6+10) are real and were previously rejected.

**The iNaturalist collision.** "iNat" + a 9-digit observation id is
shape-identical to a 4+9 accession. This is not hypothetical: across the 11,670
job directories on disk, 318,227 submitted records lead with iNat + 9 digits and
25,824 more with iNat + 8 digits, against a grand total of three real
large-scale accessions ever submitted (AYNK01000855.1 twice, AYNK01002478.1
once -- both individual contigs, 4,104 and 2,618 bp). So the INAT prefix is
excluded by name.

**Master records.** A WGS/TSA/TLS project accession is the project header: it
lists the contigs and holds no bases. Fetching one returns an empty FASTA, and
the submission used to fail several steps later as "NCBI could not resolve this
accession". The shape is unambiguous -- after the 2-digit assembly version every
remaining digit is zero -- so it is named at the boundary instead.

**Length.** There is already an authoritative per-sequence maximum for remotely
fetched accessions, MAX_CUSTOM_GENBANK_SEQUENCE_BP, and it is enforced on both
user-facing accession paths. No new biological threshold is invented here.
"""

import unittest
from unittest import mock

from app.api import routes
from app.services.fasta_utils import (
    GENBANK_ACCESSION_RE,
    is_genbank_accession,
    is_insdc_master_accession,
)


class OrdinaryAccessionTests(unittest.TestCase):
    def test_the_everyday_shapes_are_accepted(self):
        for accession in (
            "U49845",            # 1 letter + 5 digits
            "OR807397",          # 2 letters + 6 digits -- the common case
            "AF123456.1",        # with a version
            "KY12345678",        # 2 letters + 8 digits
            "NC_012345",         # RefSeq
            "NM_001234567",      # RefSeq, 9 digits
            "or807397",          # case-insensitive
        ):
            self.assertTrue(is_genbank_accession(accession), accession)

    def test_things_that_are_not_accessions_are_refused(self):
        for token in (
            "", "   ", "Amanita", "OR80739", "OR8073978", "12345",
            "OR807397.", "OR807397.1.2", "OR-807397", "NC_12345",
        ):
            self.assertFalse(is_genbank_accession(token), repr(token))


class LargeScaleAccessionSyntaxTests(unittest.TestCase):
    def test_four_letter_projects_take_eight_through_ten_digits(self):
        for accession in (
            "AAAA01000001",      # 4 + 8  (2-digit version + 6 contig digits)
            "AAAA010000001",     # 4 + 9  (7 contig digits)
            "AAAA0100000001",    # 4 + 10 (8 contig digits)
            "AYNK01000855.1",    # a real one, from this host's own job history
        ):
            self.assertTrue(is_genbank_accession(accession), accession)

    def test_six_letter_projects_take_nine_through_eleven_digits(self):
        for accession in (
            "AAAAAA010000001",   # 6 + 9  (2-digit version + 7 contig digits)
            "AAAAAA0100000001",  # 6 + 10 (8 contig digits)
            "AAAAAA01000000001",  # 6 + 11 (9 contig digits)
        ):
            self.assertTrue(is_genbank_accession(accession), accession)

    def test_the_intermediate_digit_counts_were_the_gap(self):
        # 4+9 and 6+10 are valid INSDC and used to be rejected outright.
        self.assertTrue(GENBANK_ACCESSION_RE.match("AAAA010000001"))
        self.assertTrue(GENBANK_ACCESSION_RE.match("AAAAAA0100000001"))

    def test_digit_counts_outside_the_real_range_are_refused(self):
        for token in (
            "AAAA0100001",       # 4 + 7, too short
            "AAAA01000000001",   # 4 + 11, too long
            "AAAAAA01000001",    # 6 + 8, too short
            "AAAAAA010000000001",  # 6 + 12, too long
        ):
            self.assertFalse(is_genbank_accession(token), token)

    def test_a_tls_accession_is_accepted_like_any_other(self):
        # TLS four-letter prefixes begin with K; an individual TLS record is a
        # targeted locus sequence, which is what this application is for.
        self.assertTrue(is_genbank_accession("KAAA01000001"))


class INaturalistCollisionTests(unittest.TestCase):
    def test_an_inaturalist_observation_id_is_not_an_accession(self):
        for token in (
            "INAT125467754",     # 4 + 9, the shape the broadening introduced
            "iNat280384724",
            "iNat12546775",      # 4 + 8, matched even before the broadening
            "INAT1254677543",    # 4 + 10
        ):
            self.assertFalse(is_genbank_accession(token), token)

    def test_a_real_four_letter_project_code_still_matches(self):
        self.assertTrue(is_genbank_accession("AYNK01000855"))

    def test_the_exclusion_is_by_prefix_not_by_digit_count(self):
        # Refusing 4+9 outright would be the wrong fix: it is valid INSDC.
        self.assertTrue(is_genbank_accession("ABCD010000001"))
        self.assertFalse(is_genbank_accession("INAT010000001"))


class RealNCBIRecordTests(unittest.TestCase):
    """Anchored on records verified against NCBI on 2026-09-14, not invented.

    The synthetic AAAA./ABCDEF. cases above prove the syntax; these prove the
    policy is about the right real records. Each was checked with efetch:

      KJLX01000001  TLS, "fungal sp. ASV_00001 ribosomal RNA internal
                    transcribed spacer region", 293 bp -- an individual
                    targeted-locus ITS record, i.e. exactly Dikarya's subject
                    matter. Accepting it is the point of keeping the large-scale
                    arms at all.
      KJLX00000000  the TLS project header for that same study. `efetch
                    rettype=fasta` returns an EMPTY body for it -- which is the
                    mystery the master check exists to convert into a sentence.
      AYNK01000855  WGS, "Amanita jacksonii TRTC168611 ... contig-12000001,
                    whole genome shotgun sequence", 4,104 bp. The only
                    large-scale accession real users have ever submitted here
                    (3 occurrences across 11,671 job directories).
      ABCDEF0...    NCBI's own documented trio for the 6-letter form: "a
                    minimum of 9 digits, eg XXXXXX000000000, XXXXXX010000000,
                    and XXXXXX010000001, for the wgs project, its first version,
                    and its first sequence" (ncbi.nlm.nih.gov/genbank/wgs/).
    """

    def test_an_individual_fungal_tls_its_record_is_accepted(self):
        self.assertTrue(is_genbank_accession("KJLX01000001"))
        self.assertFalse(is_insdc_master_accession("KJLX01000001"))

    def test_the_tls_project_header_is_refused_as_a_master_record(self):
        # These surface in an ordinary NCBI search for fungal ITS, so a user
        # really can paste one; it must not reach the pipeline as an empty FASTA.
        for master in ("KJLX00000000", "KJLF00000000", "KIWT00000000",
                       "TADJ00000000"):
            self.assertTrue(is_genbank_accession(master), master)
            self.assertTrue(is_insdc_master_accession(master), master)

    def test_the_only_wgs_accession_users_have_submitted_still_works(self):
        for accession in ("AYNK01000855.1", "AYNK01002478.1"):
            self.assertTrue(is_genbank_accession(accession), accession)
            self.assertFalse(is_insdc_master_accession(accession), accession)

    def test_ncbis_documented_six_letter_trio(self):
        self.assertTrue(is_insdc_master_accession("ABCDEF000000000"))
        self.assertTrue(is_insdc_master_accession("ABCDEF010000000"))
        self.assertFalse(is_insdc_master_accession("ABCDEF010000001"))
        self.assertTrue(is_genbank_accession("ABCDEF010000001"))


class MasterAccessionTests(unittest.TestCase):
    def test_a_project_header_is_recognised(self):
        for accession in (
            "AAAA00000000",       # the project
            "AAAA01000000",       # the project's first assembly version
            "AAAA010000000",      # 4 + 9 form
            "AAAAAA000000000",
            "AAAAAA010000000",
            "aaaa01000000",
            "AAAA01000000.1",
        ):
            self.assertTrue(is_insdc_master_accession(accession), accession)

    def test_an_individual_record_is_not_a_master_record(self):
        for accession in (
            "AAAA01000001", "AAAA010000001", "AAAAAA010000001",
            "AYNK01000855.1", "OR807397", "NC_012345", "U49845",
        ):
            self.assertFalse(is_insdc_master_accession(accession), accession)

    def test_a_master_record_is_still_a_syntactically_valid_accession(self):
        # It has to be, or the user gets "Invalid GenBank accession" instead of
        # an explanation of what a master record is.
        self.assertTrue(is_genbank_accession("AAAA01000000"))

    def test_an_observation_id_shaped_like_one_is_not_a_master_record(self):
        self.assertFalse(is_insdc_master_accession("INAT01000000"))

    def test_the_rejection_names_the_accession_and_says_what_to_do(self):
        message = routes._master_accession_error(["OR807397", "AAAA01000000"])
        self.assertIsNotNone(message)
        self.assertIn("AAAA01000000", message)
        self.assertNotIn("OR807397", message)
        self.assertIn("project/master accession", message)
        self.assertIn("contains no sequence", message)
        self.assertIn("individual", message)

    def test_an_ordinary_batch_is_not_rejected(self):
        self.assertIsNone(
            routes._master_accession_error(["OR807397", "AYNK01000855.1", "U49845"])
        )

    def test_several_master_records_are_reported_together(self):
        message = routes._master_accession_error(["AAAA01000000", "BBBB00000000"])
        self.assertIn("AAAA01000000", message)
        self.assertIn("BBBB00000000", message)
        self.assertIn("project/master accessions", message)

    def test_both_accession_entry_points_check_before_fetching(self):
        source = open(routes.__file__).read()
        # /api/genbank/accessions and the Tree Builder's add-sequences path,
        # not counting the definition itself.
        self.assertEqual(
            source.count("master_error = _master_accession_error(accessions)"), 2
        )
        # Each one bails out before the fetch rather than after it.
        for index, _ in enumerate(range(2)):
            start = -1
            for _ in range(index + 1):
                start = source.index(
                    "master_error = _master_accession_error(accessions)", start + 1
                )
            window = source[start:start + 300]
            self.assertIn('"error": master_error}), 400', window)

    def test_the_fetch_helper_names_a_master_record_rather_than_not_found(self):
        """A master record answers with annotation and no bases.

        It therefore never reaches the length or empty-sequence branches; it
        falls out at the end as "this accession produced nothing". Calling that
        "not_found" sends the user looking for a typo in an accession that
        exists.
        """
        with mock.patch(
            "app.services.blast_service.fetch_fasta_for_accessions", return_value=""
        ):
            _sequences, skipped = routes._fetch_genbank_sequences_for_queue(
                ["AAAA01000000", "OR807397"]
            )
        reasons = {entry["accession"]: entry["reason"] for entry in skipped}
        self.assertEqual(reasons["AAAA01000000"], "master_record")
        self.assertEqual(reasons["OR807397"], "not_found")


class FetchedSequenceLengthTests(unittest.TestCase):
    """The existing per-sequence maximum, applied to remotely fetched records.

    No new threshold: MAX_CUSTOM_GENBANK_SEQUENCE_BP is the limit the accession
    entry paths have always used, and it is what stops an individual WGS contig
    of several hundred kb entering the alignment pipeline.
    """

    def _fetch(self, fasta):
        with mock.patch(
            "app.services.blast_service.fetch_fasta_for_accessions",
            return_value=fasta,
        ):
            return routes._fetch_genbank_sequences_for_queue(["AAAA01000001"])

    def test_a_record_at_the_limit_is_accepted(self):
        limit = routes.MAX_CUSTOM_GENBANK_SEQUENCE_BP
        sequences, skipped = self._fetch(f">AAAA01000001 contig\n{'A' * limit}\n")
        self.assertEqual(len(sequences), 1)
        self.assertEqual(len(sequences[0]["sequence"]), limit)
        self.assertEqual(skipped, [])

    def test_a_record_over_the_limit_is_skipped_and_reported(self):
        limit = routes.MAX_CUSTOM_GENBANK_SEQUENCE_BP
        sequences, skipped = self._fetch(f">AAAA01000001 contig\n{'A' * (limit + 1)}\n")
        self.assertEqual(sequences, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], "too_long")
        self.assertEqual(skipped[0]["length"], limit + 1)
        self.assertEqual(skipped[0]["max_length"], limit)

    def test_a_megabase_contig_never_reaches_the_queue(self):
        sequences, skipped = self._fetch(">AAAA01000001 contig\n" + "A" * 1_000_000)
        self.assertEqual(sequences, [])
        self.assertEqual(skipped[0]["reason"], "too_long")

    def test_a_record_with_no_bases_is_skipped_as_empty(self):
        sequences, skipped = self._fetch(">AAAA01000001 project header\n\n")
        self.assertEqual(sequences, [])
        # It never parsed as a record with sequence, so it falls through to the
        # per-accession pass, which names it for what it is.
        self.assertEqual(skipped[0]["accession"], "AAAA01000001")

    def test_both_user_facing_entry_points_pass_the_limit(self):
        source = open(routes.__file__).read()
        # Two call sites plus the helper's own default.
        self.assertEqual(
            source.count("max_sequence_bp=MAX_CUSTOM_GENBANK_SEQUENCE_BP"), 3
        )
        self.assertIn(
            "def _fetch_genbank_sequences_for_queue(accessions, "
            "max_sequence_bp=MAX_CUSTOM_GENBANK_SEQUENCE_BP)",
            source,
        )


class AccessionTokenParsingTests(unittest.TestCase):
    def test_observation_ids_are_reported_as_invalid_not_sent_to_ncbi(self):
        accessions, invalid = routes._parse_genbank_accession_tokens(
            "OR807397, iNat280384724 AAAA01000001"
        )
        self.assertEqual(accessions, ["OR807397", "AAAA01000001"])
        self.assertEqual(invalid, ["iNat280384724"])

    def test_the_loose_fetch_filter_still_covers_every_accepted_shape(self):
        """blast_service's safety net must not drop what the entry points allow.

        It is a second, looser pattern; if it is narrower than
        GENBANK_ACCESSION_RE, accessions the user was just told were valid are
        silently discarded before the efetch.
        """
        import re

        from app.services import blast_service

        source = open(blast_service.__file__).read()
        pattern = re.search(r"valid_pattern = re\.compile\(r'([^']+)'", source)
        self.assertIsNotNone(pattern)
        loose = re.compile(pattern.group(1), re.IGNORECASE)
        for accession in (
            "U49845", "OR807397", "KY12345678", "NC_012345", "NM_001234567",
            "AAAA01000001", "AAAA010000001", "AAAA0100000001",
            "AAAAAA010000001", "AAAAAA0100000001", "AAAAAA01000000001",
        ):
            self.assertTrue(loose.match(accession), accession)


if __name__ == "__main__":
    unittest.main()
