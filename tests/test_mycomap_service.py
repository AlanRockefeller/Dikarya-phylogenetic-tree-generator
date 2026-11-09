"""
Unit tests for mycomap_service.py

Tests URL validation logic without requiring network access.
"""

import io
import http.client
import unittest
from unittest.mock import patch

# Imported as the package module the application actually uses. This used to
# load the file from a hardcoded /var/www/dikarya path, which failed in any
# checkout or CI runner outside the production deployment directory, and built a
# second module object that would silently drift from the real one.
from app.services import mycomap_service

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

    def test_slug_with_multiple_r_tokens_uses_final_result_id(self):
        """Earlier r-prefixed slug tokens must not override the result ID."""
        url = (
            "https://mycomap.com/genetics/blast-search/"
            "h11-r025-01591-inat327124600-1v1-ric51-snp3-563287-r452058/"
        )
        result = validate_mycomap_url(url)
        self.assertEqual(result, "452058")

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
        # Domain validation compares host labels, not substrings: "mycomap.com"
        # appearing inside "notmycomap.com" must not make it a MycoMap URL.
        self.assertIsNone(result)


class TestMycomapResultTitle(unittest.TestCase):
    def test_reads_only_the_page_title(self):
        page = (
            b"<html><head><title> AMAJ12 Psilocybe LSU - BLAST Search "
            b"- MycoMap Beta </title></head><body>ignored</body></html>"
        )
        with patch.object(
            mycomap_service,
            "diagnostic_urlopen",
            return_value=io.BytesIO(page),
        ) as urlopen:
            title = mycomap_service.fetch_mycomap_result_title(
                "https://mycomap.com/genetics/blast-search/"
                "amaj12-psilocybe-lsu-r70831/"
            )

        self.assertEqual(
            title,
            "AMAJ12 Psilocybe LSU - BLAST Search - MycoMap Beta",
        )
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 10)

    def test_rejects_an_untrusted_host_without_fetching(self):
        with patch.object(mycomap_service, "diagnostic_urlopen") as urlopen:
            title = mycomap_service.fetch_mycomap_result_title(
                "https://example.com/genetics/blast-search/r70831/"
            )

        self.assertEqual(title, "")
        urlopen.assert_not_called()


class TestMycomapCreationDiscoveryBackoff(unittest.TestCase):
    def test_polling_backs_off_after_one_hour_six_hours_and_one_day(self):
        interval = mycomap_service.get_mycomap_creation_discovery_poll_interval_seconds

        self.assertEqual(interval(0), 60)
        self.assertEqual(interval(3599), 60)
        self.assertEqual(interval(3600), 5 * 60)
        self.assertEqual(interval(6 * 3600), 15 * 60)
        self.assertEqual(interval(24 * 3600), 60 * 60)

    @patch.dict(
        "os.environ",
        {"MYCOMAP_CREATION_DISCOVERY_MAX_SECONDS": str(4 * 24 * 3600)},
    )
    def test_four_day_backoff_fits_in_a_bounded_number_of_rq_retries(self):
        self.assertEqual(
            mycomap_service.get_mycomap_creation_discovery_max_attempts(),
            264,
        )

    def test_legacy_one_hour_job_moves_to_five_minute_checks(self):
        details, expired = mycomap_service.advance_mycomap_creation_discovery({
            "creation_pending": True,
            "creation_discovery_attempt": 60,
        })

        self.assertFalse(expired)
        self.assertEqual(details["creation_discovery_elapsed_seconds"], 65 * 60)
        self.assertEqual(details["creation_discovery_next_interval_seconds"], 5 * 60)


