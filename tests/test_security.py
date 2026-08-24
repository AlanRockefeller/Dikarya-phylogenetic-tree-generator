"""Security tests for input validation."""

import unittest
from unittest.mock import Mock, patch

# The application's own module, not a second copy of it. Loading the file by
# path built an independent module object named "security_utils": these tests
# then validated that copy, and would keep passing after the real
# app.services.security_utils diverged from it.
from app.services import security_utils


class TestJobIdValidation(unittest.TestCase):
    """Test job_id validation for directory traversal prevention."""
    
    def test_valid_uuid4(self):
        """Valid UUID4 should pass."""
        valid_id = "550e8400-e29b-41d4-a716-446655440000"
        self.assertTrue(security_utils.validate_job_id(valid_id))
    
    def test_directory_traversal_rejected(self):
        """Directory traversal attempts should be rejected."""
        self.assertFalse(security_utils.validate_job_id("../etc/passwd"))
        self.assertFalse(security_utils.validate_job_id("..%2F..%2Fetc"))
        self.assertFalse(security_utils.validate_job_id("../../var/jobs/legit"))
    
    def test_empty_rejected(self):
        """Empty and None values should be rejected."""
        self.assertFalse(security_utils.validate_job_id(""))
        self.assertFalse(security_utils.validate_job_id(None))
    
    def test_random_string_rejected(self):
        """Random strings should be rejected."""
        self.assertFalse(security_utils.validate_job_id("my-job-123"))
        self.assertFalse(security_utils.validate_job_id("test"))


class TestBlastRequestSafety(unittest.TestCase):
    """Test the current BLAST request boundary rather than a removed helper."""

    def test_queries_are_sent_as_http_form_data(self):
        """Sequences, FASTA headers, and metacharacters remain inert HTTP data."""
        from app.services import blast_service

        response = Mock()
        response.text = "RID = TEST_RID\nRTOE = 1"
        queries = [
            "ATGCGATCGATCG",
            ">gi|12345|ref|NC_000000| Some Organism\nATCG...",
            "; rm -rf / `whoami` $(id)",
        ]

        for query in queries:
            with self.subTest(query=query), patch.object(
                blast_service, "_ncbi_request", return_value=response
            ) as request:
                rid, rtoe = blast_service._submit_blast_request(query)

                self.assertEqual((rid, rtoe), ("TEST_RID", 1))
                request.assert_called_once()
                args, kwargs = request.call_args
                self.assertEqual(args, ("POST", blast_service.NCBI_BLAST_URL))
                self.assertEqual(kwargs["data"]["QUERY"], query)

    def test_query_length_limit_applies_before_http_request(self):
        """An oversized BLAST query should be rejected without a network call."""
        from app.services import blast_service

        class ShortQueryConfig:
            BLAST_MAX_QUERY_LENGTH = 5

        with patch.object(blast_service, "_ncbi_request") as request:
            with self.assertRaisesRegex(ValueError, "Query too long"):
                blast_service._submit_blast_request("ATGCGC", ShortQueryConfig)
            request.assert_not_called()

    def test_query_at_exactly_the_length_limit_is_accepted(self):
        """A query of exactly BLAST_MAX_QUERY_LENGTH must still be submitted.

        The rejection test above passes just as well against `len(seq) >= max`,
        which would reject every maximum-length query in production. Only the
        accepted side of the boundary distinguishes the two.
        """
        from app.services import blast_service

        class ShortQueryConfig:
            BLAST_MAX_QUERY_LENGTH = 5
            BLAST_EMAIL = "tests@example.com"

        response = Mock()
        response.text = "RID = TEST_RID\nRTOE = 1"
        with patch.object(
            blast_service, "_ncbi_request", return_value=response
        ) as request:
            rid, rtoe = blast_service._submit_blast_request("ATGCG", ShortQueryConfig)

        self.assertEqual((rid, rtoe), ("TEST_RID", 1))
        request.assert_called_once()



class TestFastaSequenceSanitization(unittest.TestCase):
    """Every IUPAC nucleotide code must survive in both cases.

    The lowercase half of the allowed set was written out by hand and omitted
    w, b, d and h, so those codes were silently deleted -- which shortens the
    sequence and shifts every downstream coordinate.
    """

    IUPAC = "ACGTUNRYKMSWBDHV"

    def test_every_iupac_code_survives_in_upper_case(self):
        for base in self.IUPAC:
            with self.subTest(base=base):
                self.assertEqual(
                    security_utils.sanitize_fasta_sequence(base), base
                )

    def test_every_iupac_code_survives_in_lower_case(self):
        for base in self.IUPAC.lower():
            with self.subTest(base=base):
                self.assertEqual(
                    security_utils.sanitize_fasta_sequence(base), base
                )

    def test_mixed_case_sequence_is_returned_unchanged(self):
        seq = "acgtuNRYkmswbdhv-ACGTUnrykmSWBDHV"
        self.assertEqual(security_utils.sanitize_fasta_sequence(seq), seq)

    def test_lowercase_ambiguity_codes_are_not_dropped(self):
        # The exact regression: w/b/d/h used to disappear.
        self.assertEqual(
            security_utils.sanitize_fasta_sequence("acgwbdhtt"), "acgwbdhtt"
        )

    def test_gap_character_is_preserved(self):
        self.assertEqual(security_utils.sanitize_fasta_sequence("AC-GT"), "AC-GT")

    def test_invalid_characters_are_still_removed(self):
        self.assertEqual(
            security_utils.sanitize_fasta_sequence("AC GT\n>x*1ZzE"), "ACGT"
        )

    def test_allowed_alphabet_is_closed_under_case(self):
        allowed = security_utils.ALLOWED_FASTA_SEQUENCE_CHARS
        letters = [c for c in allowed if c.isalpha()]
        self.assertEqual(len(letters), 2 * len(self.IUPAC))
        for c in letters:
            with self.subTest(char=c):
                self.assertIn(c.swapcase(), allowed)


if __name__ == '__main__':
    unittest.main()
