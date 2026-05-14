import csv
import tempfile
import unittest
from pathlib import Path

from app import create_app
from app.dosage.importer import DosageImportError, rebuild_database


REFERENCE_HEADERS = [
    "reference_id",
    "citation_key",
    "authors",
    "year",
    "title",
    "doi",
    "journal",
    "url",
    "notes",
]

SPECIES_HEADERS = [
    "species_id",
    "genus",
    "specific_epithet",
    "scientific_name",
    "authority",
    "common_name",
    "strain",
    "current_species_id",
    "notes",
]

RESULT_HEADERS = [
    "result_id",
    "species_id",
    "reference_id",
    "strain_or_variety",
    "part_tested",
    "material_state",
    "percent_basis",
    "source_type",
    "psilocybin_pct",
    "psilocin_pct",
    "baeocystin_pct",
    "aeruginascin_pct",
    "norpsilocin_pct",
    "notes",
]


def write_csv(path, headers, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


class DosageCalculatorTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.csv_dir = self.root / "csv"
        self.csv_dir.mkdir()
        self.db_path = self.root / "dosage.sqlite"
        self._write_valid_csvs()
        rebuild_database(self.csv_dir, self.db_path)

        self.app = create_app("development")
        self.app.config.update(
            TESTING=True,
            DOSAGE_DB_PATH=self.db_path,
            DOSAGE_CSV_DIR=self.csv_dir,
            WTF_CSRF_ENABLED=False,
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_valid_csvs(self):
        write_csv(
            self.csv_dir / "references.csv",
            REFERENCE_HEADERS,
            [
                {
                    "reference_id": "1",
                    "citation_key": "Peer2024",
                    "authors": "Researcher A.",
                    "year": "2024",
                    "title": "Peer reviewed assay",
                    "doi": "10.1000/test",
                    "journal": "Journal of Tests",
                    "url": "https://example.test/source",
                    "notes": "LC-MS/MS.",
                },
                {
                    "reference_id": "2",
                    "citation_key": "Public2024",
                    "authors": "Public Lab",
                    "year": "2024",
                    "title": "Public aggregation",
                    "doi": "",
                    "journal": "",
                    "url": "https://example.test/public",
                    "notes": "Aggregation.",
                },
            ],
        )
        write_csv(
            self.csv_dir / "species.csv",
            SPECIES_HEADERS,
            [
                {
                    "species_id": "1",
                    "genus": "Psilocybe",
                    "specific_epithet": "cubensis",
                    "scientific_name": "Psilocybe cubensis",
                    "authority": "",
                    "common_name": "gold cap",
                    "strain": "",
                    "current_species_id": "",
                    "notes": "generic",
                },
                {
                    "species_id": "2",
                    "genus": "Psilocybe",
                    "specific_epithet": "cubensis",
                    "scientific_name": "Psilocybe cubensis",
                    "authority": "",
                    "common_name": "gold cap",
                    "strain": "Test Strain",
                    "current_species_id": "1",
                    "notes": "strain label",
                },
                {
                    "species_id": "3",
                    "genus": "Psilocybe",
                    "specific_epithet": "cubensis",
                    "scientific_name": "Psilocybe cubensis",
                    "authority": "",
                    "common_name": "gold cap",
                    "strain": "Fresh Basis",
                    "current_species_id": "1",
                    "notes": "fresh basis strain label",
                },
            ],
        )
        write_csv(
            self.csv_dir / "test_results.csv",
            RESULT_HEADERS,
            [
                {
                    "result_id": "1",
                    "species_id": "2",
                    "reference_id": "1",
                    "strain_or_variety": "Test Strain",
                    "part_tested": "whole_fruiting_body",
                    "material_state": "dried",
                    "percent_basis": "dry_weight",
                    "source_type": "lab-grown",
                    "psilocybin_pct": "0.7",
                    "psilocin_pct": "",
                    "baeocystin_pct": "0",
                    "aeruginascin_pct": "",
                    "norpsilocin_pct": "",
                    "notes": "source row",
                },
                {
                    "result_id": "2",
                    "species_id": "2",
                    "reference_id": "2",
                    "strain_or_variety": "Test Strain",
                    "part_tested": "whole_fruiting_body",
                    "material_state": "dried",
                    "percent_basis": "dry_weight",
                    "source_type": "public_lab_aggregation",
                    "psilocybin_pct": "1.2",
                    "psilocin_pct": "",
                    "baeocystin_pct": "",
                    "aeruginascin_pct": "",
                    "norpsilocin_pct": "",
                    "notes": "overall potency midpoint of range",
                },
                {
                    "result_id": "3",
                    "species_id": "3",
                    "reference_id": "2",
                    "strain_or_variety": "Fresh Basis",
                    "part_tested": "whole_fruiting_body",
                    "material_state": "fresh",
                    "percent_basis": "fresh_weight",
                    "source_type": "public_lab_aggregation",
                    "psilocybin_pct": "0.07",
                    "psilocin_pct": "",
                    "baeocystin_pct": "",
                    "aeruginascin_pct": "",
                    "norpsilocin_pct": "",
                    "notes": "fresh-weight row",
                },
            ],
        )

    def test_import_valid_csv_and_blank_potency_is_null(self):
        import sqlite3

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT psilocybin_pct, psilocin_pct, baeocystin_pct FROM dosage_test_results WHERE result_id = 1"
            ).fetchone()

        self.assertEqual(row[0], 0.7)
        self.assertIsNone(row[1])
        self.assertEqual(row[2], 0.0)

    def test_import_missing_file_fails_clearly(self):
        (self.csv_dir / "references.csv").unlink()
        with self.assertRaisesRegex(DosageImportError, "references.csv"):
            rebuild_database(self.csv_dir, self.root / "missing.sqlite")

    def test_import_missing_header_fails_clearly(self):
        write_csv(self.csv_dir / "references.csv", REFERENCE_HEADERS[:-1], [])
        with self.assertRaisesRegex(DosageImportError, "invalid headers"):
            rebuild_database(self.csv_dir, self.root / "headers.sqlite")

    def test_import_invalid_foreign_key_fails_clearly(self):
        with (self.csv_dir / "test_results.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["species_id"] = "999"
        write_csv(self.csv_dir / "test_results.csv", RESULT_HEADERS, rows)
        with self.assertRaisesRegex(DosageImportError, "foreign key"):
            rebuild_database(self.csv_dir, self.root / "fk.sqlite")

    def test_calculation_formula_and_missing_values(self):
        response = self.client.get(
            "/api/dosage/calculate",
            query_string={
                "species_id": "1",
                "strain_or_variety": "Test Strain",
                "grams": "1",
                "material_state": "dried",
                "data_mode": "best_available",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["summary"]["psilocybin"]["estimate_mg"], 7.0)
        self.assertIsNone(payload["summary"]["psilocin"]["estimate_mg"])
        self.assertEqual(payload["summary"]["baeocystin"]["estimate_mg"], 0.0)

    def test_invalid_grams_rejected(self):
        response = self.client.get(
            "/api/dosage/calculate",
            query_string={
                "species_id": "1",
                "grams": "NaN",
                "material_state": "dried",
                "data_mode": "best_available",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("finite", response.get_json()["error"])

    def test_basis_mismatch_warning_appears(self):
        response = self.client.get(
            "/api/dosage/calculate",
            query_string={
                "species_id": "1",
                "strain_or_variety": "Test Strain",
                "grams": "1",
                "material_state": "fresh",
                "data_mode": "best_available",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        warnings = " ".join(payload["warnings"])
        self.assertIn("Dry-weight potency was converted", warnings)
        self.assertAlmostEqual(payload["summary"]["psilocybin"]["estimate_mg"], 0.7)

    def test_fresh_weight_source_converts_to_dried_material(self):
        response = self.client.get(
            "/api/dosage/calculate",
            query_string={
                "species_id": "3",
                "grams": "1",
                "material_state": "dried",
                "data_mode": "best_available",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        warnings = " ".join(payload["warnings"])
        self.assertIn("Fresh-weight potency was converted", warnings)
        self.assertAlmostEqual(payload["summary"]["psilocybin"]["estimate_mg"], 7.0)

    def test_species_api_returns_json(self):
        response = self.client.get("/api/dosage/species")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "success")
        self.assertTrue(response.get_json()["species"])

    def test_calculate_validates_required_fields(self):
        response = self.client.get("/api/dosage/calculate")

        self.assertEqual(response.status_code, 400)
        self.assertIn("species_id", response.get_json()["error"])

    def test_best_available_is_deterministic(self):
        first = self.client.get(
            "/api/dosage/calculate",
            query_string={
                "species_id": "1",
                "strain_or_variety": "Test Strain",
                "grams": "1",
                "material_state": "dried",
                "data_mode": "best_available",
            },
        ).get_json()
        second = self.client.get(
            "/api/dosage/calculate",
            query_string={
                "species_id": "1",
                "strain_or_variety": "Test Strain",
                "grams": "1",
                "material_state": "dried",
                "data_mode": "best_available",
            },
        ).get_json()

        self.assertEqual(first["selected_rows"][0]["row"]["result_id"], 1)
        self.assertEqual(second["selected_rows"][0]["row"]["result_id"], 1)

    def test_selected_strain_species_does_not_expand_to_all_strains(self):
        species_response = self.client.get("/api/dosage/species", query_string={"q": "Test Strain"})
        species_rows = species_response.get_json()["species"]
        selected = next(row for row in species_rows if row["species_id"] == 2)

        response = self.client.get(
            "/api/dosage/calculate",
            query_string={
                "species_id": "2",
                "grams": "1",
                "material_state": "dried",
                "data_mode": "best_available",
            },
        )

        self.assertEqual(selected["result_count"], 2)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["selected_rows"][0]["row"]["result_id"], 1)
        self.assertEqual(payload["summary"]["psilocybin"]["estimate_mg"], 7.0)

    def test_test_page_renders_widget(self):
        response = self.client.get("/test")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('id="dosage-estimator"', body)
        self.assertIn('id="dosage-grams"', body)


if __name__ == "__main__":
    unittest.main()
