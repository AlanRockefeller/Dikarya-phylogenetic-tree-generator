import csv
import sqlite3
from pathlib import Path

from app.dosage.db import connect


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

POTENCY_FIELDS = [
    "psilocybin_pct",
    "psilocin_pct",
    "baeocystin_pct",
    "aeruginascin_pct",
    "norpsilocin_pct",
]


class DosageImportError(ValueError):
    pass


def _read_csv(path, expected_headers):
    if not path.exists():
        raise DosageImportError(f"Required CSV file is missing: {path.name}")

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != expected_headers:
            raise DosageImportError(
                f"{path.name} has invalid headers. Expected: {', '.join(expected_headers)}"
            )
        return [
            {key: (value.strip() if value is not None else "") for key, value in row.items()}
            for row in reader
        ]


def _optional_int(value, field, row_label):
    if value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise DosageImportError(f"{row_label}: {field} must be an integer") from exc


def _optional_float(value, field, row_label):
    if value == "":
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise DosageImportError(f"{row_label}: {field} must be numeric or blank") from exc
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise DosageImportError(f"{row_label}: {field} must be finite")
    return parsed


def _create_schema(connection):
    connection.executescript(
        """
        PRAGMA foreign_keys=OFF;
        DROP TABLE IF EXISTS dosage_test_results;
        DROP TABLE IF EXISTS dosage_species;
        DROP TABLE IF EXISTS dosage_references;
        PRAGMA foreign_keys=ON;

        CREATE TABLE dosage_references (
            reference_id INTEGER PRIMARY KEY,
            citation_key TEXT NOT NULL,
            authors TEXT,
            year INTEGER,
            title TEXT,
            doi TEXT,
            journal TEXT,
            url TEXT,
            notes TEXT
        );

        CREATE TABLE dosage_species (
            species_id INTEGER PRIMARY KEY,
            genus TEXT,
            specific_epithet TEXT,
            scientific_name TEXT NOT NULL,
            authority TEXT,
            common_name TEXT,
            strain TEXT,
            current_species_id INTEGER,
            notes TEXT,
            FOREIGN KEY(current_species_id) REFERENCES dosage_species(species_id)
        );

        CREATE TABLE dosage_test_results (
            result_id INTEGER PRIMARY KEY,
            species_id INTEGER NOT NULL,
            reference_id INTEGER NOT NULL,
            strain_or_variety TEXT,
            part_tested TEXT,
            material_state TEXT,
            percent_basis TEXT,
            source_type TEXT,
            psilocybin_pct REAL,
            psilocin_pct REAL,
            baeocystin_pct REAL,
            aeruginascin_pct REAL,
            norpsilocin_pct REAL,
            notes TEXT,
            FOREIGN KEY(species_id) REFERENCES dosage_species(species_id),
            FOREIGN KEY(reference_id) REFERENCES dosage_references(reference_id)
        );

        CREATE INDEX idx_dosage_species_scientific_name ON dosage_species(scientific_name);
        CREATE INDEX idx_dosage_species_binomial ON dosage_species(genus, specific_epithet);
        CREATE INDEX idx_dosage_species_strain ON dosage_species(strain);
        CREATE INDEX idx_dosage_results_species_id ON dosage_test_results(species_id);
        CREATE INDEX idx_dosage_results_reference_id ON dosage_test_results(reference_id);
        CREATE INDEX idx_dosage_results_source_type ON dosage_test_results(source_type);
        CREATE INDEX idx_dosage_results_percent_basis ON dosage_test_results(percent_basis);
        CREATE INDEX idx_dosage_results_material_state ON dosage_test_results(material_state);
        """
    )


def rebuild_database(csv_dir, db_path):
    csv_dir = Path(csv_dir)
    references = _read_csv(csv_dir / "references.csv", REFERENCE_HEADERS)
    species = _read_csv(csv_dir / "species.csv", SPECIES_HEADERS)
    results = _read_csv(csv_dir / "test_results.csv", RESULT_HEADERS)

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with connect(db_path) as connection:
            _create_schema(connection)
            for row in references:
                row_label = f"references.csv reference_id={row.get('reference_id')}"
                connection.execute(
                    """
                    INSERT INTO dosage_references
                    (reference_id, citation_key, authors, year, title, doi, journal, url, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _optional_int(row["reference_id"], "reference_id", row_label),
                        row["citation_key"],
                        row["authors"] or None,
                        _optional_int(row["year"], "year", row_label),
                        row["title"] or None,
                        row["doi"] or None,
                        row["journal"] or None,
                        row["url"] or None,
                        row["notes"] or None,
                    ),
                )

            for row in species:
                row_label = f"species.csv species_id={row.get('species_id')}"
                connection.execute(
                    """
                    INSERT INTO dosage_species
                    (species_id, genus, specific_epithet, scientific_name, authority,
                     common_name, strain, current_species_id, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _optional_int(row["species_id"], "species_id", row_label),
                        row["genus"] or None,
                        row["specific_epithet"] or None,
                        row["scientific_name"],
                        row["authority"] or None,
                        row["common_name"] or None,
                        row["strain"] or None,
                        _optional_int(row["current_species_id"], "current_species_id", row_label),
                        row["notes"] or None,
                    ),
                )

            for row in results:
                row_label = f"test_results.csv result_id={row.get('result_id')}"
                connection.execute(
                    """
                    INSERT INTO dosage_test_results
                    (result_id, species_id, reference_id, strain_or_variety, part_tested,
                     material_state, percent_basis, source_type, psilocybin_pct, psilocin_pct,
                     baeocystin_pct, aeruginascin_pct, norpsilocin_pct, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _optional_int(row["result_id"], "result_id", row_label),
                        _optional_int(row["species_id"], "species_id", row_label),
                        _optional_int(row["reference_id"], "reference_id", row_label),
                        row["strain_or_variety"] or None,
                        row["part_tested"] or None,
                        row["material_state"] or None,
                        row["percent_basis"] or None,
                        row["source_type"] or None,
                        *[
                            _optional_float(row[field], field, row_label)
                            for field in POTENCY_FIELDS
                        ],
                        row["notes"] or None,
                    ),
                )
            connection.commit()
    except sqlite3.IntegrityError as exc:
        raise DosageImportError(f"CSV foreign key or uniqueness validation failed: {exc}") from exc

    return {
        "references": len(references),
        "species": len(species),
        "test_results": len(results),
        "database": str(db_path),
    }

