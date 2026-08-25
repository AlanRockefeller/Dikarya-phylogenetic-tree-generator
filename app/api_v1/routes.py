"""Public API v1 routes.

Phase 1 endpoints: /health, /me, /tokens
Phase 2 endpoints: /jobs (+ mutation), /jobs/{id}/files, /jobs/{id}/logs, /tools/*
"""
import json
import math
import re
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Response, current_app, g, request, send_file, stream_with_context, url_for,
)
from sqlalchemy import desc

from app.api_v1 import bp
from app.api_v1.auth import require_api_token, api_token_key_func
from app.api_v1.envelope import error_response, ok, paginate_query, server_error
from app.api_v1.idempotency import idempotent
from app.api_v1.job_defaults import (
    DEFAULT_ALIGNMENT_METHOD,
    DEFAULT_BOOTSTRAP,
    DEFAULT_TREE_METHOD,
)
from app.api_v1.jobs import (
    DOWNLOADABLE_ARTIFACTS, LOG_NAMES, _guess_mime, artifact_path,
    get_owned_job_or_404, list_available_artifacts, serialize_job,
)
from app.api_v1.openapi import build_spec
from app.config import Config
from app.extensions import db, limiter
from app.models import ApiToken, Job
from app.services.artifact_storage import read_artifact_bytes
from app.services.security_utils import validate_safe_file_path, coerce_bool
from app.services.tree_parameter_validation import (
    normalize_inherited_iqtree_ufboot_count,
    validate_iqtree_ufboot_count,
)
from app.services.tree_edit_service import (
    MAX_TREE_TIP_NAME_LENGTH,
    NEWICK_UNSAFE_TIP_CHARS,
)
from app.workers.queue import enqueue_job, enqueue_recompute_job

logger = logging.getLogger(__name__)


# ============================================================================
# Health + identity (Phase 1)
# ============================================================================

@bp.route('/health', methods=['GET'])
def api_health():
    """Unauthenticated ping so clients can verify the API is reachable."""
    return ok({"status": "ok", "api_version": "v1"})


@bp.route('/openapi.json', methods=['GET'])
def openapi_json():
    """Machine-readable OpenAPI 3.1 spec. Unauthenticated."""
    from flask import jsonify
    return jsonify(build_spec())


@bp.route('/docs', methods=['GET'])
def api_docs():
    """Interactive Swagger UI for the v1 API."""
    from flask import render_template
    return render_template('api_v1/docs.html')


@bp.route('/me', methods=['GET'])
@require_api_token(scope='account:read')
@limiter.limit("600 per minute", key_func=api_token_key_func)
def whoami():
    user = g.api_user
    return ok({
        "id": user.id,
        "email": user.email,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    })


@bp.route('/tokens', methods=['GET'])
@require_api_token(scope='account:read')
@limiter.limit("600 per minute", key_func=api_token_key_func)
def list_tokens():
    user = g.api_user
    tokens = sorted(user.api_tokens, key=lambda t: t.created_at, reverse=True)
    return ok([{
        "id": t.id,
        "name": t.name,
        "prefix": t.token_prefix,
        "scopes": list(t.scopes or []),
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
        "revoked_at": t.revoked_at.isoformat() if t.revoked_at else None,
    } for t in tokens])


# ============================================================================
# Job CRUD
# ============================================================================

