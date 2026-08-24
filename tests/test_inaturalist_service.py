"""
Unit tests for inaturalist_service.py

Tests URL validation and DNA cleanup logic without requiring network access.
"""

import sys
import unittest
import importlib.util

import os
# Load inaturalist_service directly to avoid Flask dependency from app/__init__.py
# Use relative path for portability
SERVICE_PATH = os.path.join(os.path.dirname(__file__), '../app/services/inaturalist_service.py')
spec = importlib.util.spec_from_file_location(
    "inaturalist_service", 
    SERVICE_PATH
)
inaturalist_service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inaturalist_service)

validate_inaturalist_url = inaturalist_service.validate_inaturalist_url
clean_dna_sequence = inaturalist_service.clean_dna_sequence


class TestInaturalistUrlValidation(unittest.TestCase):
    """Test URL validation for iNaturalist service."""

    def test_valid_url_single_observation(self):
        """Valid single observation URL should return observation type."""
        url = "https://www.inaturalist.org/observations/12345"
        result = validate_inaturalist_url(url)
        self.assertIsNotNone(result)
        self.assertEqual(result['type'], 'single_observation')
        self.assertEqual(result['observation_id'], '12345')

    def test_valid_url_single_observation_with_query(self):
        """Single observation URL with query params should work."""
        url = "https://www.inaturalist.org/observations/67890?locale=en"
        result = validate_inaturalist_url(url)
        self.assertIsNotNone(result)
        self.assertEqual(result['type'], 'single_observation')
        self.assertEqual(result['observation_id'], '67890')

    def test_valid_url_observations_search(self):
        """Observations search URL should return search type."""
        url = "https://www.inaturalist.org/observations?taxon_id=47170"
        result = validate_inaturalist_url(url)
        self.assertIsNotNone(result)
        self.assertEqual(result['type'], 'observations_search')

    def test_valid_url_observations_with_field_filter(self):
        """Observations URL with DNA Barcode ITS field filter."""
        # Note: trailing '=' implies empty value, which must be preserved
        url = "https://www.inaturalist.org/observations?field:DNA%20Barcode%20ITS="
        result = validate_inaturalist_url(url)
        self.assertIsNotNone(result)
        self.assertEqual(result['type'], 'observations_search')
        # Verify the empty field param was preserved
        self.assertIn('field:DNA Barcode ITS', result['query_params'])
        self.assertEqual(result['query_params']['field:DNA Barcode ITS'], [''])

    def test_invalid_url_wrong_domain(self):
        """Non-iNaturalist domains should be rejected."""
        url = "https://example.com/observations/12345"
        result = validate_inaturalist_url(url)
        self.assertIsNone(result)

    def test_invalid_url_no_observations(self):
        """iNaturalist URLs without /observations should be rejected."""
        url = "https://www.inaturalist.org/taxa/12345"
        result = validate_inaturalist_url(url)
        self.assertIsNone(result)

    def test_invalid_url_empty(self):
        """Empty URL should return None."""
        result = validate_inaturalist_url("")
        self.assertIsNone(result)

    def test_invalid_url_none(self):
        """None URL should return None."""
        result = validate_inaturalist_url(None)
        self.assertIsNone(result)

    def test_valid_url_subdomain(self):
        """Subdomains of inaturalist.org should be accepted."""
        url = "https://www.inaturalist.org/observations/12345"
        result = validate_inaturalist_url(url)
        self.assertIsNotNone(result)
        self.assertEqual(result['observation_id'], '12345')

    def test_invalid_url_partial_domain(self):
        """Partial domain match (e.g., notinaturalist.org) should be rejected."""
        url = "https://notinaturalist.org/observations/12345"
        result = validate_inaturalist_url(url)
        self.assertIsNone(result)


