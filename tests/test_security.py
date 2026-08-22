"""Security tests for input validation."""

import unittest
import sys
import importlib.util
from unittest.mock import Mock, patch

# Load security_utils relative to this test file
import os
current_dir = os.path.dirname(os.path.abspath(__file__))
module_path = os.path.join(current_dir, '../app/services/security_utils.py')

spec = importlib.util.spec_from_file_location("security_utils", module_path)
security_utils = importlib.util.module_from_spec(spec)
spec.loader.exec_module(security_utils)


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


if __name__ == '__main__':
    unittest.main()
