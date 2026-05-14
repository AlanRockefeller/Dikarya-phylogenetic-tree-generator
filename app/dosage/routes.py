from flask import jsonify, request

from app.api import bp
from app.dosage import service


def _json_error(message, status=400):
    return jsonify({"status": "error", "error": message}), status


@bp.route("/dosage/species")
def dosage_species():
    try:
        species = service.list_species(
            q=request.args.get("q", ""),
            limit=request.args.get("limit", 75),
        )
    except service.DosageValidationError as exc:
        return _json_error(str(exc), 400)
    except service.DosageDataUnavailable as exc:
        return _json_error(str(exc), 503)
    return jsonify({"status": "success", "species": species})


@bp.route("/dosage/species/<int:species_id>/strains")
def dosage_strains(species_id):
    try:
        strains = service.list_strains(species_id)
    except service.DosageValidationError as exc:
        return _json_error(str(exc), 400)
    except service.DosageDataUnavailable as exc:
        return _json_error(str(exc), 503)
    return jsonify({"status": "success", "strains": strains})


@bp.route("/dosage/results")
def dosage_results():
    try:
        rows = service.query_results(
            request.args.get("species_id"),
            strain=request.args.get("strain"),
            data_mode=request.args.get("data_mode", "show_all"),
            material_state=request.args.get("material_state"),
            percent_basis=request.args.get("percent_basis"),
        )
    except service.DosageValidationError as exc:
        return _json_error(str(exc), 400)
    except service.DosageDataUnavailable as exc:
        return _json_error(str(exc), 503)
    return jsonify({"status": "success", "results": rows})


@bp.route("/dosage/calculate")
def dosage_calculate():
    try:
        payload = service.calculate(
            species_id=request.args.get("species_id"),
            strain_or_variety=request.args.get("strain_or_variety") or request.args.get("strain"),
            grams=request.args.get("grams"),
            material_state=request.args.get("material_state", "unknown"),
            data_mode=request.args.get("data_mode", "best_available"),
        )
    except service.DosageValidationError as exc:
        return _json_error(str(exc), 400)
    except service.DosageDataUnavailable as exc:
        return _json_error(str(exc), 503)
    return jsonify({"status": "success", **payload})