class TestDnaSequenceCleanup(unittest.TestCase):
    """Test DNA sequence cleanup functionality."""

    def test_clean_sequence_already_clean(self):
        """Already clean sequence of sufficient length should be returned unchanged (uppercase)."""
        # Create a 150bp sequence (above 100bp minimum)
        seq = "ATGCGATCGATCG" * 12  # 156bp
        result = clean_dna_sequence(seq)
        self.assertEqual(result, seq.upper())

    def test_clean_sequence_lowercase(self):
        """Lowercase sequence should be uppercased."""
        seq = "atgcgatcgatcg" * 12  # 156bp
        result = clean_dna_sequence(seq)
        self.assertEqual(result, seq.upper())

    def test_clean_sequence_with_fasta_header(self):
        """FASTA header should be removed, keeping sequence from same line."""
        # Header on one line, sequence on next
        dna = "ATGCGATCGATCG" * 12
        # 'description' ends in 'n' (valid DNA), so use 'info' (o is invalid) to ensure clean break
        seq = f">seq1 info\n{dna}"
        result = clean_dna_sequence(seq)
        self.assertEqual(result, dna.upper())

    def test_clean_sequence_inline_header_with_separator(self):
        """A clearly separated same-line sequence should be recovered."""
        dna = "AAAGTCGTAACAAGGTTTCCGTAGGTGAACCTGCGGAAGGATCATTATTGAATGAACTTGGCATGGTTGT" * 2  # 140bp
        seq = f">description_info {dna}"
        result = clean_dna_sequence(seq)
        self.assertEqual(result, dna.upper())

    def test_clean_sequence_glued_header_rejected(self):
        """A sequence glued directly to a FASTA header is ambiguous and should be rejected."""
        dna = "AAAGTCGTAACAAGGTTTCCGTAGGTGAACCTGCGGAAGGATCATTATTGAATGAACTTGGCATGGTTGT" * 2
        seq = f">description_info{dna}"
        result = clean_dna_sequence(seq)
        self.assertEqual(result, "")

    def test_clean_sequence_species_name_prefix(self):
        """Species name prefix should be stripped, longest contiguous run extracted."""
        dna = "AAAGTCGTAACAAGGTTTCCGTAGGTGAACCTGCGGAAGGATCATTATTGAATGAACTTGGCATGGTTGT" * 2  # 140bp
        # 'spumo' contains valid chars but 'u' breaks the run? actually u is not valid.
        # 'spumosa': s=ok, p=not, u=not, m=ok, o=not, s=ok, a=ok
        # So it should find the run starting after the last invalid char.
        seq = f"Pholiota zzmozz{dna}"  # 'zz' ensures break, ends with 'z' (invalid)
        result = clean_dna_sequence(seq)
        # Should exact match dna
        self.assertEqual(result, dna.upper())

    def test_clean_sequence_with_trailing_garbage(self):
        """Trailing garbage after sequence should be stripped."""
        dna = "AAAGTCGTAACAAGGTTTCCGTAGGTGAACCTGCGGAAGGATCATTATTGAATGAACTTGGCATGGTTGT" * 2  # 140bp
        # 'notes' -> n=ok, o=not. 'zotes' -> z=not
        seq = f"{dna} zome trailing notes here"
        result = clean_dna_sequence(seq)
        self.assertEqual(result, dna.upper())

    def test_clean_sequence_with_whitespace(self):
        """Whitespace and newlines should be removed."""
        dna = "ATGCGATCGATCG" * 12  # 156bp
        # Add whitespace throughout
        seq = dna[:50] + " " + dna[50:100] + "\n" + dna[100:]
        result = clean_dna_sequence(seq)
        self.assertEqual(result, dna.upper())

    def test_clean_sequence_with_ambiguity_codes(self):
        """IUPAC ambiguity codes should be preserved."""
        # Include ambiguity codes in a 150bp sequence
        seq = "ATGCYRWSKMN-" * 13  # 156bp
        result = clean_dna_sequence(seq)
        self.assertEqual(result, seq.upper())

    def test_clean_sequence_empty(self):
        """Empty input should return empty string."""
        result = clean_dna_sequence("")
        self.assertEqual(result, "")

    def test_clean_sequence_none(self):
        """None input should return empty string."""
        result = clean_dna_sequence(None)
        self.assertEqual(result, "")

    def test_clean_sequence_too_short(self):
        """Sequences below minimum length should return empty."""
        seq = "ATGCGATCGATCG"  # 13bp, below 100bp default
        result = clean_dna_sequence(seq)
        self.assertEqual(result, "")

    def test_clean_sequence_custom_min_length(self):
        """Custom minimum length should be respected."""
        seq = "ATGCGATCGATCG"  # 13bp
        result = clean_dna_sequence(seq, min_length=10)
        self.assertEqual(result, "ATGCGATCGATCG")


class TestSecurityInputSanitization(unittest.TestCase):
    """Test security-related input sanitization."""

    def test_search_url_without_a_filter_is_rejected_script_tags_or_not(self):
        """A bare search URL is rejected because it carries no filter param.

        The old version asserted only `if result:`, so it proved nothing
        whenever validation returned None -- which is in fact what happens here.
        Its docstring also blamed the domain, but inaturalist.org is the accepted
        domain; the rejection comes from the missing filter.
        """
        url = "https://inaturalist.org/observations?<script>alert(1)</script>"
        self.assertIsNone(validate_inaturalist_url(url))

    def test_script_tags_in_the_query_are_carried_through_as_inert_data(self):
        """Markup in a query value must neither be executed nor change the verdict.

        With a filter present the URL is accepted, and the point of the security
        contract is that the tag survives as an ordinary string in query_params
        rather than being interpreted, stripped, or causing a rejection that
        would look like validation working for the wrong reason.
        """
        payload = "<script>alert(1)</script>"
        url = f"https://inaturalist.org/observations?taxon_id=123&q={payload}"

        result = validate_inaturalist_url(url)

        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "observations_search")
        self.assertEqual(result["query_params"]["q"], [payload])
        self.assertEqual(result["query_params"]["taxon_id"], ["123"])

    def test_url_path_traversal(self):
        """Path traversal attempts should not affect validation."""
        # Add required filter to satisfy new security check
        url = "https://inaturalist.org/observations/../../../etc/passwd?taxon_id=123"
        result = validate_inaturalist_url(url)
        # Should be parsed as observations_search but we don't use path for file access
        self.assertIsNotNone(result)
        self.assertEqual(result['type'], 'observations_search')


