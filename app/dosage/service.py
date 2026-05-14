import math

from app.dosage.db import connect


COMPOUNDS = [
    ("psilocybin", "psilocybin_pct"),
    ("psilocin", "psilocin_pct"),
    ("baeocystin", "baeocystin_pct"),
    ("aeruginascin", "aeruginascin_pct"),
    ("norpsilocin", "norpsilocin_pct"),
]

DATA_MODES = {
    "best_available",
    "academic_only",
    "include_public_lab",
    "include_literature_compilations",
    "show_all",
}

MATERIAL_STATES = {"dried", "fresh", "unknown"}
BASIS_BY_MATERIAL_STATE = {"dried": "dry_weight", "fresh": "fresh_weight"}
GENERIC_STRAIN = "__generic__"

HIGH_SOURCE_TYPES = {
    "lab-grown",
    "herbarium",
    "wild",
    "cultivated",
}
MEDIUM_SOURCE_TYPES = {
    "review_table",
    "literature_compilation",
    "older_literature",
}
LOW_SOURCE_TYPES = {"public_lab_aggregation"}


class DosageDataUnavailable(RuntimeError):
    pass


class DosageValidationError(ValueError):
    pass


def _rows(connection, query, params=()):
    return [dict(row) for row in connection.execute(query, params).fetchall()]


def _ensure_database(connection):
    try:
        connection.execute("SELECT 1 FROM dosage_species LIMIT 1").fetchone()
    except Exception as exc:
        raise DosageDataUnavailable(
            "Dosage calculator data has not been imported yet."
        ) from exc


def _species_group_condition(species_id):
    return (
        "(s.species_id = ? OR s.current_species_id = ? OR "
        "s.species_id = (SELECT current_species_id FROM dosage_species WHERE species_id = ?))"
    )


def _get_species(connection, species_id):
    row = connection.execute(
        """
        SELECT species_id, scientific_name, strain, current_species_id
        FROM dosage_species
        WHERE species_id = ?
        """,
        (species_id,),
    ).fetchone()
    if not row:
        raise DosageValidationError("species_id was not found.")
    return dict(row)


def list_species(q=None, limit=75):
    search = (q or "").strip()
    if len(search) > 80:
        raise DosageValidationError("Search query is too long.")
    limit = max(1, min(int(limit or 75), 100))

    params = []
    where = ""
    if search:
        where = """
            WHERE s.scientific_name LIKE ?
               OR s.common_name LIKE ?
               OR s.strain LIKE ?
               OR s.genus LIKE ?
               OR s.specific_epithet LIKE ?
        """
        pattern = f"%{search}%"
        params.extend([pattern, pattern, pattern, pattern, pattern])

    params.append(limit)
    query = f"""
        SELECT
            s.species_id,
            s.scientific_name,
            s.common_name,
            s.strain,
            s.current_species_id,
            (
                SELECT COUNT(*)
                FROM dosage_test_results r
                WHERE r.species_id = s.species_id
            ) AS result_count
        FROM dosage_species s
        {where}
        WHERE result_count > 0
        ORDER BY
            CASE WHEN s.scientific_name = 'Psilocybe cubensis' AND COALESCE(s.strain, '') = '' THEN 0
                 WHEN s.scientific_name = 'Psilocybe cubensis' THEN 1
                 ELSE 2 END,
            s.scientific_name COLLATE NOCASE,
            COALESCE(s.strain, '') COLLATE NOCASE
        LIMIT ?
    """
    if where:
        query = query.replace(f"{where}\n        WHERE result_count > 0", f"{where}\n          AND result_count > 0")

    with connect() as connection:
        _ensure_database(connection)
        return _rows(connection, query, params)


