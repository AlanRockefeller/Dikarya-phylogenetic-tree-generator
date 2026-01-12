"""Security tests for input validation."""

import unittest
import sys
import importlib.util

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


class TestBlastQueryValidation(unittest.TestCase):
    """Test BLAST query validation."""
    
    def test_valid_sequence(self):
        """Valid nucleotide sequence should pass."""
        is_valid, _ = security_utils.validate_blast_query("ATGCGATCGATCG")
        self.assertTrue(is_valid)
    
    def test_shell_injection_rejected(self):
        """Shell metacharacters should be rejected."""
        # Note: | is now allowed (common in FASTA headers), preventing shell injection 
        # relies on how the query is used (passed as file/arg, not raw shell string).
        patterns = ['; rm -rf /', '`whoami`', '$(id)']
        for pattern in patterns:
            is_valid, _ = security_utils.validate_blast_query(pattern)
            self.assertFalse(is_valid, f"Should reject: {pattern}")

    def test_fasta_header_allowed(self):
        """Standard FASTA headers with pipes should be allowed."""
        valid_query = ">gi|12345|ref|NC_000000| Some Organism\nATCG..."
        is_valid, _ = security_utils.validate_blast_query(valid_query)
        self.assertTrue(is_valid, "Should allow FASTA headers with pipes")


if __name__ == '__main__':
    unittest.main()
