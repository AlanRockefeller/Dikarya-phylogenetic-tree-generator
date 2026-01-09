"""
Unit tests for mycomap_service.py

Tests URL validation logic without requiring network access.
"""

import sys
import unittest
import importlib.util

# Load mycomap_service directly to avoid Flask dependency from app/__init__.py
spec = importlib.util.spec_from_file_location(
    "mycomap_service", 
    "/var/www/dikarya/app/services/mycomap_service.py"
)
mycomap_service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mycomap_service)
validate_mycomap_url = mycomap_service.validate_mycomap_url


class TestMycomapUrlValidation(unittest.TestCase):
    """Test URL validation for Mycomap service."""

    def test_valid_url_standard(self):
        """Valid Mycomap URL with r<digits> pattern should return blast_id."""
        url = "https://mycomap.com/something/r12345/results"
        result = validate_mycomap_url(url)
        self.assertEqual(result, "12345")

    def test_valid_url_short(self):
        """Valid Mycomap URL with minimal path should work."""
        url = "https://mycomap.com/r999"
        result = validate_mycomap_url(url)
        self.assertEqual(result, "999")

    def test_valid_url_http(self):
        """HTTP (non-HTTPS) Mycomap URLs should also work."""
        url = "http://mycomap.com/blast/r7654321"
        result = validate_mycomap_url(url)
        self.assertEqual(result, "7654321")

    def test_valid_url_with_query_params(self):
        """URL with query parameters should work."""
        url = "https://mycomap.com/index.php?app=genbank&r54321"
        result = validate_mycomap_url(url)
        self.assertEqual(result, "54321")

    def test_invalid_url_wrong_domain(self):
        """Non-Mycomap domains should be rejected."""
        url = "https://example.com/r12345"
        result = validate_mycomap_url(url)
        self.assertIsNone(result)

    def test_invalid_url_no_pattern(self):
        """Mycomap URLs without r<digits> pattern should be rejected."""
        url = "https://mycomap.com/just/a/path"
        result = validate_mycomap_url(url)
        self.assertIsNone(result)

    def test_invalid_url_empty(self):
        """Empty URL should return None."""
        result = validate_mycomap_url("")
        self.assertIsNone(result)

    def test_invalid_url_none(self):
        """None URL should return None."""
        result = validate_mycomap_url(None)
        self.assertIsNone(result)

    def test_valid_url_subdomain(self):
        """Subdomains of mycomap.com should be accepted."""
        url = "https://www.mycomap.com/r88888"
        result = validate_mycomap_url(url)
        self.assertEqual(result, "88888")

    def test_invalid_url_partial_domain(self):
        """Partial domain match (e.g., notmycomap.com) should be rejected."""
        url = "https://notmycomap.com/r12345"
        result = validate_mycomap_url(url)
        # This should actually match because 'mycomap.com' is in the string
        # Let's verify current behavior - this might be a loose match
        # For security, it would be better to be stricter, but keeping current behavior
        self.assertIsNone(result)  # If this fails, we need stricter domain validation


if __name__ == '__main__':
    unittest.main()
