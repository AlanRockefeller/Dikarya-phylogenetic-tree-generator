"""
Unit tests for mycomap_service.py

Tests URL validation logic without requiring network access.
"""

import sys
import unittest
import importlib.util
from unittest.mock import patch

# Load mycomap_service directly to avoid Flask dependency from app/__init__.py
spec = importlib.util.spec_from_file_location(
    "mycomap_service", 
    "/var/www/dikarya/app/services/mycomap_service.py"
)
mycomap_service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mycomap_service)
validate_mycomap_url = mycomap_service.validate_mycomap_url
build_blast_metric_keys = mycomap_service.build_blast_metric_keys
improve_mycomap_sequence_name = mycomap_service.improve_mycomap_sequence_name
parse_blast_metrics_table = mycomap_service._parse_blast_metrics_table
prefer_local_mycomap_taxa = mycomap_service.prefer_local_mycomap_taxa


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


class TestMycomapBlastMetrics(unittest.TestCase):
    """Test local/MycoBLAST metric key parsing without network access."""

    def test_metric_keys_include_local_observation_variants(self):
        """Local MycoMap labels should match FASTA headers and table labels."""
        inat_keys = build_blast_metric_keys(
            "iNaturalist # 106191931 Mycena epipterygia"
        )
        mushroom_observer_keys = build_blast_metric_keys(
            "MushroomObserver.org/479981 Mycena epipterygia"
        )

        self.assertIn("iNat106191931", inat_keys)
        self.assertIn("106191931", inat_keys)
        self.assertIn("MO479981", mushroom_observer_keys)
        self.assertIn("479981", mushroom_observer_keys)

    def test_parse_local_metrics_table_from_name_column(self):
        """Local result tables can identify hits by display name, not accession."""
        rows = [
            ["Source", "Name", "Query Cover", "Subject Cover", "Identity"],
            [
                "Local",
                "iNaturalist # 106191931 Mycena epipterygia",
                "80%",
                "79%",
                "95.2%",
            ],
            [
                "Local",
                "Mushroom Observer # 479981 Mycena epipterygia",
                "81%",
                "80%",
                "94.4%",
            ],
        ]

        metrics = parse_blast_metrics_table(rows)

        self.assertEqual(metrics["iNat106191931"]["identity"], 95.2)
        self.assertEqual(metrics["iNat106191931"]["query_cover"], 80.0)
        self.assertEqual(metrics["MO479981"]["identity"], 94.4)
        self.assertEqual(metrics["MO479981"]["subject_cover"], 80.0)

    def test_parse_local_metrics_aliases_source_observation_to_current_name(self):
        """Local source IDs should resolve to the current MycoMap species name."""
        rows = [
            [
                "Hit Number", "Description", "Identity", "Query/Subject Cover",
                "Gap Openings", "Accession", "Source",
            ],
            [
                "80",
                (
                    "iNaturalist #339209504 Species Name: Mycena sp. 'CA28' "
                    "Location: Fort Bragg California US"
                ),
                "96.73 (98.04)",
                "100%/64%",
                "7 (1)",
                "650687",
                "339209504 - Hypomyces sp. 'cervinigenus-IN01'",
            ],
        ]

        metrics = parse_blast_metrics_table(rows)

        self.assertEqual(metrics["339209504"]["species_name"], "Mycena sp. 'CA28'")
        self.assertEqual(
            metrics["339209504"]["mycomap_location"],
            "Fort Bragg California US",
        )

    def test_parse_ncbi_metrics_prefers_accession_column(self):
        """GenBank descriptions should not create local keys when accession exists."""
        rows = [
            ["Description", "Query Cover", "Per. Ident", "Accession"],
            [
                "Akanthomyces sp. iNaturalist # 106614104 California US",
                "88%",
                "99.8%",
                "OP035386",
            ],
        ]

        metrics = parse_blast_metrics_table(rows)

        self.assertIn("OP035386", metrics)
        self.assertNotIn("iNat106614104", metrics)

    def test_parse_ncbi_metrics_extracts_display_name(self):
        """NCBI result rows should expose species/location labels from MycoMap."""
        rows = [
            ["Hit Number", "Description", "Identity", "Query/Subject Cover", "Accession"],
            [
                "5",
                (
                    "Ascobolus equinus strain CBS 107.33 small subunit ribosomal RNA "
                    "Species Name: Ascobolus equinus Location: England GB"
                ),
                "96.8 (97.86)",
                "99%/98%",
                "MH855376",
            ],
        ]

        metrics = parse_blast_metrics_table(rows)

        self.assertEqual(metrics["MH855376"]["display_name"], "MH855376 Ascobolus equinus England GB")
        self.assertEqual(metrics["MH855376"]["species_name"], "Ascobolus equinus")
        self.assertEqual(metrics["MH855376"]["mycomap_location"], "England GB")
        self.assertEqual(metrics["MH855376"]["query_cover"], 99.0)
        self.assertEqual(metrics["MH855376"]["subject_cover"], 98.0)

    def test_improve_sequence_name_repairs_sparse_ncbi_header(self):
        """Sparse NCBI FASTA labels should use the MycoMap table species name."""
        metric = {
            "display_name": "MH855376 Ascobolus equinus England GB",
            "species_name": "Ascobolus equinus",
            "mycomap_location": "England GB",
        }

        improved = improve_mycomap_sequence_name("MH855376 England GB", metric, "ncbi")

        self.assertEqual(improved, "MH855376 Ascobolus equinus England GB")

    def test_improve_sequence_name_preserves_existing_species_label(self):
        """Existing useful NCBI labels should not be shortened by table metadata."""
        metric = {
            "display_name": "OP339522 Lasiobolus sp. 'UT01' Chile",
            "species_name": "Lasiobolus sp. 'UT01'",
            "mycomap_location": "Chile",
        }
        current = "OP339522 Lasiobolus sp. 'UT01' FLAS:F-70452-MES-3926 Chile"

        improved = improve_mycomap_sequence_name(current, metric, "ncbi")

        self.assertEqual(improved, current)

    def test_improve_sequence_name_prefers_current_mycomap_taxon_for_ncbi_header(self):
        """Current MycoMap taxonomy should replace a stale DB39 FASTA taxon."""
        metric = {
            "display_name": "OR987324 Panaeolus sp. 'NY02' New York US",
            "species_name": "Panaeolus sp. 'NY02'",
            "mycomap_location": "New York US",
        }

        improved = improve_mycomap_sequence_name(
            "OR987324 Fungi New York US",
            metric,
            "ncbi",
            accession="OR987324",
            location="New York US",
        )

        self.assertEqual(
            improved,
            "OR987324 Panaeolus sp. 'NY02' New York US",
        )

    def test_improve_sequence_name_keeps_ncbi_voucher_with_updated_taxon(self):
        """Updating a taxon should not discard structured NCBI metadata."""
        metric = {
            "display_name": "PP808684 Panaeolus sp. 'NY02' New York US",
            "species_name": "Panaeolus sp. 'NY02'",
            "mycomap_location": "New York US",
        }

        improved = improve_mycomap_sequence_name(
            "PP808684 Panaeolus atrobalteatus jlh7468-14 New York US",
            metric,
            "ncbi",
            accession="PP808684",
            voucher="jlh7468-14",
            location="New York US",
        )

        self.assertEqual(
            improved,
            "PP808684 Panaeolus sp. 'NY02' jlh7468-14 New York US",
        )

    def test_exact_local_sequence_taxon_has_priority_over_ncbi_taxon(self):
        """A current local taxon should rename an exact NCBI sequence match."""
        sequences = [
            {
                "name": "PP808684 Panaeolus atrobalteatus jlh7468-14 New York US",
                "sequence": "ACGTTGCA",
                "hit_source": "ncbi",
                "accession": "PP808684",
                "taxon": "Panaeolus atrobalteatus",
                "voucher": "jlh7468-14",
                "location": "New York US",
            },
            {
                "name": "iNat190438995 Panaeolus sp. 'NY02' Brookhaven New York US",
                "sequence": "ACGTTGCA",
                "hit_source": "local",
                "taxon": "Panaeolus sp. 'NY02'",
            },
        ]

        updated = prefer_local_mycomap_taxa(sequences)

        self.assertEqual(
            updated[0]["name"],
            "PP808684 Panaeolus sp. 'NY02' jlh7468-14 New York US",
        )
        self.assertEqual(updated[0]["taxon"], "Panaeolus sp. 'NY02'")

    def test_conflicting_exact_local_taxa_do_not_rename_ncbi_taxon(self):
        """Conflicting local labels must not arbitrarily rename an NCBI hit."""
        sequences = [
            {
                "name": "AB123456 Panaeolus olivaceus",
                "sequence": "ACGTTGCA",
                "hit_source": "ncbi",
                "accession": "AB123456",
                "taxon": "Panaeolus olivaceus",
            },
            {
                "name": "iNat1 Panaeolus sp. 'NY02'",
                "sequence": "ACGTTGCA",
                "hit_source": "local",
                "taxon": "Panaeolus sp. 'NY02'",
            },
            {
                "name": "iNat2 Panaeolus fraxinophilus",
                "sequence": "ACGTTGCA",
                "hit_source": "local",
                "taxon": "Panaeolus fraxinophilus",
            },
        ]

        updated = prefer_local_mycomap_taxa(sequences)

        self.assertEqual(updated[0]["name"], "AB123456 Panaeolus olivaceus")
        self.assertEqual(updated[0]["taxon"], "Panaeolus olivaceus")

    def test_improve_sequence_name_uses_current_local_mycomap_name(self):
        """Local labels should replace stale source taxonomy with MycoMap taxonomy."""
        metric = {
            "display_name": "397866 Lasiobolus sp. 'UT01' Nederland Colorado US",
            "species_name": "Lasiobolus sp. 'UT01'",
            "mycomap_location": "Nederland Colorado US",
        }
        current = "iNat135398232 Lasiobolus sp. 'UT01' Colorado US"

        improved = improve_mycomap_sequence_name(current, metric, "local")

        self.assertEqual(
            improved,
            "iNat135398232 Lasiobolus sp. 'UT01' Nederland Colorado US",
        )