class TestMycomapBlastMetrics(unittest.TestCase):
    """Test local/MycoBLAST metric key parsing without network access."""

    def test_metrics_page_retries_a_truncated_response_before_fallback(self):
        html_page = b"""
            <table>
              <tr><th>Description</th><th>Query Cover</th><th>Per. Ident</th><th>Accession</th></tr>
              <tr><td>Example fungus</td><td>99%</td><td>98.5%</td><td>OR807397</td></tr>
            </table>
        """
        source_url = (
            "https://mycomap.com/genetics/blast-search/"
            "example-search-r12345/"
        )

        with (
            patch(
                "app.services.mycomap_service.diagnostic_urlopen",
                side_effect=[
                    http.client.IncompleteRead(b"<table>"),
                    io.BytesIO(html_page),
                ],
            ) as urlopen,
            patch("app.services.mycomap_service.time.sleep") as sleep,
        ):
            metrics = mycomap_service.fetch_mycomap_blast_metrics(
                "12345", source_url=source_url,
            )

        self.assertEqual(metrics["OR807397"]["identity"], 98.5)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in urlopen.call_args_list],
            [source_url, source_url],
        )
        sleep.assert_called_once_with(
            mycomap_service.BLAST_METRICS_RETRY_SECONDS
        )

    def test_metrics_page_does_not_retry_a_404_before_fallback(self):
        html_page = b"""
            <table>
              <tr><th>Description</th><th>Query Cover</th><th>Per. Ident</th><th>Accession</th></tr>
              <tr><td>Example fungus</td><td>99%</td><td>98.5%</td><td>OR807397</td></tr>
            </table>
        """
        source_url = (
            "https://mycomap.com/genetics/blast-search/"
            "example-search-r12345/"
        )
        missing = mycomap_service.urllib.error.HTTPError(
            source_url, 404, "not found", {}, None,
        )

        with (
            patch(
                "app.services.mycomap_service.diagnostic_urlopen",
                side_effect=[missing, io.BytesIO(html_page)],
            ) as urlopen,
            patch("app.services.mycomap_service.time.sleep") as sleep,
        ):
            metrics = mycomap_service.fetch_mycomap_blast_metrics(
                "12345", source_url=source_url,
            )

        self.assertIn("OR807397", metrics)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_not_called()

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

    def test_parse_local_metrics_falls_back_to_source_column_taxon(self):
        """A record with no "Species Name:" takes the observation taxon instead."""
        rows = [
            [
                "Hit Number", "Description", "Identity", "Query/Subject Cover",
                "Gap Openings", "Accession", "Source",
            ],
            [
                "52",
                "iNaturalist #79684897 Location: Trout Lake Washington US",
                "81.17 (84.17)",
                "98%/99%",
                "52 (31)",
                "768561",
                "79684897 - Rhodocybe sp. 'tugrulii-NB01'",
            ],
            [
                "55",
                "Rhodocybe 'tugrulii-NB01' (4/1/3) Buck#918 WA iNat79684897",
                "81.17 (84.17)",
                "98%/99%",
                "52 (31)",
                "df-47-72-2682",
                "Danny Miller WC Annotations",
            ],
        ]

        metrics = parse_blast_metrics_table(rows)

        self.assertEqual(
            metrics["768561"]["species_name"],
            "Rhodocybe sp. 'tugrulii-NB01'",
        )
        self.assertEqual(
            metrics["768561"]["mycomap_location"],
            "Trout Lake Washington US",
        )
        self.assertEqual(
            improve_mycomap_sequence_name(
                "iNat79684897 Washington US", metrics["768561"], "local"
            ),
            "iNat79684897 Rhodocybe sp. 'tugrulii-NB01' Trout Lake Washington US",
        )
        # An annotation-file source is a file title, not "<id> - <taxon>".
        self.assertEqual(metrics["df-47-72-2682"].get("species_name", ""), "")

    def test_parse_local_metrics_ignores_source_taxon_for_other_observation(self):
        """A Source ID that contradicts the Description must not name the hit."""
        rows = [
            [
                "Hit Number", "Description", "Identity", "Query/Subject Cover",
                "Gap Openings", "Accession", "Source",
            ],
            [
                "52",
                "iNaturalist #79684897 Location: Trout Lake Washington US",
                "81.17 (84.17)",
                "98%/99%",
                "52 (31)",
                "768561",
                "11111111 - Amanita muscaria",
            ],
        ]

        metrics = parse_blast_metrics_table(rows)

        self.assertEqual(metrics["768561"].get("species_name", ""), "")

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

    def test_improve_local_name_uses_accession_when_header_leads_with_genus(self):
        """A curator header ending in an accession should not label the tip by genus."""
        metric = {
            "display_name": "Hygrocybe minutula",
            "species_name": "Hygrocybe minutula",
            "mycomap_location": "",
        }
        current = "Hygrocybe cf minutula QC (type NY) KM248898"

        improved = improve_mycomap_sequence_name(current, metric, "local")

        # The curator's "cf" is kept: it says the identification is uncertain.
        self.assertEqual(improved, "KM248898 Hygrocybe cf minutula")

        # A herbarium barcode is accession-shaped too (NY + 8 digits). With no
        # voucher to say which is which, both are kept in header order, in
        # either position, rather than one being guessed.
        with_barcode = "Hygrocybe cf minutula NY00123456 KM248898"
        self.assertEqual(
            improve_mycomap_sequence_name(with_barcode, metric, "local"),
            "NY00123456 KM248898 Hygrocybe cf minutula",
        )
        barcode_last = "Hygrocybe cf minutula KM248898 NY00123456"
        self.assertEqual(
            improve_mycomap_sequence_name(barcode_last, metric, "local"),
            "KM248898 NY00123456 Hygrocybe cf minutula",
        )
        # A known voucher is never taken for the accession, wherever it sits.
        self.assertEqual(
            improve_mycomap_sequence_name(barcode_last, metric, "local",
                                          voucher="NY00123456"),
            "KM248898 Hygrocybe cf minutula",
        )
        # A versioned and an unversioned spelling are one record.
        self.assertEqual(
            improve_mycomap_sequence_name(
                "Hygrocybe cf minutula KM248898 KM248898.1", metric, "local"),
            "KM248898 Hygrocybe cf minutula",
        )

    def test_improve_local_name_ignores_accessions_in_curator_notes(self):
        """A parenthetical note names another record, never this one."""
        # Real curator headers from local hits: the note holds the type
        # it was matched to, or a second specimen's herbarium number.
        metric = {"display_name": "Russula mendocinensis",
                  "species_name": "Russula mendocinensis", "mycomap_location": ""}
        self.assertEqual(
            improve_mycomap_sequence_name(
                "Russula mendocinensis CA (1/0 ITS1 matchto TYPE CA KX812826) DQ367913",
                metric, "local"),
            "DQ367913 Russula mendocinensis",
        )
        metric = {"display_name": "Russula crenulata",
                  "species_name": "Russula crenulata", "mycomap_location": ""}
        self.assertEqual(
            improve_mycomap_sequence_name(
                "Russula crenulata 24a (crenulata UNITE #2) (+CAx3) HQ604846 F19990 "
                "MO67832 Vict Garry Oak (F18833 unseq'd)", metric, "local"),
            "HQ604846 F19990 Russula crenulata",
        )
        # A holotype named only in a note must not label (and so type-mark) the
        # tip; the UNITE accession outside the note is the identifier instead.
        metric = {"display_name": "Entoloma undulatosporum",
                  "species_name": "Entoloma undulatosporum", "mycomap_location": ""}
        self.assertEqual(
            improve_mycomap_sequence_name(
                "Entoloma undulatosporum (match to dirty holotype PX412020) EU UDB0799647",
                metric, "local"),
            "UDB0799647 Entoloma undulatosporum",
        )
        # Nothing to look up: MycoMap's own record ID, else no identifier at
        # all -- and never the genus twice.
        header = "Entoloma undulatosporum (match to dirty holotype PX412020) EU"
        self.assertEqual(
            improve_mycomap_sequence_name(header, metric, "local",
                                          mycomap_record_id="df-47-71-8636"),
            "MycoMap#df-47-71-8636 Entoloma undulatosporum",
        )
        self.assertEqual(improve_mycomap_sequence_name(header, metric, "local"),
                         "Entoloma undulatosporum")
        metric = {"display_name": "Pluteus tomentosulus",
                  "species_name": "Pluteus tomentosulus", "mycomap_location": "Oregon US"}
        self.assertEqual(
            improve_mycomap_sequence_name("P. Pluteus tomentosulus Oregon US", metric, "local"),
            "Pluteus tomentosulus Oregon US",
        )