def list_strains(species_id):
    species_id = _parse_int(species_id, "species_id")
    with connect() as connection:
        _ensure_database(connection)
        species = _get_species(connection, species_id)
        if species.get("strain"):
            query = """
                SELECT DISTINCT COALESCE(r.strain_or_variety, s.strain, '') AS strain_label
                FROM dosage_test_results r
                JOIN dosage_species s ON s.species_id = r.species_id
                WHERE r.species_id = ?
                ORDER BY strain_label COLLATE NOCASE
            """
            params = (species_id,)
        else:
            query = f"""
                SELECT DISTINCT
                    COALESCE(r.strain_or_variety, s.strain, '') AS strain_label
                FROM dosage_test_results r
                JOIN dosage_species s ON s.species_id = r.species_id
                WHERE {_species_group_condition(species_id)}
                ORDER BY strain_label COLLATE NOCASE
            """
            params = (species_id, species_id, species_id)
        labels = [row["strain_label"] for row in connection.execute(query, params)]

    options = []
    if any(not label for label in labels):
        options.append({"value": GENERIC_STRAIN, "label": "generic / unspecified"})
    options.extend({"value": label, "label": label} for label in labels if label)
    return options


def quality_for_row(row):
    source_type = (row.get("source_type") or "").strip().lower()
    journal = (row.get("journal") or "").strip()
    notes = (row.get("result_notes") or row.get("notes") or "").lower()
    title = (row.get("title") or "").lower()
    reference_notes = (row.get("reference_notes") or "").lower()

    if "overall potency" in notes or "midpoint of range" in notes:
        return {
            "label": "Low confidence",
            "level": "low",
            "rank": 30,
            "reason": "Aggregated or midpoint values require cautious interpretation.",
        }
    if source_type in LOW_SOURCE_TYPES:
        return {
            "label": "Low confidence",
            "level": "low",
            "rank": 35,
            "reason": "Public lab aggregation data is useful context but less standardized.",
        }
    if source_type in MEDIUM_SOURCE_TYPES:
        return {
            "label": "Medium confidence",
            "level": "medium",
            "rank": 70,
            "reason": "Published compilation or review-table value.",
        }
    if "review" in title or "review article" in reference_notes or "review cites" in notes:
        return {
            "label": "Medium confidence",
            "level": "medium",
            "rank": 70,
            "reason": "Value is mediated through a review or compilation source.",
        }
    if source_type in HIGH_SOURCE_TYPES and journal:
        return {
            "label": "High confidence",
            "level": "high",
            "rank": 100,
            "reason": "Analytical row with a peer-reviewed source.",
        }
    if journal:
        return {
            "label": "Medium confidence",
            "level": "medium",
            "rank": 65,
            "reason": "Published source with limited assay context.",
        }
    return {
        "label": "Needs review",
        "level": "needs_review",
        "rank": 10,
        "reason": "Source or basis needs manual review.",
    }


def _mode_allowed(row, data_mode):
    source_type = (row.get("source_type") or "").strip().lower()
    journal = (row.get("journal") or "").strip()
    if data_mode == "show_all" or data_mode == "best_available":
        return True
    if data_mode == "academic_only":
        return bool(journal) and source_type != "public_lab_aggregation"
    if data_mode == "include_public_lab":
        return source_type != "literature_compilation"
    if data_mode == "include_literature_compilations":
        return source_type != "public_lab_aggregation"
    return False


def _base_results(species_id):
    species_id = _parse_int(species_id, "species_id")
    base_select = """
        SELECT
            r.result_id,
            r.species_id,
            r.reference_id,
            r.strain_or_variety,
            r.part_tested,
            r.material_state,
            r.percent_basis,
            r.source_type,
            r.psilocybin_pct,
            r.psilocin_pct,
            r.baeocystin_pct,
            r.aeruginascin_pct,
            r.norpsilocin_pct,
            r.notes AS result_notes,
            s.scientific_name,
            s.common_name,
            s.strain AS species_strain,
            s.current_species_id,
            ref.citation_key,
            ref.authors,
            ref.year,
            ref.title,
            ref.doi,
            ref.journal,
            ref.url,
            ref.notes AS reference_notes
        FROM dosage_test_results r
        JOIN dosage_species s ON s.species_id = r.species_id
        JOIN dosage_references ref ON ref.reference_id = r.reference_id
    """
    with connect() as connection:
        _ensure_database(connection)
        species = _get_species(connection, species_id)
        if species.get("strain"):
            query = base_select + """
                WHERE r.species_id = ?
                ORDER BY r.result_id
            """
            params = (species_id,)
        else:
            query = base_select + f"""
                WHERE {_species_group_condition(species_id)}
        ORDER BY r.result_id
            """
            params = (species_id, species_id, species_id)
        rows = _rows(connection, query, params)

    for row in rows:
        quality = quality_for_row(row)
        row["quality"] = quality
        row["display_strain"] = row.get("strain_or_variety") or row.get("species_strain") or ""
        row["is_aggregate_potency"] = _is_aggregate_potency(row)
    return rows