class TestMycomapBlastCreation(unittest.TestCase):
    def test_find_blast_by_exact_title(self):
        class FakePageResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"""
                    <a href='https://mycomap.com/genetics/blast-search/
                        unrelated-r590132/'>Other title</a>
                    <a href='https://mycomap.com/genetics/blast-search/inat378687760-dna-barcode-its-r590133/'>
                        iNat378687760 DNA Barcode ITS - 5CXMJMZ3016
                    </a>
                """

        with patch.object(
            mycomap_service.urllib.request,
            "urlopen",
            return_value=FakePageResponse(),
        ):
            found = mycomap_service.find_mycomap_blast_by_title(
                "iNat378687760 DNA Barcode ITS"
            )

        self.assertEqual(found["blast_id"], "590133")
        self.assertTrue(found["url"].endswith("r590133/"))

    def test_parse_created_blast_from_nested_url(self):
        created = mycomap_service._parse_created_mycomap_blast(
            {
                "status": "queued",
                "result": {
                    "url": "https://mycomap.com/genetics/blast-search/inat123-r590123/"
                },
            },
            "",
        )

        self.assertEqual(created["blast_id"], "590123")
        self.assertEqual(
            created["url"],
            "https://mycomap.com/genetics/blast-search/inat123-r590123/",
        )

    def test_create_blast_posts_required_sequence_contract(self):
        class FakeResponse:
            status = 202
            headers = {"Location": "https://mycomap.com/genetics/blast-search/r590124/"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

            def geturl(self):
                return "https://mycomap.com/api/mycomap/blast"

            def read(self):
                return b'{"status":"queued"}'

        with (
            patch.object(
                mycomap_service,
                "_mycomap_api_key_info",
                return_value=("a" * 32, {"source": "test", "length": 32, "sha256": "safe"}),
            ),
            patch.object(
                mycomap_service.urllib.request,
                "urlopen",
                return_value=FakeResponse(),
            ) as urlopen,
        ):
            created = mycomap_service.create_mycomap_blast(
                "ACGT" * 40,
                title="iNat123 DNA Barcode ITS",
                local_limit=50,
                ncbi_limit=100,
            )

        request = urlopen.call_args.args[0]
        body = request.data.decode("utf-8")
        self.assertEqual(created["blast_id"], "590124")
        self.assertIn("type=sequence", body)
        self.assertIn("input=ACGT", body)
        self.assertIn("userID=1", body)
        self.assertIn("blast_search_by=sequence", body)
        self.assertIn("blast_search_sequence=ACGT", body)
        self.assertNotIn("a" * 32, body)

    def test_create_blast_returns_pending_until_result_page_is_published(self):
        class FakeResponse:
            status = 202
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

            def geturl(self):
                return "https://mycomap.com/api/mycomap/blast"

            def read(self):
                return b'{"status":"queued"}'

        with (
            patch.object(
                mycomap_service,
                "_mycomap_api_key_info",
                return_value=("a" * 32, {"source": "test", "length": 32, "sha256": "safe"}),
            ),
            patch.object(
                mycomap_service.urllib.request,
                "urlopen",
                return_value=FakeResponse(),
            ),
            patch.object(
                mycomap_service,
                "find_mycomap_blast_by_title",
                return_value=None,
            ),
        ):
            created = mycomap_service.create_mycomap_blast(
                "ACGT" * 40,
                title="iNat365269897 DNA Barcode ITS",
            )

        self.assertTrue(created["record_pending"])
        self.assertEqual(created["title"], "iNat365269897 DNA Barcode ITS")
        self.assertNotIn("blast_id", created)

if __name__ == '__main__':
    unittest.main()