class TestQueryLogic(unittest.TestCase):
    """Test query construction and analysis logic."""
    
    def test_build_base_params_preserves_blank_fields(self):
        """build_base_params should keep blank field values as empty strings."""
        build_base_params = inaturalist_service.build_base_params
        
        # input from parse_qs(keep_blank_values=True)
        query_params = {
            'taxon_id': ['123'],
            'field:DNA Barcode ITS': [''],
            'field:Other': ['some value']
        }
        
        params = build_base_params(query_params)
        
        self.assertEqual(params['taxon_id'], '123')
        self.assertEqual(params['field:DNA Barcode ITS'], '') # Must be empty string
        self.assertEqual(params['field:Other'], 'some value')
        
    def test_analyze_provisional_species(self):
        """analyze_provisional_species should produce correct histogram."""
        analyze_provisional_species = inaturalist_service.analyze_provisional_species
        
        # Mock observations
        obs = [
            {'ofvs': [{'name': 'Provisional Species Name', 'value': 'Species A'}]},
            {'ofvs': [{'name': 'Provisional Species Name', 'value': 'Species B'}]},
            {'ofvs': [{'name': 'Provisional Species Name', 'value': 'Species A'}]},
            {'ofvs': []}, # No PSN
            {'ofvs': [{'name': 'Other', 'value': 'X'}]} # Wrong field
        ]
        
        histogram = analyze_provisional_species(obs)
        
        # Expected: Species A: 2, Species B: 1
        self.assertEqual(len(histogram), 2)
        self.assertEqual(histogram[0]['name'], 'Species A')
        self.assertEqual(histogram[0]['count'], 2)
        self.assertEqual(histogram[1]['name'], 'Species B')
        self.assertEqual(histogram[1]['count'], 1)

    def test_extract_sequences_name_priority(self):
        """Test name extraction priority: Override > Provisional > Taxon."""
        extract_sequences = inaturalist_service.extract_sequences_from_observations
        
        obs_data = [
            {
                # Case 1: Override exists -> Use Override
                'observation': {
                    'id': 1,
                    'ofvs': [
                        {'name': 'Species Name Override', 'value': 'Override Name'},
                        {'name': 'Provisional Species Name', 'value': 'Prov Name'},
                    ],
                    'taxon': {'name': 'Taxon Name'}
                },
                'cleaned_dna': 'ATGC'
            },
            {
                # Case 2: Only Provisional -> Use Provisional
                'observation': {
                    'id': 2,
                    'ofvs': [
                        {'name': 'Provisional Species Name', 'value': 'Prov Name'},
                    ],
                    'taxon': {'name': 'Taxon Name'}
                },
                'cleaned_dna': 'ATGC'
            },
            {
                # Case 3: Neither -> Use Taxon
                'observation': {
                    'id': 3,
                    'ofvs': [],
                    'taxon': {'name': 'Taxon Name'}
                },
                'cleaned_dna': 'ATGC'
            }
        ]
        
        sequences = extract_sequences(obs_data)
        
        self.assertEqual(sequences[0]['organism'], 'Override Name')
        self.assertEqual(sequences[1]['organism'], 'Prov Name')
        self.assertEqual(sequences[2]['organism'], 'Taxon Name')


    def test_fetch_observations_with_field_filter_strips_params(self):
        """fetch_observations_with_field_filter should strip existing field: params."""
        # Mock _make_api_request to capture URL
        original_make_request = inaturalist_service._make_api_request
        captured_urls = []
        def mock_make_request(url, max_retries=3):
            captured_urls.append(url)
            return {'results': [], 'total_results': 0}
            
        inaturalist_service._make_api_request = mock_make_request
        
        try:
            base_params = {
                'taxon_id': '123',
                'field:SomeOldField': 'some_val',
                'per_page': 200,
                'order_by': 'id',
                'order': 'asc'
            }
            
            inaturalist_service.fetch_observations_with_field_filter(base_params, "NewField")
            
            # Verify URL
            self.assertTrue(len(captured_urls) > 0)
            url = captured_urls[0]
            self.assertIn('field%3ANewField=', url) # encoded field:NewField=
            self.assertNotIn('field%3ASomeOldField', url)
            
        finally:
            inaturalist_service._make_api_request = original_make_request


if __name__ == '__main__':
    unittest.main()