def _clamp_int(value, default, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


VALID_TREE_METHODS = {"nj", "raxml", "iqtree", "mrbayes", "fasttree"}
# The input modes the public API accepts, mapped to the canonical value the
# worker understands. `fasta_upload` is deliberately absent: it expects a file
# already staged on disk by the web UI and is unreachable over JSON. Anything
# outside this table is rejected here rather than being handed to the worker,
# which used to accept it with 202 and fail with "Unknown input type" minutes
# later.
PUBLIC_INPUT_TYPE_ALIASES = {
    "pasted_sequence": "pasted_sequence",
    "sequence":        "pasted_sequence",
    "fasta":           "pasted_sequence",
    "accession_list":  "accession_list",
    "accessions":      "accession_list",
}
VALID_ALIGNERS    = {"mafft", "muscle", "clustalo", "iqtree_builtin", "default"}
VALID_TRIMMERS    = {"none", "trimal_gappy", "trimal", "bmge"}

# Per-field limits used in both create and recompute paths. Kept in one place
# so validation errors and the OpenAPI schema can quote the same numbers.
LIMITS = {
    "sequence_max_bytes":  5_000_000,
    "accessions_max":      500,
    "accession_str_max":   64,
    "notes_max":           2000,
    "tree_model_max":      64,
    "outgroup_max":        256,
    "bootstrap":           (0,     10_000),
    "alrt_replicates":     (0,     10_000),
    "mcmc_generations":    (1_000, 100_000_000),
    "mcmc_nruns":          (1,     8),
    "mcmc_nchains":        (1,     16),
    "mcmc_burnin_fraction": (0.0,   0.99),
}

# Fields a recompute may override. Sequence/accessions are intentionally
# excluded -- recompute is "re-run with different parameters on the same
# input data," not "submit a new dataset." Use POST /jobs for that.
RECOMPUTE_ALLOWED_FIELDS = frozenset({
    "tree_method", "tree_model",
    "alignment_method", "trimming_method", "trim_terminal_overhangs",
    "fix_orientation",
    "bootstrap", "alrt_replicates", "mcmc_generations", "mcmc_nruns", "mcmc_nchains",
    "mcmc_burnin_fraction", "mcmc_stop_early",
    "outgroup", "notes",
})


def _validate_bool(field, value, default=True):
    """Strict boolean validation for the public API.

    Shares the token sets with the worker/api paths via coerce_bool, but rejects
    (422) unrecognized strings and non-boolean JSON types instead of coercing.
    """
    result, recognized = coerce_bool(value, default)
    valid_type = value is None or isinstance(value, (bool, str))
    if recognized and valid_type:
        return result, None
    return None, error_response(
        code="validation_failed",
        message=f"`{field}` must be a boolean.",
        status=422,
        details={"field": field, "value": value},
    )


def _validate_mycomap_rerun_limit(body, result_type):
    """Validate optional MycoMap BLAST rerun limits for tool endpoints."""
    from app.services.mycomap_service import validate_mycomap_rerun_limit

    aliases = {
        "local": ("mycomap_local_limit", "mycomap_local_blast_limit", "local_limit"),
        "ncbi": ("mycomap_ncbi_limit", "mycomap_ncbi_blast_limit", "ncbi_limit"),
    }
    value = None
    field = aliases[result_type][0]
    for key in aliases.get(result_type, ()):
        if key in body:
            field = key
            value = body.get(key)
            break
    limit, message = validate_mycomap_rerun_limit(value, result_type)
    if not message:
        return limit, None
    return None, error_response(
        code="validation_failed",
        message=message,
        status=422,
        details={"field": field, "value": value},
    )


def _validate_categorical(field, value, allowed):
    """Return (clean_value, None) or (None, error_response). `allowed` is a set."""
    if value not in allowed:
        return None, error_response(
            code="validation_failed",
            message=(
                f"`{field}` must be one of {sorted(allowed)}; got "
                f"{value!r}. See /api/v1/openapi.json for the full schema."
            ),
            status=422,
            details={"field": field, "value": value, "allowed": sorted(allowed)},
        )
    return value, None


def _validate_clamped_int(field, value, *, default):
    """Validate-and-clamp; reject non-numeric values with a clear 422.

    If the value is missing (None) we substitute the default. If present
    but not coercible to int, we 422 instead of silently substituting --
    silent substitution hides typos like `"bootstrap": "many"`.
    """
    lo, hi = LIMITS[field]
    if value is None:
        return default, None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None, error_response(
            code="validation_failed",
            message=(
                f"`{field}` must be an integer between {lo:,} and {hi:,}. "
                f"Received {value!r} ({type(value).__name__})."
            ),
            status=422,
            details={"field": field, "value": value, "min": lo, "max": hi},
        )
    if n < lo or n > hi:
        return None, error_response(
            code="validation_failed",
            message=(
                f"`{field}` is {n:,}, outside the allowed range "
                f"{lo:,}..{hi:,}."
            ),
            status=422,
            details={"field": field, "value": n, "min": lo, "max": hi},
        )
    return n, None


def _validate_fraction(field, value, *, default):
    """Validate a finite decimal fraction against the configured limits."""
    lo, hi = LIMITS[field]
    if value is None:
        return default, None
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = math.nan
    if not math.isfinite(n) or n < lo or n > hi:
        return None, error_response(
            code="validation_failed",
            message=f"`{field}` must be a number between {lo} and {hi}.",
            status=422,
            details={"field": field, "value": value, "min": lo, "max": hi},
        )
    return n, None


def _validate_string(field, value, *, max_len, allow_empty=True):
    if value is None:
        if allow_empty:
            return "", None
        return None, error_response(
            code="validation_failed",
            message=f"`{field}` is required.",
            status=422,
            details={"field": field},
        )
    if not isinstance(value, str):
        return None, error_response(
            code="validation_failed",
            message=f"`{field}` must be a string; got {type(value).__name__}.",
            status=422,
            details={"field": field, "type": type(value).__name__},
        )
    if not allow_empty and not value.strip():
        return None, error_response(
            code="validation_failed",
            message=f"`{field}` must be a non-empty string.",
            status=422,
            details={"field": field},
        )
    if len(value) > max_len:
        return None, error_response(
            code="validation_failed",
            message=(
                f"`{field}` is {len(value):,} characters; the maximum is "
                f"{max_len:,}."
            ),
            status=422,
            details={"field": field, "length": len(value), "max_length": max_len},
        )
    return value, None


def _validate_accessions(value):
    """Accessions must be a list of short strings, capped at LIMITS['accessions_max']."""
    if value is None:
        return [], None
    if not isinstance(value, list):
        return None, error_response(
            code="validation_failed",
            message=(
                "`accessions` must be a JSON array of GenBank accession "
                "strings (e.g. [\"MK564475\", \"OL123456\"])."
            ),
            status=422,
            details={"field": "accessions", "type": type(value).__name__},
        )
    if len(value) > LIMITS["accessions_max"]:
        return None, error_response(
            code="validation_failed",
            message=(
                f"`accessions` has {len(value):,} entries; the maximum is "
                f"{LIMITS['accessions_max']:,}. Split your dataset across "
                f"multiple jobs."
            ),
            status=422,
            details={"field": "accessions", "count": len(value),
                     "max": LIMITS["accessions_max"]},
        )
    cleaned = []
    for i, a in enumerate(value):
        if not isinstance(a, str):
            return None, error_response(
                code="validation_failed",
                message=(
                    f"`accessions[{i}]` must be a string; got "
                    f"{type(a).__name__}."
                ),
                status=422,
                details={"field": "accessions", "index": i,
                         "type": type(a).__name__},
            )
        if len(a) > LIMITS["accession_str_max"]:
            return None, error_response(
                code="validation_failed",
                message=(
                    f"`accessions[{i}]` is {len(a):,} characters; max is "
                    f"{LIMITS['accession_str_max']:,}."
                ),
                status=422,
                details={"field": "accessions", "index": i, "length": len(a),
                         "max_length": LIMITS["accession_str_max"]},
            )
        cleaned.append(a)
    return cleaned, None


@bp.route('/jobs', methods=['POST'])
@require_api_token(scope='jobs:write')
@limiter.limit("30 per hour; 200 per day", key_func=api_token_key_func)
@idempotent
def create_job():
    """Create a new phylo job. Body: {input_type, sequence, accessions,
    alignment_method, trimming_method, tree_method, tree_model, bootstrap,
    mcmc_generations, mcmc_nruns, mcmc_nchains, mcmc_burnin_fraction,
    mcmc_stop_early, notes}.
    """
    data = request.get_json(silent=True)
    if data is None:
        return error_response(
            code="bad_request",
            message=(
                "Request body must be valid JSON with `Content-Type: "
                "application/json`. See /api/v1/openapi.json for the "
                "CreateJobRequest schema."
            ),
            status=400,
        )
    if not isinstance(data, dict):
        return error_response(
            code="bad_request",
            message="Request body must be a JSON object.",
            status=400,
        )

    # Allowlisted categorical params.
    tree_method, err = _validate_categorical(
        "tree_method", data.get("tree_method", DEFAULT_TREE_METHOD), VALID_TREE_METHODS)
    if err: return err
    aligner, err = _validate_categorical(
        "alignment_method", data.get("alignment_method", DEFAULT_ALIGNMENT_METHOD),
        VALID_ALIGNERS)
    if err: return err
    trimmer, err = _validate_categorical(
        "trimming_method", data.get("trimming_method", Config.DEFAULT_TRIMMING_METHOD), VALID_TRIMMERS)
    if err: return err
    trim_terminal_overhangs, err = _validate_bool(
        "trim_terminal_overhangs", data.get("trim_terminal_overhangs"), default=True)
    if err: return err
    fix_orientation, err = _validate_bool(
        "fix_orientation", data.get("fix_orientation"), default=True)
    if err: return err

    # Clamped integers.
    try:
        requested_bootstrap = data.get("bootstrap")
        if requested_bootstrap is None:
            requested_bootstrap = DEFAULT_BOOTSTRAP
        requested_bootstrap = validate_iqtree_ufboot_count(
            tree_method, requested_bootstrap
        )
    except ValueError as exc:
        return error_response(
            code="validation_failed", message=str(exc), status=422,
            details={"field": "bootstrap", "value": data.get("bootstrap")},
        )
    bootstrap, err = _validate_clamped_int(
        "bootstrap", requested_bootstrap, default=DEFAULT_BOOTSTRAP)
    if err: return err
    # IQ-TREE SH-aLRT replicates; defaults to Config.DEFAULT_IQTREE_ALRT so API
    # callers get the same UFBoot + SH-aLRT pairing as the web form. 0 disables it.
    alrt_replicates, err = _validate_clamped_int(
        "alrt_replicates", data.get("alrt_replicates"),
        default=Config.DEFAULT_IQTREE_ALRT)
    if err: return err
    # With mcmc_stop_early on (the default), this is the maximum: MrBayes ends
    # the run early once the independent runs agree on split frequencies.
    mcmc_generations, err = _validate_clamped_int(
        "mcmc_generations", data.get("mcmc_generations"),
        default=Config.DEFAULT_MCMC_GENERATIONS)
    if err: return err
    mcmc_nruns, err = _validate_clamped_int(
        "mcmc_nruns", data.get("mcmc_nruns"), default=Config.DEFAULT_MCMC_NRNS)
    if err: return err
    mcmc_nchains, err = _validate_clamped_int(
        "mcmc_nchains", data.get("mcmc_nchains"), default=Config.DEFAULT_MCMC_CHAINS)
    if err: return err
    mcmc_burnin_fraction, err = _validate_fraction(
        "mcmc_burnin_fraction", data.get("mcmc_burnin_fraction"),
        default=Config.DEFAULT_MCMC_BURNIN_FRACTION)
    if err: return err
    mcmc_stop_early, err = _validate_bool(
        "mcmc_stop_early", data.get("mcmc_stop_early"),
        default=Config.DEFAULT_MCMC_STOP_EARLY)
    if err: return err
    # DEFAULT_MCMC_GENERATIONS is a ceiling, and the stop rule that is supposed
    # to end the run well short of it needs two independent runs. A caller who
    # asks for one run and names no generation count would otherwise inherit the
    # ceiling as a full-length run on a worker that runs one job at a time.
    # Only the unrequested default is reduced; an explicit count is honoured.
    if (tree_method == "mrbayes"
            and data.get("mcmc_generations") is None
            and not (mcmc_stop_early and mcmc_nruns > 1)):
        mcmc_generations = Config.DEFAULT_MCMC_GENERATIONS_FIXED_RUN

    # Strings.
    notes, err = _validate_string("notes", data.get("notes"),
                                  max_len=LIMITS["notes_max"])
    if err: return err
    # IQ-TREE ships ModelFinder, so an omitted model means "let ModelFinder pick"
    # rather than a fixed GTR+G. Every other method keeps the fixed default.
    default_tree_model = (
        Config.DEFAULT_IQTREE_MODEL if tree_method == "iqtree" else Config.DEFAULT_ML_MODEL
    )
    tree_model, err = _validate_string(
        "tree_model", data.get("tree_model", default_tree_model),
        max_len=LIMITS["tree_model_max"], allow_empty=False)
    if err: return err

    # Sequence + accessions.
    sequence_text, err = _validate_string(
        "sequence", data.get("sequence"),
        max_len=LIMITS["sequence_max_bytes"], allow_empty=True)
    if err: return err
    accessions, err = _validate_accessions(data.get("accessions"))
    if err: return err
    if not sequence_text and not accessions:
        return error_response(
            code="validation_failed",
            message=(
                "Provide either `sequence` (FASTA text) or `accessions` "
                "(list of GenBank IDs). Both cannot be empty."
            ),
            status=422,
            details={"fields": ["sequence", "accessions"]},
        )

    # `alignment_options` is a free-form dict consumed by the worker; we cap
    # only its shape and a rough byte size to keep it from being abused as
    # an exfiltration channel into stored job params.
    alignment_options = data.get("alignment_options")
    if alignment_options is None:
        alignment_options = {}
    if not isinstance(alignment_options, dict):
        return error_response(
            code="validation_failed",
            message="`alignment_options` must be a JSON object.",
            status=422,
            details={"field": "alignment_options",
                     "type": type(alignment_options).__name__},
        )
    if len(json.dumps(alignment_options)) > 4096:
        return error_response(
            code="validation_failed",
            message=(
                "`alignment_options` serializes to more than 4 KB; reduce "
                "the number of options or use shorter values."
            ),
            status=422,
            details={"field": "alignment_options", "max_serialized_bytes": 4096},
        )

    # Normalize input_type. The public JSON API only supports inline data:
    # FASTA text in `sequence`, or accessions in `accessions`. Server-side
    # FASTA uploads (the worker's `fasta_upload` mode, which expects a file
    # already staged on disk) are not reachable via this endpoint.
    raw = data.get("input_type")
    if raw is not None and not isinstance(raw, str):
        return error_response(
            code="validation_failed",
            message="`input_type` must be a string.",
            status=422,
            details={"field": "input_type", "type": type(raw).__name__},
        )
    raw_input_type = (raw or "").strip().lower() or None
    if raw_input_type == "fasta_upload" or (
            raw_input_type == "fasta" and not sequence_text):
        return error_response(
            code="validation_failed",
            message=(
                "The public API does not support server-side FASTA file "
                "uploads. Send FASTA text in the `sequence` field with "
                "`input_type=\"pasted_sequence\"`, or use `accession_list` "
                "with GenBank IDs in `accessions`."
            ),
            status=422,
            details={"field": "input_type", "value": raw_input_type},
        )
    if raw_input_type is not None and raw_input_type not in PUBLIC_INPUT_TYPE_ALIASES:
        return error_response(
            code="validation_failed",
            message=(
                f"`input_type` must be one of "
                f"{sorted(set(PUBLIC_INPUT_TYPE_ALIASES.values()))}; got "
                f"{raw_input_type!r}. Use `pasted_sequence` with FASTA text in "
                f"`sequence`, or `accession_list` with GenBank IDs in "
                f"`accessions`."
            ),
            status=422,
            details={
                "field": "input_type",
                "value": raw_input_type,
                "allowed": sorted(PUBLIC_INPUT_TYPE_ALIASES),
            },
        )
    if raw_input_type is not None:
        input_type = PUBLIC_INPUT_TYPE_ALIASES[raw_input_type]
    elif sequence_text:
        input_type = "pasted_sequence"
    else:
        input_type = "accession_list"

    job_params = {
        "input_type":        input_type[:64],
        "notes":             notes,
        "sequence":          sequence_text,
        "accessions":        accessions,
        "alignment_method":  aligner,
        "trimming_method":   trimmer,
        "trim_terminal_overhangs": trim_terminal_overhangs,
        "fix_orientation":   fix_orientation,
        "alignment_options": alignment_options,
        "tree_method":       tree_method,
        "tree_model":        tree_model,
        "bootstrap":         bootstrap,
        "alrt_replicates":   alrt_replicates,
        "mcmc_generations":  mcmc_generations,
        "mcmc_nruns":        mcmc_nruns,
        "mcmc_nchains":      mcmc_nchains,
        "mcmc_burnin_fraction": mcmc_burnin_fraction,
        "mcmc_stop_early":   mcmc_stop_early,
    }

    # Mint the id ourselves and commit the Job row *before* the work becomes
    # runnable. Enqueueing first meant a free worker could pick the job up in
    # the gap before the row existed; the pipeline tolerates a missing row, so
    # ownership, status and metrics bookkeeping were simply skipped for that
    # job -- it ran, but the submitter could not see or own it.
    job_id = str(uuid.uuid4())
    job_record = Job(
        id=job_id,
        user_id=g.api_user.id,
        status="queued",
        job_dir=str(Config.JOB_DIR / job_id),
        input_type=job_params["input_type"],
        metrics={
            "tree_method": job_params["tree_method"],
            "notes": job_params["notes"],
            "alignment_method": job_params["alignment_method"],
            "trimming_method": job_params["trimming_method"],
            "trim_terminal_overhangs": job_params["trim_terminal_overhangs"],
            "fix_orientation": job_params["fix_orientation"],
            "via": "api_v1",
            "api_token_id": g.api_token.id,
        },
    )
    try:
        db.session.add(job_record)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return server_error(e, where="create_job")

    try:
        enqueue_job(job_params, job_id=job_id)
    except Exception as e:
        # The row is committed and is the only record that this submission was
        # ever accepted, so it is marked failed rather than deleted: erasing it
        # would leave the caller with a 500 and no trace of what happened.
        logger.exception(
            "event=api_v1.enqueue_failed job=%s could not be queued after its "
            "DB row was committed", job_id,
        )
        try:
            job_record.status = "failed"
            metrics = dict(job_record.metrics or {})
            metrics["error"] = (
                "This job could not be added to the processing queue and was "
                "never started. Please submit it again."
            )
            metrics["enqueue_error"] = type(e).__name__
            metrics["failed_at"] = datetime.utcnow().isoformat()
            job_record.metrics = metrics
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception(
                "event=api_v1.enqueue_failure_unrecorded job=%s could not be "
                "marked failed after a queueing failure", job_id,
            )
        return server_error(e, where="create_job")

    return ok(serialize_job(job_record), status=202)


def parse_utc_query_timestamp(raw):
    """Parse an ISO-8601 query timestamp into the form the DB column holds.

    `Job.created_at` is a naive `DateTime` written from `datetime.utcnow()`, so
    every stored value is UTC with no tzinfo. An offset-aware filter compared
    against it raises `TypeError: can't compare offset-naive and offset-aware
    datetimes` on SQLite and, worse, is compared *literally* by PostgreSQL --
    `?since=2026-08-01T00:00:00-07:00` silently filtered on midnight rather
    than on 07:00 UTC.

    So an offset (including a trailing `Z`) is honoured and converted to UTC,
    then stripped. A timestamp with no offset is taken to already be UTC, which
    matches what the API returns in `created_at`; it is never re-interpreted as
    server local time.

    Raises ValueError on anything `datetime.fromisoformat` will not accept.
    """
    parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


@bp.route('/jobs', methods=['GET'])
@require_api_token(scope='jobs:read')
@limiter.limit("600 per minute", key_func=api_token_key_func)
def list_jobs():
    """List the caller's jobs. Filters: ?status=, ?since=, ?until=.
    Pagination: ?page=, ?per_page= (max 100).

    `since`/`until` accept offset-aware or naive ISO-8601; see
    `parse_utc_query_timestamp` for how each is interpreted."""
    q = Job.query.filter_by(user_id=g.api_user.id)

    status = request.args.get("status")
    if status:
        q = q.filter(Job.status == status)

    since = request.args.get("since")
    if since:
        try:
            q = q.filter(Job.created_at >= parse_utc_query_timestamp(since))
        except ValueError:
            return error_response(
                code="bad_request",
                message="`since` must be ISO-8601 (naive values are read as UTC).",
                status=400)
    until = request.args.get("until")
    if until:
        try:
            q = q.filter(Job.created_at < parse_utc_query_timestamp(until))
        except ValueError:
            return error_response(
                code="bad_request",
                message="`until` must be ISO-8601 (naive values are read as UTC).",
                status=400)

    q = q.order_by(desc(Job.created_at))
    items, meta = paginate_query(q, default_per_page=50, max_per_page=100)
    return ok([serialize_job(j) for j in items], meta=meta)


@bp.route('/jobs/<job_id>', methods=['GET'])
@require_api_token(scope='jobs:read')
@limiter.limit("600 per minute", key_func=api_token_key_func)
def get_job(job_id):
    job = get_owned_job_or_404(job_id)
    if not job:
        return error_response(code="not_found", message="Job not found.", status=404)
    return ok(serialize_job(job))


# How long a DELETE waits for a running work-horse to actually stop after the
# stop command is sent, before giving up and telling the caller to retry.
DELETE_STOP_WAIT_SECONDS = 3.0
DELETE_STOP_POLL_SECONDS = 0.2

# Terminal RQ states: nothing will execute the job from here.
_RQ_TERMINAL_STATUSES = {"finished", "failed", "stopped", "canceled"}


def _release_rq_job(job_id):
    """Make sure RQ will not run (or keep running) this job.

    Returns (released, detail). `released` is True only when we have positive
    evidence that no work-horse can still be writing into the job directory:
    either RQ has no record of the job, or it is in a terminal state, or we
    cancelled it out of the queue before it started.

    DB, RQ and the filesystem are three separate systems, so this deliberately
    reports failure rather than guessing -- the caller then declines to destroy
    anything instead of deleting a directory out from under a live process.
    """
    import time

    from rq.exceptions import NoSuchJobError
    from rq.job import Job as RQJob

    try:
        from app.workers.queue import get_redis_connection
        conn = get_redis_connection()
        try:
            rq_job = RQJob.fetch(job_id, connection=conn)
        except NoSuchJobError:
            return True, "no RQ record"
        status = rq_job.get_status(refresh=True)
        if status in _RQ_TERMINAL_STATUSES:
            return True, f"already {status}"
        if status != "started":
            # queued / deferred / scheduled / created: cancelling removes it
            # from the queue, so it can never begin.
            rq_job.cancel()
            terminal = rq_job.get_status(refresh=True)
            if terminal in _RQ_TERMINAL_STATUSES:
                return True, f"cancelled from {status}"
            if terminal != "started":
                return False, f"RQ status became {terminal} during cancellation"
            # A worker won the dequeue race. Fall through to the same stop and
            # positive-terminal-evidence path as a job that was already started.

        # Started: ask the worker to kill the horse, then wait for RQ to say so.
        from rq.command import send_stop_job_command
        send_stop_job_command(conn, job_id)
        deadline = time.monotonic() + DELETE_STOP_WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(DELETE_STOP_POLL_SECONDS)
            if rq_job.get_status(refresh=True) in _RQ_TERMINAL_STATUSES:
                return True, "stopped"
        return False, "still running after a stop request"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


@bp.route('/jobs/<job_id>', methods=['DELETE'])
@require_api_token(scope='jobs:write')
@limiter.limit("60 per hour", key_func=api_token_key_func)
def delete_job(job_id):
    """Delete a job's DB record and its on-disk directory.

    Ordering matters. This used to `shutil.rmtree` the job directory and only
    then commit the row deletion, so a failed commit left a live job record
    whose artifacts were already gone for good -- and a queued or running
    work-horse was free to keep writing into a directory the user had just
    destroyed. The directory is now moved aside first (cheap, atomic and
    reversible), the row deletion is committed, and only then is the moved copy
    removed. If that last step fails the logical deletion still stands and the
    bytes are left behind as evidence rather than half-removed in place.
    """
    import shutil
    import time

    job = get_owned_job_or_404(job_id)
    if not job:
        return error_response(code="not_found", message="Job not found.", status=404)
    if job.status == "deleting":
        return error_response(
            code="conflict",
            message="This job is already being deleted. Try again shortly.",
            status=409,
            details={"job_id": job_id, "status": job.status},
        )

    original_status = job.status

    # Publish a durable guard before relying on queue cancellation. A worker
    # dequeued in the status-check/cancel window checks this state before its
    # first filesystem write and exits. Finished jobs need no guard or Redis.
    if job.status in ("queued", "running"):
        try:
            job.status = "deleting"
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            return server_error(exc, where="delete_job.guard")

        released, detail = _release_rq_job(job_id)
        if not released:
            try:
                job.status = original_status
                db.session.commit()
            except Exception as exc:
                db.session.rollback()
                logger.exception(
                    "event=api_v1.delete_guard_restore_failed job=%s", job_id,
                )
                return server_error(exc, where="delete_job.restore_guard")
            logger.warning(
                "event=api_v1.delete_blocked job=%s reason=%s", job_id, detail,
            )
            return error_response(
                code="conflict",
                message=(
                    "This job is still running and could not be stopped, so it "
                    "was not deleted -- removing its files now could leave the "
                    "running step writing into a half-deleted job. Nothing has "
                    "been changed. Try again in a few seconds."
                ),
                status=409,
                details={"job_id": job_id, "status": job.status, "reason": detail},
            )

    # Move the directory aside rather than destroying it in place.
    source = None
    staged = None
    if job.job_dir:
        candidate = Path(job.job_dir)
        # Guard against touching anything outside JOB_DIR.
        try:
            inside = candidate.resolve().is_relative_to(Config.JOB_DIR.resolve())
        except OSError:
            inside = False
        if candidate.is_dir() and inside:
            trash_root = Config.JOB_DIR / ".trash"
            try:
                trash_root.mkdir(parents=True, exist_ok=True)
                staged = trash_root / f"{job_id}.{int(time.time() * 1000)}"
                candidate.rename(staged)
                source = candidate
            except OSError as exc:
                staged = None
                logger.exception(
                    "event=api_v1.delete_stage_failed job=%s", job_id,
                )
                if original_status in ("queued", "running"):
                    try:
                        job.status = original_status
                        db.session.commit()
                    except Exception as restore_exc:
                        db.session.rollback()
                        return server_error(
                            restore_exc, where="delete_job.restore_guard"
                        )
                return error_response(
                    code="conflict",
                    message=(
                        "This job's files could not be moved aside for deletion, "
                        "so nothing was deleted. Please try again."
                    ),
                    status=409,
                    details={"job_id": job_id, "reason": type(exc).__name__},
                )

    try:
        db.session.delete(job)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        # The row survives, so its artifacts must too: put them back.
        if staged is not None and source is not None:
            try:
                staged.rename(source)
            except OSError:
                logger.exception(
                    "event=api_v1.delete_restore_failed job=%s staged=%s",
                    job_id, staged,
                )
        if original_status in ("queued", "running"):
            try:
                job.status = original_status
                db.session.commit()
            except Exception:
                db.session.rollback()
                logger.exception(
                    "event=api_v1.delete_guard_restore_failed job=%s", job_id,
                )
        return server_error(e, where="delete_job")

    # Logical deletion is committed. Reclaiming the bytes is now best effort;
    # a leftover under var/jobs/.trash is visible evidence, not lost data.
    if staged is not None:
        failure = None
        try:
            shutil.rmtree(staged)
        except Exception as exc:
            failure = type(exc).__name__
        if failure or staged.exists():
            from app.services.log_context import log_degradation
            log_degradation(
                logger, "job_trash_cleanup_failed",
                "Deleted job files could not be removed from var/jobs/.trash",
                job_id=job_id, staged=str(staged),
                error=failure or "still_present",
            )

    return ok({"deleted": True, "id": job_id})


@bp.route('/jobs/<job_id>/recompute', methods=['POST'])
@require_api_token(scope='jobs:write')
@limiter.limit("5 per hour", key_func=api_token_key_func)
@idempotent
def recompute_job(job_id):
    job = get_owned_job_or_404(job_id)
    if not job:
        return error_response(code="not_found", message="Job not found.", status=404)
    job_dir = Config.JOB_DIR / job_id
    try:
        # Start from stored params and merge in *only* allowlisted overrides
        # from the request body. Unknown keys are rejected so the caller
        # gets a clear error rather than a silently ignored override.
        params_path = job_dir / "input_info.json"
        params = {}
        if params_path.exists():
            with open(params_path, "r") as f:
                params = json.load(f)
        body = request.get_json(silent=True)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return error_response(
                code="bad_request",
                message="Request body must be a JSON object of overrides.",
                status=400,
            )

        unknown = sorted(k for k in body.keys() if k not in RECOMPUTE_ALLOWED_FIELDS)
        if unknown:
            return error_response(
                code="validation_failed",
                message=(
                    "The recompute endpoint only accepts a fixed set of "
                    "parameter overrides on the same input data. The "
                    "following field(s) are not allowed here: "
                    f"{unknown}. Allowed fields: "
                    f"{sorted(RECOMPUTE_ALLOWED_FIELDS)}. To submit different "
                    "input data, create a new job with POST /jobs."
                ),
                status=422,
                details={"unknown_fields": unknown,
                         "allowed_fields": sorted(RECOMPUTE_ALLOWED_FIELDS)},
            )

        # Validate each override using the same helpers as POST /jobs. Only
        # fields actually present in the body are touched; everything else
        # falls back to the stored value.
        overrides = {}
        if "tree_method" in body:
            v, err = _validate_categorical(
                "tree_method", body["tree_method"], VALID_TREE_METHODS)
            if err: return err
            overrides["tree_method"] = v
        if "alignment_method" in body:
            v, err = _validate_categorical(
                "alignment_method", body["alignment_method"], VALID_ALIGNERS)
            if err: return err
            overrides["alignment_method"] = v
        if "trimming_method" in body:
            v, err = _validate_categorical(
                "trimming_method", body["trimming_method"], VALID_TRIMMERS)
            if err: return err
            overrides["trimming_method"] = v
        if "trim_terminal_overhangs" in body:
            v, err = _validate_bool("trim_terminal_overhangs", body["trim_terminal_overhangs"])
            if err: return err
            overrides["trim_terminal_overhangs"] = v
        if "fix_orientation" in body:
            v, err = _validate_bool("fix_orientation", body["fix_orientation"])
            if err: return err
            overrides["fix_orientation"] = v
        for field, default in (("alrt_replicates", Config.DEFAULT_IQTREE_ALRT),
                               ("mcmc_generations", Config.DEFAULT_MCMC_GENERATIONS),
                               ("mcmc_nruns", Config.DEFAULT_MCMC_NRNS),
                               ("mcmc_nchains", Config.DEFAULT_MCMC_CHAINS)):
            if field in body:
                v, err = _validate_clamped_int(field, body[field], default=default)
                if err: return err
                overrides[field] = v
        if "bootstrap" in body:
            # The effective tree method is resolved after all overrides merge;
            # preserve the raw value until the IQ-TREE-specific validator sees
            # it so a fractional count cannot be truncated by int().
            overrides["bootstrap"] = body["bootstrap"]
        if "mcmc_burnin_fraction" in body:
            v, err = _validate_fraction(
                "mcmc_burnin_fraction", body["mcmc_burnin_fraction"],
                default=Config.DEFAULT_MCMC_BURNIN_FRACTION
            )
            if err: return err
            overrides["mcmc_burnin_fraction"] = v
        if "mcmc_stop_early" in body:
            v, err = _validate_bool(
                "mcmc_stop_early", body["mcmc_stop_early"],
                default=Config.DEFAULT_MCMC_STOP_EARLY)
            if err: return err
            overrides["mcmc_stop_early"] = v
        if "tree_model" in body:
            v, err = _validate_string(
                "tree_model", body["tree_model"],
                max_len=LIMITS["tree_model_max"], allow_empty=False)
            if err: return err
            overrides["tree_model"] = v
        if "outgroup" in body:
            v, err = _validate_string(
                "outgroup", body["outgroup"],
                max_len=LIMITS["outgroup_max"], allow_empty=True)
            if err: return err
            overrides["outgroup"] = v
        if "notes" in body:
            v, err = _validate_string(
                "notes", body["notes"], max_len=LIMITS["notes_max"])
            if err: return err
            overrides["notes"] = v

        params.update(overrides)
        # Same split as the web recompute route: strict for a value the caller
        # supplied (or for an IQ-TREE configuration newly requested through a
        # tree_method override), lenient for one inherited from a job that
        # predates the -B >= 1000 rule.
        caller_chose_bootstrap = (
            "bootstrap" in overrides or "tree_method" in overrides
        )
        try:
            if caller_chose_bootstrap:
                requested_bootstrap = validate_iqtree_ufboot_count(
                    params.get("tree_method"), params.get("bootstrap", DEFAULT_BOOTSTRAP)
                )
            else:
                requested_bootstrap = normalize_inherited_iqtree_ufboot_count(
                    params.get("tree_method"), params.get("bootstrap", DEFAULT_BOOTSTRAP)
                )
        except ValueError as exc:
            return error_response(
                code="validation_failed", message=str(exc), status=422,
                details={"field": "bootstrap", "value": params.get("bootstrap")},
            )
        bootstrap, err = _validate_clamped_int(
            "bootstrap", requested_bootstrap, default=DEFAULT_BOOTSTRAP
        )
        if err: return err
        params["bootstrap"] = bootstrap
        # Always async for the public API. At most one recompute is active per
        # job, so a request that carries overrides while another generation is
        # already running cannot be answered as an idempotent duplicate: the
        # running task captured its params when it started, and reporting 202
        # here would promise settings the resulting tree was never built with.
        rq_job_id, created = enqueue_recompute_job(
            job_id, params, return_created=True,
        )
        if not created and overrides:
            return error_response(
                code="recompute_in_progress",
                message=(
                    "Another recompute is already in progress for this job and "
                    "will not use the supplied settings. Wait for it to finish, "
                    "then retry."
                ),
                status=409,
                details={"rq_job_id": rq_job_id,
                         "ignored_fields": sorted(overrides)},
            )
        if created:
            job.status = "queued"
            metrics = job.metrics or {}
            metrics["recompute_requested_at"] = datetime.utcnow().isoformat()
            job.metrics = metrics
            db.session.commit()
        return ok({
            "id": job_id,
            "status": "queued" if created else "already_queued",
            "rq_job_id": rq_job_id,
            "links": {
                "self":   url_for("api_v1.get_job", job_id=job_id),
                "events": url_for("api_v1.job_events", job_id=job_id),
            },
        }, status=202)
    except Exception as e:
        db.session.rollback()
        return server_error(e, where="recompute_job")


# ============================================================================
# Job files & logs
# ============================================================================

@bp.route('/jobs/<job_id>/files', methods=['GET'])
@require_api_token(scope='jobs:read')
@limiter.limit("600 per minute", key_func=api_token_key_func)
def list_job_files(job_id):
    job = get_owned_job_or_404(job_id)
    if not job:
        return error_response(code="not_found", message="Job not found.", status=404)
    artifacts = list_available_artifacts(job_id)
    for a in artifacts:
        a["url"] = url_for("api_v1.download_job_file", job_id=job_id, name=a["name"])
    return ok(artifacts)


_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@bp.route('/jobs/<job_id>/files/<name>', methods=['GET'])
@require_api_token(scope='jobs:read')
@limiter.limit("600 per minute", key_func=api_token_key_func)
def download_job_file(job_id, name):
    job = get_owned_job_or_404(job_id)
    if not job:
        return error_response(code="not_found", message="Job not found.", status=404)
    if not _FILE_NAME_RE.match(name) or name not in DOWNLOADABLE_ARTIFACTS:
        return error_response(
            code="not_found",
            message=f"Unknown artifact. Valid names: {sorted(DOWNLOADABLE_ARTIFACTS)}",
            status=404,
        )
    p = artifact_path(job_id, name)
    job_dir = Config.JOB_DIR / job_id
    if p is None or not validate_safe_file_path(p, job_dir):
        return error_response(code="not_found", message="File not available yet.", status=404)
    if p.suffix == ".gz":
        # Stored gzipped at rest; the client asked for `name`, so hand back the
        # decompressed bytes rather than an archive under a misleading name.
        from io import BytesIO

        return send_file(
            BytesIO(read_artifact_bytes(p)),
            as_attachment=True,
            download_name=name,
            mimetype=_guess_mime(name),
        )
    return send_file(p, as_attachment=True, download_name=name)


@bp.route('/jobs/<job_id>/logs/<log_name>', methods=['GET'])
@require_api_token(scope='jobs:read')
@limiter.limit("600 per minute", key_func=api_token_key_func)
def get_job_log(job_id, log_name):
    job = get_owned_job_or_404(job_id)
    if not job:
        return error_response(code="not_found", message="Job not found.", status=404)
    if log_name not in LOG_NAMES:
        return error_response(
            code="not_found",
            message=f"Unknown log. Valid: {sorted(LOG_NAMES)}",
            status=404,
        )
    job_dir = Config.JOB_DIR / job_id
    log_path = job_dir / "logs" / LOG_NAMES[log_name]
    if not validate_safe_file_path(log_path, job_dir / "logs"):
        return error_response(code="not_found", message="Log not available yet.", status=404)
    return send_file(log_path, mimetype="text/plain", as_attachment=False,
                     download_name=LOG_NAMES[log_name])


# ============================================================================
# Job events (SSE)
# ============================================================================

SSE_MAX_CONCURRENT_PER_TOKEN = 5          # max simultaneous streams per token
SSE_HEARTBEAT_SECONDS = 15                # ping interval


def _sse_limits():
    """Lifecycle limits for a v1 stream, taken from the same Config values the
    internal /api/job/<id>/events stream uses.

    This endpoint used to carry its own hard-coded 30-minute policy, which was
    both stricter than the internal stream (cutting a live RAxML run that was
    streaming progress normally) and looser where it mattered: with no idle
    limit and no terminal linger, connecting to an already-finished job entered
    a loop with no reachable exit, because the DB poll that ends the stream is
    skipped for terminal jobs. Such a stream held a Gunicorn request slot until
    the hard cap.
    """
    return (
        int(getattr(Config, "SSE_MAX_STREAM_SECONDS", 21600) or 21600),
        int(getattr(Config, "SSE_MAX_IDLE_SECONDS", 600) or 0),
        int(getattr(Config, "SSE_TERMINAL_LINGER_SECONDS", 10) or 0),
    )


@bp.route('/jobs/<job_id>/events', methods=['GET'])
@require_api_token(scope='jobs:read')
@limiter.limit("30 per minute", key_func=api_token_key_func)
def job_events(job_id):
    """SSE stream of pipeline progress events. Mirrors the internal /api/job/<id>/events
    stream but enforces strict ownership, a per-token concurrent-connection cap,
    a max stream duration, and mid-stream revocation checks."""
    import time
    import redis as _redis
    from app.api.routes import _build_snapshot  # reuse existing snapshot builder

    job = get_owned_job_or_404(job_id)
    if not job:
        return error_response(code="not_found", message="Job not found.", status=404)

    # Per-token concurrent connection cap. We use a Redis counter that's
    # INCR'd on connect and DECR'd in the generator's finally block. A short
    # TTL on the counter prevents permanently-stuck counters if a worker
    # is killed before the finally runs.
    max_stream_seconds, max_idle_seconds, terminal_linger_seconds = _sse_limits()
    token_id = g.api_token.id
    conn_key = f"sse:conn:{token_id}"
    try:
        r_gate = _redis.from_url(Config.REDIS_URL)
        current = r_gate.incr(conn_key)
        r_gate.expire(conn_key, max_stream_seconds + 60)
    except Exception as exc:
        # If Redis is unreachable we fall back to enforcing only the per-token
        # rate limit -- which is itself sufficient to bound abuse.
        current = 1
        r_gate = None
        from app.services.log_context import log_degradation_rate_limited
        log_degradation_rate_limited(
            logger, "sse_connection_gate_failed",
            "API SSE stream continued without the Redis connection gate",
            exception=type(exc).__name__,
        )

    if current > SSE_MAX_CONCURRENT_PER_TOKEN:
        if r_gate is not None:
            try:
                r_gate.decr(conn_key)
            except Exception:
                pass
        return error_response(
            code="too_many_streams",
            message=(
                f"This API token already has {SSE_MAX_CONCURRENT_PER_TOKEN} "
                f"open event streams (the per-token maximum). Close an "
                f"existing stream before opening a new one, or mint a "
                f"separate token if your workload genuinely needs more "
                f"concurrent streams."
            ),
            status=429,
            details={"max_concurrent": SSE_MAX_CONCURRENT_PER_TOKEN,
                     "current": current},
        )

    # Snapshot the bits of state we'll need inside the generator. Once the
    # generator yields, Flask's request context is gone, so we can't touch
    # `g`, `request`, etc. from within `generate()`.
    api_token_id = token_id

    def generate():
        from app.services.log_context import log_degradation_rate_limited
        from app.services import sse_registry
        from app.models import ApiToken as _ApiToken
        started = time.monotonic()
        close_reason = "unexpected_exception"
        pubsub = None
        stream_token = None
        registry_conn = None
        try:
            r = _redis.from_url(Config.REDIS_URL)
            pubsub = r.pubsub()
            pubsub.subscribe(f"job:{job_id}:events")
            registry_conn = r
            # Count this stream in the same site-wide census the internal
            # stream uses; both hold the same finite pool of request slots.
            stream_token, _open_count = sse_registry.open_stream(r, job_id)
            snapshot = _build_snapshot(job_id)
            yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
            job_status = snapshot["job"]["status"]
            # A job that finished before the client connected will never publish
            # a terminal event, and the DB poll below is skipped for terminal
            # jobs -- so this loop had no reachable exit and pinned a request
            # slot until the hard cap. Linger only long enough for stragglers.
            already_terminal = job_status in ('completed', 'failed')
            terminal_deadline = (
                time.monotonic() + terminal_linger_seconds
                if already_terminal else None
            )
            last_ping = time.monotonic()
            last_db_poll = 0.0
            last_token_check = time.monotonic()
            last_activity = started
            while True:
                # Hard duration cap. Clients should reconnect.
                if time.monotonic() - started > max_stream_seconds:
                    yield (
                        "event: timeout\n"
                        "data: {\"reason\": \"max_duration_reached\", "
                        f"\"max_seconds\": {max_stream_seconds}}}\n\n"
                    )
                    close_reason = "lifetime_cap"
                    break

                # Idle cap. Age alone is a bad proxy for an abandoned stream --
                # it cut genuinely long jobs. A live run keeps publishing log
                # lines, which resets this; only silent streams reach it.
                if (
                    max_idle_seconds > 0
                    and not already_terminal
                    and job_status not in ('completed', 'failed')
                    and time.monotonic() - last_activity >= max_idle_seconds
                ):
                    yield (
                        "event: timeout\n"
                        "data: {\"reason\": \"idle\", "
                        f"\"max_seconds\": {max_idle_seconds}}}\n\n"
                    )
                    close_reason = "idle_reconnect"
                    break

                if terminal_deadline is not None and time.monotonic() >= terminal_deadline:
                    # Nothing more is coming for a job that was already finished
                    # when this stream opened; release the slot.
                    close_reason = "terminal_completion"
                    break

                message = pubsub.get_message(timeout=0.1)
                if message and message['type'] == 'message':
                    data = message['data']
                    if isinstance(data, bytes):
                        data = data.decode('utf-8')
                    yield f"data: {data}\n\n"
                    # Real job output: this stream is following live work.
                    last_activity = time.monotonic()
                    try:
                        ev = json.loads(data)
                        if ev.get('type') == 'job_state' and ev.get('status') in ('completed', 'failed'):
                            time.sleep(0.5)
                            close_reason = "terminal_completion"
                            break
                    except json.JSONDecodeError:
                        pass
                now = time.monotonic()
                if now - last_ping >= SSE_HEARTBEAT_SECONDS:
                    yield "event: ping\ndata: {}\n\n"
                    last_ping = now
                    # Re-check token validity on each heartbeat so a revoked
                    # token can't keep streaming indefinitely.
                    if now - last_token_check >= SSE_HEARTBEAT_SECONDS:
                        last_token_check = now
                        try:
                            tok = _ApiToken.query.get(api_token_id)
                            if tok is None or not tok.is_active:
                                yield (
                                    "event: revoked\n"
                                    "data: {\"reason\": \"token_revoked\"}\n\n"
                                )
                                close_reason = "token_revoked"
                                break
                        except Exception as exc:
                            db.session.rollback()
                            log_degradation_rate_limited(
                                logger, "sse_token_check_failed",
                                "API SSE token recheck failed; stream remained open",
                                exception=type(exc).__name__,
                            )
                if job_status not in ('completed', 'failed'):
                    if now - last_db_poll >= 1.0:
                        last_db_poll = now
                        db.session.expire_all()
                        db_job_check = Job.query.get(job_id)
                        if db_job_check and db_job_check.status in ('completed', 'failed'):
                            job_status = db_job_check.status
                            # Give final metadata a moment to settle, then send a
                            # terminal snapshot in case the Redis completion
                            # event was missed, so a client closing here is not
                            # left showing the last non-terminal step.
                            time.sleep(1)
                            yield (
                                "event: snapshot\n"
                                f"data: {json.dumps(_build_snapshot(job_id))}\n\n"
                            )
                            close_reason = "terminal_completion"
                            break
                time.sleep(0.05)
        except GeneratorExit:
            close_reason = "client_disconnect"
            raise
        except _redis.RedisError as exc:
            close_reason = "redis_failure"
            log_degradation_rate_limited(logger, "sse_redis_failure", "API SSE stream lost Redis", exception=type(exc).__name__)
        except Exception:
            close_reason = "unexpected_exception"
            logger.exception("event=sse.generator_failed API SSE generator failed")
        finally:
            try:
                if pubsub is not None:
                    pubsub.unsubscribe()
                    pubsub.close()
            except Exception as exc:
                log_degradation_rate_limited(logger, "sse_cleanup_failed", "API SSE cleanup failed", exception=type(exc).__name__)
            if r_gate is not None:
                try:
                    r_gate.decr(conn_key)
                except Exception as exc:
                    log_degradation_rate_limited(logger, "sse_gate_cleanup_failed", "API SSE connection gate cleanup failed", exception=type(exc).__name__)
            remaining = (
                sse_registry.close_stream(registry_conn, stream_token)
                if stream_token is not None else 0
            )
            logger.info(
                "event=sse.closed API SSE stream closed reason=%s "
                "duration_seconds=%.3f open_streams=%s",
                close_reason, time.monotonic() - started, remaining,
            )

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


# ============================================================================
# Tree mutations
# ============================================================================

# Tree mutation input bounds. Real fungal trees in this codebase rarely have
# more than a few thousand tips; 10 000 is generous headroom. Per-name length
# is bounded both to prevent memory abuse and to keep Newick/Nexus files
# downstream a reasonable size.
TREE_MUTATION_LIMITS = {
    "max_tips":      10_000,
    "max_name_len":  MAX_TREE_TIP_NAME_LENGTH,
}

# Characters that would corrupt Newick syntax if written verbatim as a tip
# name. Reject these in *new* tip names (rename target, prune target list)
# so a malformed name can't break tree exports.
_NEWICK_UNSAFE = set(NEWICK_UNSAFE_TIP_CHARS) | set("\t\n\r")


def _validate_tip_name(field, value, *, allow_newick_unsafe=False):
    """Validate a single tip / taxon name string.

    `allow_newick_unsafe=True` is used for `old_name` and `tips` inputs that
    must match what already exists in the tree (those values came from the
    pipeline itself, not from the caller, so we only length-bound them).
    For new names introduced by the caller (`new_name`), we additionally
    reject Newick-unsafe characters.
    """
    s, err = _validate_string(
        field, value,
        max_len=TREE_MUTATION_LIMITS["max_name_len"],
        allow_empty=False,
    )
    if err:
        return None, err
    if not allow_newick_unsafe:
        bad = sorted({c for c in s if c in _NEWICK_UNSAFE})
        if bad:
            return None, error_response(
                code="validation_failed",
                message=(
                    f"`{field}` contains characters that are invalid in "
                    f"Newick tip names: {bad}. Avoid parentheses, brackets, "
                    f"commas, colons, semicolons, and whitespace other than "
                    f"spaces."
                ),
                status=422,
                details={"field": field, "invalid_chars": bad},
            )
    return s, None


def _json_object_body():
    """Return (body_dict, error_response) for a POST body that must be an object.

    `request.get_json(silent=True) or {}` is not enough: a JSON array or string
    survives it (a non-empty list is truthy), and the handler's first `.get()`
    then raises AttributeError and the caller sees a 500 for what is really a
    malformed request. Missing/empty bodies still become `{}` so endpoints with
    all-optional fields keep working.
    """
    body = request.get_json(silent=True)
    if body is None:
        return {}, None
    if not isinstance(body, dict):
        return None, error_response(
            code="bad_request",
            message="Request body must be a JSON object.",
            status=400,
            details={"type": type(body).__name__},
        )
    return body, None


def _mutation(handler, job_id, *, where, scope="jobs:write"):
    """Common wrapper for prune/rename/reroot/midpoint endpoints. Performs
    auth, calls the underlying tree_edit_service function, returns envelope.
    """
    job = get_owned_job_or_404(job_id)
    if not job:
        return error_response(code="not_found", message="Job not found.", status=404)
    job_dir = Config.JOB_DIR / job_id
    body, body_error = _json_object_body()
    if body_error:
        return body_error
    try:
        from app.services.tree_edit_service import tree_state_lock

        with tree_state_lock(job_dir):
            result = handler(job_dir, body)
            # The v1 API is a separate client from the tree viewer, so it does
            # not get the viewer's Undo affordance -- but it does write the same
            # files. Leaving a viewer checkpoint from before this mutation would
            # let a later Undo silently revert an API edit, so drop it.
            from app.services.tree_undo_service import clear_undo_checkpoint
            clear_undo_checkpoint(job_dir)
        return ok(result)
    except ValueError as e:
        return error_response(code="validation_failed", message=str(e), status=422)
    except Exception as e:
        return server_error(e, where=where)


@bp.route('/jobs/<job_id>/tree/prune', methods=['POST'])
@require_api_token(scope='jobs:write')
@limiter.limit("60 per hour", key_func=api_token_key_func)
def prune_tree_v1(job_id):
    def handler(job_dir, body):
        from app.services.tree_edit_service import load_tree_state, prune_taxa, save_tree_state
        tips = body.get("tips")
        if not isinstance(tips, list):
            raise ValueError(
                "`tips` must be a JSON array of tip-name strings, e.g. "
                "{\"tips\": [\"Sample_A\", \"Sample_B\"]}."
            )
        if not tips:
            raise ValueError("`tips` must contain at least one tip name to prune.")
        max_tips = TREE_MUTATION_LIMITS["max_tips"]
        if len(tips) > max_tips:
            raise ValueError(
                f"`tips` has {len(tips):,} entries; the per-request maximum "
                f"is {max_tips:,}. Split large prune operations across "
                f"multiple requests."
            )
        max_len = TREE_MUTATION_LIMITS["max_name_len"]
        for i, t in enumerate(tips):
            if not isinstance(t, str):
                raise ValueError(
                    f"`tips[{i}]` must be a string; got {type(t).__name__}."
                )
            if not t:
                raise ValueError(f"`tips[{i}]` is an empty string.")
            if len(t) > max_len:
                raise ValueError(
                    f"`tips[{i}]` is {len(t):,} characters; tip names are "
                    f"capped at {max_len:,}."
                )
        state = load_tree_state(job_dir)
        state = prune_taxa(job_dir, state, tips)
        save_tree_state(job_dir, state)
        return state
    return _mutation(handler, job_id, where="prune_tree")


@bp.route('/jobs/<job_id>/tree/rename', methods=['POST'])
@require_api_token(scope='jobs:write')
@limiter.limit("60 per hour", key_func=api_token_key_func)
def rename_tip_v1(job_id):
    def handler(job_dir, body):
        from app.services.tree_edit_service import (
            load_tree_state,
            rename_tip,
            save_tree_state,
            validate_tip_rename,
        )
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        old_name, new_name = validate_tip_rename(
            body.get("old_name"), body.get("new_name")
        )
        state = load_tree_state(job_dir)
        state = rename_tip(state, old_name, new_name)
        save_tree_state(job_dir, state)
        return state
    return _mutation(handler, job_id, where="rename_tip")


@bp.route('/jobs/<job_id>/tree/reroot', methods=['POST'])
@require_api_token(scope='jobs:write')
@limiter.limit("60 per hour", key_func=api_token_key_func)
def reroot_v1(job_id):
    def handler(job_dir, body):
        from app.services.tree_edit_service import load_tree_state, reroot_tree, save_tree_state
        outgroup = body.get("outgroup")
        max_len = TREE_MUTATION_LIMITS["max_name_len"]
        if not isinstance(outgroup, str) or not outgroup.strip():
            raise ValueError(
                "`outgroup` must be a non-empty string naming a tip or "
                "internal node in the tree."
            )
        if len(outgroup) > max_len:
            raise ValueError(
                f"`outgroup` is {len(outgroup):,} characters; the limit is "
                f"{max_len:,}."
            )
        state = load_tree_state(job_dir)
        state = reroot_tree(job_dir, state, outgroup)
        save_tree_state(job_dir, state)
        return state
    return _mutation(handler, job_id, where="reroot_tree")


@bp.route('/jobs/<job_id>/tree/midpoint_root', methods=['POST'])
@require_api_token(scope='jobs:write')
@limiter.limit("60 per hour", key_func=api_token_key_func)
def midpoint_root_v1(job_id):
    def handler(job_dir, body):
        from app.services.tree_edit_service import load_tree_state, midpoint_root, save_tree_state
        state = load_tree_state(job_dir)
        state = midpoint_root(job_dir, state)
        save_tree_state(job_dir, state)
        return state
    return _mutation(handler, job_id, where="midpoint_root")


# ============================================================================
# Tools (parity with existing internal endpoints)
# ============================================================================

@bp.route('/tools/blast', methods=['POST'])
@require_api_token(scope='tools:read')
@limiter.limit("10 per minute; 200 per hour", key_func=api_token_key_func)
def tools_blast():
    from app.services.blast_service import blast_from_sequence, blast_from_accessions
    body, body_error = _json_object_body()
    if body_error:
        return body_error
    raw_query = body.get("query")
    query = raw_query.strip() if isinstance(raw_query, str) else ""
    if not query:
        return error_response(
            code="bad_request",
            message=(
                "`query` is required. Provide either a FASTA-formatted "
                "nucleotide sequence (e.g. '>name\\nACGT...') or a single "
                "GenBank accession (e.g. 'MK564475')."
            ),
            status=400,
        )
    max_query_len = Config.BLAST_MAX_QUERY_LENGTH
    if len(query) > max_query_len:
        return error_response(
            code="validation_failed",
            message=(
                f"`query` is {len(query):,} characters, which exceeds the "
                f"BLAST query limit of {max_query_len:,} characters. NCBI "
                f"rejects very long queries; please trim the sequence or "
                f"BLAST individual records separately."
            ),
            status=422,
            details={"field": "query", "length": len(query), "max_length": max_query_len},
        )

    try:
        min_identity = float(body.get("min_identity", 90.0))
    except (TypeError, ValueError):
        min_identity = 90.0
    min_identity = max(50.0, min(100.0, min_identity))
    try:
        max_sequences = int(body.get("max_sequences", 50))
    except (TypeError, ValueError):
        max_sequences = 50
    max_sequences = max(1, min(500, max_sequences))

    try:
        from app.api.routes import _is_genbank_accession
        if _is_genbank_accession(query):
            result = blast_from_accessions([query], Config,
                                           min_identity=min_identity,
                                           max_sequences=max_sequences)
        else:
            result = blast_from_sequence(query, Config,
                                         min_identity=min_identity,
                                         max_sequences=max_sequences)
        return ok(result)
    except Exception as e:
        return server_error(e, where="tools_blast")


@bp.route('/tools/genbank', methods=['POST'])
@require_api_token(scope='tools:read')
@limiter.limit("10 per minute; 200 per hour", key_func=api_token_key_func)
def tools_genbank():
    from app.api.routes import (
        MAX_CUSTOM_GENBANK_ACCESSIONS,
        _fetch_genbank_sequences_for_queue,
        _parse_genbank_accession_tokens,
    )
    body, body_error = _json_object_body()
    if body_error:
        return body_error
    raw = body.get("accessions") or body.get("query") or ""
    accessions, invalid = _parse_genbank_accession_tokens(raw)
    if not accessions:
        return error_response(
            code="validation_failed",
            message="No valid GenBank accessions found.",
            status=422,
            details={"invalid": invalid},
        )
    if len(accessions) > MAX_CUSTOM_GENBANK_ACCESSIONS:
        return error_response(
            code="validation_failed",
            message=f"Too many accessions. Maximum is {MAX_CUSTOM_GENBANK_ACCESSIONS}.",
            status=422,
            details={"count": len(accessions), "max": MAX_CUSTOM_GENBANK_ACCESSIONS},
        )
    try:
        sequences, skipped = _fetch_genbank_sequences_for_queue(accessions)
        return ok({
            "sequences": sequences,
            "skipped": skipped,
            "invalid": invalid,
        })
    except Exception as e:
        return server_error(e, where="tools_genbank")


def _inat_tree_v1_rate_key():
    """Token-id-bucketed key, with admin token-owners getting an exempt bucket."""
    token = getattr(g, "api_token", None)
    user = getattr(g, "api_user", None)
    if user and (user.email or "").strip().lower() in Config.INAT_OAUTH_ADMIN_EMAILS:
        return f"admin:{user.id}"
    if token is not None:
        return f"token:{token.id}"
    return api_token_key_func()


def _inat_tree_v1_rate_limit():
    user = getattr(g, "api_user", None)
    if user and (user.email or "").strip().lower() in Config.INAT_OAUTH_ADMIN_EMAILS:
        return "10000 per minute"
    return "10 per 5 minutes"


@bp.route('/tools/inaturalist-tree', methods=['POST'])
@require_api_token(scope='jobs:write')
@limiter.limit(_inat_tree_v1_rate_limit, key_func=_inat_tree_v1_rate_key)
@idempotent
def tools_inaturalist_tree():
    """Build Dikarya tree jobs from iNaturalist one-click tree input.

    Body: { "observation": "<id, URL, username, or project>", "resolved_type": "user|project" }
    Requires scope ``jobs:write`` (the call creates a tree job and later
    writes back to the iNaturalist observation field).
    """
    from app.services.inaturalist_tree_service import (
        InatTreeError, create_job_from_inat_observation,
        create_jobs_from_inat_scope, parse_inaturalist_tree_input,
    )
    body, body_error = _json_object_body()
    if body_error:
        return body_error
    raw = body.get("observation") or body.get("url") or body.get("input") or ""
    raw_resolved_type = body.get("resolved_type")
    resolved_type = (
        raw_resolved_type.strip().lower()
        if isinstance(raw_resolved_type, str) else ""
    )
    rebuild_ncbi_blast, bool_error = _validate_bool(
        "rebuild_ncbi_blast",
        body.get("rebuild_ncbi_blast", body.get("rebuild_ncbi")),
        default=False,
    )
    if bool_error:
        return bool_error
    recreate_existing_tree, bool_error = _validate_bool(
        "recreate_existing_tree",
        body.get("recreate_existing_tree"),
        default=False,
    )
    if bool_error:
        return bool_error
    # Alan 8/4/26 - Build an extra tree without touching the existing field URL.
    keep_existing_tree_url, bool_error = _validate_bool(
        "keep_existing_tree_url",
        body.get("keep_existing_tree_url"),
        default=False,
    )
    if bool_error:
        return bool_error
    local_limit, limit_error = _validate_mycomap_rerun_limit(body, "local")
    if limit_error:
        return limit_error
    ncbi_limit, limit_error = _validate_mycomap_rerun_limit(body, "ncbi")
    if limit_error:
        return limit_error
    try:
        parsed = parse_inaturalist_tree_input(raw)
        if parsed.get("type") == "single_observation":
            result = create_job_from_inat_observation(
                raw,
                user=g.api_user,
                rebuild_ncbi_blast=rebuild_ncbi_blast,
                recreate_existing_tree=recreate_existing_tree,
                keep_existing_tree_url=keep_existing_tree_url,
                mycomap_local_limit=local_limit,
                mycomap_ncbi_limit=ncbi_limit,
            )
            job_ids = [result["job_id"]]
        else:
            if rebuild_ncbi_blast or recreate_existing_tree or keep_existing_tree_url:
                if rebuild_ncbi_blast:
                    invalid_field = "rebuild_ncbi_blast"
                elif recreate_existing_tree:
                    invalid_field = "recreate_existing_tree"
                else:
                    invalid_field = "keep_existing_tree_url"
                return error_response(
                    code="validation_failed",
                    message=(
                        "NCBI BLAST rebuild and existing-tree options are "
                        "only supported for a single iNaturalist observation."
                    ),
                    status=422,
                    details={"field": invalid_field},
                )
            if not resolved_type:
                return error_response(
                    code="ambiguous_scope",
                    message="Provide resolved_type='user' or 'project' for username/project inputs.",
                    status=409,
                )
            result = create_jobs_from_inat_scope(
                raw,
                resolved_type=resolved_type,
                user=g.api_user,
                mycomap_local_limit=local_limit,
                mycomap_ncbi_limit=ncbi_limit,
            )
            job_ids = result.get("job_ids") or []
        # Tag metrics with the originating API token id for traceability.
        for job_id in job_ids:
            job = Job.query.get(job_id)
            if job is not None:
                m = job.metrics or {}
                m["api_token_id"] = g.api_token.id
                job.metrics = m
        db.session.commit()
        return ok(result, status=202)
    except InatTreeError as e:
        if e.status in (400, 422):
            code = "validation_failed"
        elif e.status == 503:
            code = "service_unavailable"
        else:
            code = "upstream_error"
        return error_response(
            code=code,
            message=str(e),
            status=e.status,
            details=e.details,
        )
    except Exception as e:
        db.session.rollback()
        return server_error(e, where="tools_inaturalist_tree")