def query_results(species_id, strain=None, data_mode="show_all", material_state=None, percent_basis=None):
    if data_mode not in DATA_MODES:
        raise DosageValidationError("Invalid data mode.")
    if material_state and material_state not in MATERIAL_STATES:
        raise DosageValidationError("Invalid material state.")
    clean_basis = (percent_basis or "").strip()
    parsed_species_id = _parse_int(species_id, "species_id")
    selected_strain = _normalize_strain(strain)
    if not selected_strain:
        selected_strain = _selected_species_strain(parsed_species_id)

    rows = []
    for row in _base_results(parsed_species_id):
        if selected_strain == GENERIC_STRAIN and row["display_strain"]:
            continue
        if selected_strain and selected_strain != GENERIC_STRAIN:
            if row["display_strain"].strip().lower() != selected_strain.lower():
                continue
        if clean_basis and (row.get("percent_basis") or "") != clean_basis:
            continue
        if material_state and material_state != "unknown":
            if row.get("material_state") and row.get("material_state") != material_state:
                continue
        if not _mode_allowed(row, data_mode):
            continue
        rows.append(row)
    return rows


def calculate(species_id, grams, material_state, data_mode="best_available", strain_or_variety=None):
    parsed_species_id = _parse_int(species_id, "species_id")
    parsed_grams = _parse_grams(grams)
    if material_state not in MATERIAL_STATES:
        raise DosageValidationError("Choose dried, fresh, or unknown material state.")
    if data_mode not in DATA_MODES:
        raise DosageValidationError("Invalid data mode.")
    effective_strain = _normalize_strain(strain_or_variety) or _selected_species_strain(parsed_species_id)

    candidate_mode = "show_all" if data_mode == "best_available" else data_mode
    rows = query_results(
        parsed_species_id,
        strain=effective_strain,
        data_mode=candidate_mode,
        material_state=None,
    )
    if data_mode == "best_available" and rows:
        rows = _best_available_rows(rows, effective_strain)

    calculations = [_calculate_row(row, parsed_grams, material_state) for row in rows]
    summary = _summarize(calculations)
    warnings = _warnings(calculations, material_state, effective_strain)

    return {
        "grams": parsed_grams,
        "material_state": material_state,
        "data_mode": data_mode,
        "selected_rows": calculations,
        "summary": summary,
        "warnings": warnings,
        "formula": "mg = grams_material × effective_potency_percent × 10",
        "formula_note": "Effective potency adjusts dry-weight data 10x lower for fresh material, and fresh-weight data 10x higher for dried material, assuming mushrooms are about 90% water by weight.",
    }


def _best_available_rows(rows, strain):
    scored = sorted(rows, key=lambda row: _rank_row(row, strain), reverse=True)
    best_score = _rank_row(scored[0], strain)
    return [row for row in scored if _rank_row(row, strain) == best_score]


def _rank_row(row, strain):
    source_type = (row.get("source_type") or "").strip().lower()
    has_journal = bool((row.get("journal") or "").strip())
    has_strain = bool(_normalize_strain(strain))
    exact_strain = has_strain and row.get("display_strain", "").strip().lower() == _normalize_strain(strain).lower()
    no_strain = not row.get("display_strain")

    if exact_strain and source_type in HIGH_SOURCE_TYPES and has_journal:
        tier = 600
    elif exact_strain and source_type == "review_table":
        tier = 500
    elif exact_strain and source_type == "public_lab_aggregation":
        tier = 400
    elif no_strain and source_type in HIGH_SOURCE_TYPES and has_journal:
        tier = 300
    elif source_type == "literature_compilation":
        tier = 200
    else:
        tier = 100
    return (tier + row["quality"]["rank"], -(row.get("result_id") or 0))