class TestCuratorLocalRecords(unittest.TestCase):
    """Curator-labelled local hits match their own results row (real row shapes)."""

    LOCAL_HEADER = ["HitNumber", "Description", "Identity", "Query/Subject Cover",
                    "Gap Openings", "Accession", "Source"]
    NCBI_HEADER = ["HitNumber", "Description", "Identity", "Query/Subject Cover",
                   "Gap Openings", "Accession"]

    def _metrics(self, local_rows, ncbi_rows=()):
        combined = {}
        for rows in ([self.NCBI_HEADER, *ncbi_rows], [self.LOCAL_HEADER, *local_rows]):
            for key, metric in parse_blast_metrics_table(rows).items():
                combined.setdefault(key, metric)
        return combined

    def _match(self, header, metrics):
        from app.api.routes import _mycomap_metric_for_sequence
        return _mycomap_metric_for_sequence(
            {"name": header, "_mycomap_original_name": header, "hit_source": "local"},
            metrics)

    def test_a_note_accession_no_longer_lends_its_row(self):
        header = "Russula mendocinensis CA (1/0 ITS1 matchto TYPE CA KX812826) DQ367913"
        metrics = self._metrics(
            [["7", header, "97.1 (98)", "95%/90%", "3 (0)", "df-47-72-5001",
              "Danny Miller WC Annotations"]],
            [["1", "Russula mendocinensis TYPE Species Name: Russula mendocinensis",
              "88.0", "60%/60%", "9 (1)", "KX812826"]])
        metric = self._match(header, metrics)
        self.assertTrue(metric["own_row"])
        self.assertEqual(metric["identity"], 97.1)
        self.assertEqual(metric["mycomap_record_id"], "df-47-72-5001")
        self.assertEqual(metric["mycomap_dataset"], "Danny Miller WC Annotations")

        # Off the page (only the top 100 rows are listed), the note is still
        # not used to find a row, so the type's numbers are never borrowed.
        self.assertIsNone(self._match(header, self._metrics([], [
            ["1", "Russula mendocinensis TYPE Species Name: Russula mendocinensis",
             "88.0", "60%/60%", "9 (1)", "KX812826"]])))

    def test_name_and_location_come_from_the_named_observation(self):
        header = "Hygrocybe 'minutula PNW24' WA iNat176918366"
        metrics = self._metrics([
            ["52", header, "96.52 (99)", "95%/95%", "2 (0)", "df-47-72-9128",
             "Danny Miller WC Annotations"],
            ["40", "iNaturalist #176918366 Species Name: Hygrocybe sp. 'minutula-PNW24' "
                   "Location: Deming Washington US", "95.0", "90%/90%", "4 (0)",
             "301122", "176918366 - Hygrocybe sp. 'minutula-PNW24'"],
        ])
        metric = self._match(header, metrics)
        self.assertEqual(metric["identity"], 96.52)          # its own row's numbers
        self.assertEqual(metric["species_name"], "Hygrocybe sp. 'minutula-PNW24'")
        self.assertEqual(metric["mycomap_location"], "Deming Washington US")
        self.assertEqual(metric["mycomap_record_id"], "df-47-72-9128")
        # An observation's own Source cell is not a dataset name.
        observation_row = metrics["176918366"]
        self.assertNotIn("mycomap_dataset", observation_row)

    # Alan 11/9/26 - Which name wins when a curator record and the observation
    # it names disagree (both cases confirmed against the real observations).
    def test_another_genus_on_the_observation_keeps_the_curators_name(self):
        header = "Pholiotina 'rugosa PNW01' (error: as Hypholoma dispersum) iNat17859197 WA"
        observation = ["30", "iNaturalist #17859197 Species Name: Hypholoma dispersum "
                             "Location: Treefrog Loop Trail Washington US", "95.0",
                       "90%/90%", "4 (0)", "88001", "17859197 - Hypholoma dispersum"]
        own = ["12", header, "99.1 (99)", "95%/95%", "1 (0)", "df-47-72-9300",
               "Danny Miller WC Annotations"]
        for rows in ([own, observation], [observation]):  # on the page, then off it
            metric = self._match(header, self._metrics(rows))
            self.assertNotIn("Hypholoma", metric.get("species_name") or "")
            self.assertNotIn("mycomap_location", metric)
            self.assertEqual(
                improve_mycomap_sequence_name(header, metric, "local",
                                              mycomap_record_id=metric.get("mycomap_record_id", "")),
                header)

    def test_the_same_genus_takes_the_observations_current_name(self):
        header = "Galerina smithii WA iNat162755648 (was PNW15)"
        metric = self._match(header, self._metrics([
            ["9", header, "99.5 (99)", "95%/95%", "0 (0)", "df-47-72-9400",
             "Danny Miller WC Annotations"],
            ["2", "iNaturalist #162755648 Species Name: Galerina laticeps "
                  "Location: Middle Island New York US", "99.0", "95%/95%", "1 (0)",
             "306130", "162755648 - Galerina laticeps"],
        ]))
        self.assertEqual(metric["species_name"], "Galerina laticeps")
        self.assertEqual(
            improve_mycomap_sequence_name(header, metric, "local"),
            "iNat162755648 Galerina laticeps Middle Island New York US")

    def test_a_curator_headers_first_word_is_never_kept_as_an_id(self):
        header = "Pholiotina 'rugosa PNW01' iNat17859197 WA"
        self.assertEqual(
            improve_mycomap_sequence_name(header, {"species_name": "Hypholoma dispersum"}, "local"),
            "iNat17859197 Hypholoma dispersum")

    def test_a_shared_description_is_not_used_to_match(self):
        header = "Russula sp. CA unlabelled"
        row = ["3", header, "99.0", "90%/90%", "0 (0)", "df-1", "Some Annotations"]
        twin = ["4", header, "91.0", "90%/90%", "6 (0)", "df-2", "Some Annotations"]
        self.assertIsNone(self._match(header, self._metrics([row, twin])))

    def test_species_inference_keeps_a_qualifier(self):
        from app.services.mycomap_service import _infer_species_name
        self.assertEqual(_infer_species_name("Hygrocybe cf minutula QC (type NY) KM248898"),
                         "Hygrocybe cf minutula")
        self.assertEqual(_infer_species_name("Inocybe aff. lacera voucher X ITS"),
                         "Inocybe aff. lacera")
        self.assertEqual(_infer_species_name("Russula crenulata 24a"), "Russula crenulata")

    def test_the_curators_qualifier_survives_mycomaps_name(self):
        metric = {"species_name": "Hygrocybe minutula",
                  "mycomap_location": "Petersham Massachusetts US"}
        self.assertEqual(
            improve_mycomap_sequence_name(
                "Hygrocybe cf. minutula MA (1/0 PQ) (Peter 2083) iNat75529848", metric, "local"),
            "iNat75529848 Hygrocybe cf. minutula Petersham Massachusetts US")
        # Never added to a different epithet, nor to a machine-written header.
        self.assertEqual(
            improve_mycomap_sequence_name("Hygrocybe cf miniata KM248898", metric, "local"),
            "KM248898 Hygrocybe minutula Petersham Massachusetts US")
        self.assertEqual(
            improve_mycomap_sequence_name("iNat75529848 Hygrocybe cf minutula MA", metric, "local"),
            "iNat75529848 Hygrocybe minutula Petersham Massachusetts US")

    def test_the_curators_type_status_is_kept(self):
        metric = {"species_name": "Inocephalus furfuraceidiscus"}
        self.assertEqual(
            improve_mycomap_sequence_name(
                "Inocephalus furfuraceidiscus HOLOTYPE ID AHS69078 (=E. undulatosporum)",
                metric, "local", mycomap_record_id="df-47-71-8637"),
            "MycoMap#df-47-71-8637 Inocephalus furfuraceidiscus HOLOTYPE")
        # Inside a note "type" describes another record.
        metric = {"species_name": "Hygrocybe minutula"}
        self.assertEqual(
            improve_mycomap_sequence_name("Hygrocybe cf minutula QC (type NY) KM248898",
                                          metric, "local"),
            "KM248898 Hygrocybe cf minutula")

    def test_a_record_with_no_name_or_lookup_id_gets_mycomaps_id(self):
        header = "Russula 38 SAT 13-286-04 (WA birch R. nitida atypical)"
        metric = {"identity": 99.0}
        self.assertEqual(
            improve_mycomap_sequence_name(header, metric, "local",
                                          mycomap_record_id="df-47-66-18048"),
            f"MycoMap#df-47-66-18048 {header}")
        # Anything a reader can open already identifies it.
        for named in ("R. nitida UNITE unanimous KU205349",
                      "Pholiotina gracilenta NZ iNat1351891"):
            self.assertEqual(
                improve_mycomap_sequence_name(named, metric, "local",
                                              mycomap_record_id="df-1"),
                named)

    def test_saved_job_metadata_keeps_the_provenance(self):
        from app.api.routes import _normalize_sequence_metadata
        [row] = _normalize_sequence_metadata([{
            "name": "UDB0799647 Entoloma undulatosporum", "source": "mycomap",
            "hit_source": "local", "mycomap_record_id": "df-47-71-8636",
            "mycomap_dataset": "Danny Miller WC Annotations"}])
        self.assertEqual(row["mycomap_record_id"], "df-47-71-8636")
        self.assertEqual(row["mycomap_dataset"], "Danny Miller WC Annotations")
        [plain] = _normalize_sequence_metadata([{"name": "KM248898 Hygrocybe minutula"}])
        self.assertNotIn("mycomap_dataset", plain)


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


class TestExistingBlastResolution(unittest.TestCase):
    """Resolving a legacy do=results link, and finding a search by known ID."""

    CANDIDATES = [
        {"blast_id": "101", "url": "https://mycomap.com/genetics/blast-search/a-r101/"},
        {"blast_id": "102", "url": "https://mycomap.com/genetics/blast-search/a-r102/"},
        {"blast_id": "103", "url": "https://mycomap.com/genetics/blast-search/a-r103/"},
    ]

    def _choose(self, statuses, **kwargs):
        records = {bid: ({"status": st} if st is not None else None)
                   for bid, st in statuses.items()}
        with patch.object(mycomap_service, "fetch_mycomap_blast_record",
                          side_effect=lambda bid: records.get(bid)) as fetch:
            chosen = mycomap_service._choose_existing_blast(self.CANDIDATES, **kwargs)
        return chosen, fetch.call_count

    def test_the_oldest_finished_search_wins(self):
        chosen, _ = self._choose({"101": "running", "102": "complete", "103": "complete"})
        self.assertEqual((chosen["blast_id"], chosen["status"]), ("102", "complete"))

    def test_an_unreadable_status_counts_as_unknown(self):
        chosen, _ = self._choose({"101": "running", "102": None, "103": "running"})
        self.assertEqual((chosen["blast_id"], chosen["status"]), ("102", "unknown"))

    def test_a_status_budget_limits_the_checks_and_falls_back_to_the_next(self):
        # An interactive request asks once; if that search is still running, the
        # next one is offered as unknown instead of "no search exists".
        chosen, calls = self._choose({"101": "running", "102": "complete"},
                                     max_status_checks=1)
        self.assertEqual(calls, 1)
        self.assertEqual((chosen["blast_id"], chosen["status"]), ("102", "unknown"))

    def test_a_time_budget_keeps_checking_while_time_remains(self):
        # Bounding by time rather than to one check finds an older finished
        # search instead of offering an unchecked one as unknown.
        chosen, calls = self._choose(
            {"101": "running", "102": "running", "103": "complete"},
            status_check_deadline=float("inf"),
        )
        self.assertEqual(calls, 3)
        self.assertEqual((chosen["blast_id"], chosen["status"]), ("103", "complete"))

    def test_a_spent_time_budget_still_makes_one_check(self):
        chosen, calls = self._choose(
            {"101": "running", "102": "complete"}, status_check_deadline=0.0,
        )
        self.assertEqual(calls, 1)
        self.assertEqual((chosen["blast_id"], chosen["status"]), ("102", "unknown"))

    def test_a_pending_history_job_id_is_not_taken_for_a_blast_id(self):
        # MycoMap's job id is not necessarily the result's r<id>; only the latter
        # may drive the known-id lookup.
        row = {"title": "iNat1 DNA Barcode ITS", "status": "queued", "job_id": "77"}
        pending = {}
        with patch.object(mycomap_service, "_history_backoff_active", return_value=False), \
                patch.object(mycomap_service, "_fetch_mycomap_history", return_value=[row]):
            found = mycomap_service.find_mycomap_blast_via_history(
                "iNat1 DNA Barcode ITS", pending_out=pending)
        self.assertIsNone(found)
        self.assertEqual(pending, {"blast_id": "77", "id_kind": "job"})

    def test_a_fresh_history_lookup_skips_the_shared_cache(self):
        class Conn:
            def __init__(self):
                self.writes = []
            def get(self, key):
                raise AssertionError("a fresh lookup must not read the cache")
            def set(self, key, value, **kwargs):
                self.writes.append(key)
                return True
        conn = Conn()
        with patch.object(mycomap_service, "_shared_redis", return_value=conn), \
                patch.object(mycomap_service, "_mycomap_refresh_request",
                             return_value=[{"title": "x"}]) as request:
            payload = mycomap_service._fetch_mycomap_history(1, fresh=True)
        request.assert_called_once()
        self.assertEqual(payload, [{"title": "x"}])
        self.assertEqual(len(conn.writes), 1, "the fresh page still refills the cache")

    def test_every_search_still_running_is_none(self):
        chosen, calls = self._choose({"101": "running", "102": "running", "103": "running"})
        self.assertIsNone(chosen)
        self.assertEqual(calls, 3)

    def test_an_unreachable_blast_list_is_reported_as_a_warning(self):
        url = ("https://mycomap.com/index.php?app=genbank&module=genbank"
               "&controller=blast&do=results&db=42&id=123")
        warnings = []
        with patch.object(mycomap_service, "diagnostic_urlopen",
                          side_effect=OSError("connection refused")):
            chosen = mycomap_service.resolve_legacy_mycomap_results_url(url, warnings=warnings)
        self.assertIsNone(chosen)
        self.assertEqual(len(warnings), 1)

    def test_a_known_id_is_still_found_when_the_status_record_is_unavailable(self):
        found_url = "https://mycomap.com/genetics/blast-search/inat1-dna-barcode-its-r555/"
        with patch.object(mycomap_service, "fetch_mycomap_blast_record", return_value=None), \
                patch.object(mycomap_service, "find_mycomap_record_url_by_id",
                             return_value=found_url) as by_id:
            result = mycomap_service.find_mycomap_blast_by_known_id("555", "iNat1 DNA Barcode ITS")
        by_id.assert_called_once()
        self.assertIsNotNone(result)

    def test_an_unfinished_known_id_costs_no_page_lookup(self):
        with patch.object(mycomap_service, "fetch_mycomap_blast_record",
                          return_value={"status": "running"}), \
                patch.object(mycomap_service, "find_mycomap_record_url_by_id") as by_id:
            result = mycomap_service.find_mycomap_blast_by_known_id("555", "iNat1 DNA Barcode ITS")
        self.assertIsNone(result)
        by_id.assert_not_called()

    def test_job_id_requires_matching_result_page_title_even_on_listing_hit(self):
        wrong_url = "https://mycomap.com/genetics/blast-search/inat2-dna-barcode-its-r555/"
        page = b"<title>iNat2 DNA Barcode ITS - BLAST Search - MycoMap Beta</title>"
        with patch.object(mycomap_service, "fetch_mycomap_blast_record",
                          return_value={"status": "complete"}), \
                patch.object(mycomap_service, "find_mycomap_record_url_by_id",
                             return_value=wrong_url), \
                patch.object(mycomap_service, "diagnostic_urlopen",
                             return_value=io.BytesIO(page)):
            result = mycomap_service.find_mycomap_blast_by_known_id(
                "555", "iNat1 DNA Barcode ITS", verify_title=True
            )
        self.assertIsNone(result)

    def test_job_id_accepts_matching_result_page_title(self):
        page = b"<title>iNat1 DNA Barcode ITS - ABC123 - BLAST Search - MycoMap Beta</title>"
        with patch.object(mycomap_service, "fetch_mycomap_blast_record",
                          return_value={"status": "complete"}), \
                patch.object(mycomap_service, "find_mycomap_record_url_by_id",
                             return_value=None), \
                patch.object(mycomap_service, "diagnostic_urlopen",
                             return_value=io.BytesIO(page)):
            result = mycomap_service.find_mycomap_blast_by_known_id(
                "555", "iNat1 DNA Barcode ITS", verify_title=True
            )
        self.assertEqual(result["blast_id"], "555")

class DropRedundantInatBioMaterialTests(unittest.TestCase):
    drop = staticmethod(mycomap_service._drop_redundant_inat_bio_material)

    def test_drops_bio_material_repeating_an_existing_reference(self):
        self.assertEqual(
            self.drop("Amanita sp. isolate OMDL iNat # 217417974 bio-material iNAT:217417974"),
            "Amanita sp. isolate OMDL iNat # 217417974",
        )

    def test_keeps_the_only_reference(self):
        text = "Amanita sp. bio-material iNAT:217417974"
        self.assertEqual(self.drop(text), text)

    def test_keeps_one_copy_of_a_repeated_only_reference(self):
        self.assertEqual(
            self.drop("Amanita sp. bio-material iNAT:217417974 ITS bio-material iNAT:217417974"),
            "Amanita sp. bio-material iNAT:217417974 ITS",
        )


if __name__ == '__main__':
    unittest.main()