def _calculate_row(row, grams, selected_material_state):
    conversion = _basis_conversion(row.get("percent_basis"), selected_material_state)
    compounds = {}
    for name, field in COMPOUNDS:
        percent = row.get(field)
        effective_percent = None if percent is None else percent * conversion["factor"]
        compounds[name] = {
            "percent": percent,
            "effective_percent": effective_percent,
            "mg": None if effective_percent is None else grams * effective_percent * 10,
            "reported": percent is not None,
        }
    row_warnings = []
    if conversion["warning"]:
        row_warnings.append(conversion["warning"])
    if row.get("is_aggregate_potency"):
        row_warnings.append(
            "This source may report aggregate potency or psilocybin-equivalent values rather than separated compounds."
        )

    return {
        "row": row,
        "compounds": compounds,
        "basis_conversion": conversion,
        "warnings": row_warnings,
        "formula_examples": {
            name: None if values["percent"] is None else _formula_example(grams, values, conversion)
            for name, values in compounds.items()
        },
    }


def _basis_conversion(percent_basis, selected_material_state):
    data_basis = (percent_basis or "").strip()
    if selected_material_state == "fresh" and data_basis == "dry_weight":
        return {
            "factor": 0.1,
            "label": "dry-to-fresh",
            "warning": "Dry-weight potency was converted to a fresh-material estimate by multiplying potency by 0.1, assuming about 90% water weight.",
        }
    if selected_material_state == "dried" and data_basis == "fresh_weight":
        return {
            "factor": 10.0,
            "label": "fresh-to-dry",
            "warning": "Fresh-weight potency was converted to a dried-material estimate by multiplying potency by 10, assuming about 90% water weight.",
        }
    return {
        "factor": 1.0,
        "label": "none",
        "warning": None,
    }


def _formula_example(grams, values, conversion):
    if conversion["factor"] == 1:
        return f"{grams:g} × {values['percent']:g} × 10 = {values['mg']:.6g} mg"
    return (
        f"{grams:g} × ({values['percent']:g} × {conversion['factor']:g}) "
        f"× 10 = {values['mg']:.6g} mg"
    )


def _summarize(calculations):
    summary = {}
    for name, _field in COMPOUNDS:
        reported = [item["compounds"][name]["mg"] for item in calculations if item["compounds"][name]["mg"] is not None]
        if not reported:
            summary[name] = {"reported": False, "min_mg": None, "max_mg": None, "estimate_mg": None}
            continue
        summary[name] = {
            "reported": True,
            "min_mg": min(reported),
            "max_mg": max(reported),
            "estimate_mg": reported[0] if len(reported) == 1 else None,
        }
    return summary


def _warnings(calculations, material_state, strain):
    warnings = [
        "This is an analytical-content estimate from reported data, not dosing advice.",
        "Natural samples vary widely by mushroom, flush, substrate, storage, method, and tissue part.",
    ]
    if strain:
        warnings.append("P. cubensis strain names are cultivation/commercial labels, not standardized taxonomic units.")
    if material_state == "unknown":
        warnings.append("Material state is unknown, so dry/fresh basis mismatches may be harder to interpret.")

    seen = set(warnings)
    for item in calculations:
        for warning in item["warnings"]:
            if warning not in seen:
                warnings.append(warning)
                seen.add(warning)
    return warnings


def _is_aggregate_potency(row):
    notes = (row.get("result_notes") or "").lower()
    return "overall potency" in notes or "psilocybin-equivalent" in notes or "aggregate potency" in notes


def _parse_int(value, field):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DosageValidationError(f"{field} must be an integer.") from exc
    if parsed <= 0:
        raise DosageValidationError(f"{field} must be positive.")
    return parsed


def _parse_grams(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise DosageValidationError("Amount in grams must be numeric.") from exc
    if not math.isfinite(parsed):
        raise DosageValidationError("Amount in grams must be finite.")
    if parsed <= 0:
        raise DosageValidationError("Amount in grams must be greater than 0.")
    if parsed > 10000:
        raise DosageValidationError("Amount is too large for this estimator.")
    return parsed


def _normalize_strain(value):
    return (value or "").strip()


def _selected_species_strain(species_id):
    with connect() as connection:
        _ensure_database(connection)
        species = _get_species(connection, species_id)
    return _normalize_strain(species.get("strain"))
