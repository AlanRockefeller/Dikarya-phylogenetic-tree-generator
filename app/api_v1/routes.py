"""Public API v1 routes.

Phase 1 endpoints: /health, /me, /tokens
Phase 2 endpoints: /jobs (+ mutation), /jobs/{id}/files, /jobs/{id}/logs, /tools/*
"""
import json
import re
from datetime import datetime
from pathlib import Path

from flask import (
    Response, current_app, g, request, send_file, stream_with_context, url_for,
)
from sqlalchemy import desc

from app.api_v1 import bp
from app.api_v1.auth import require_api_token, api_token_key_func
from app.api_v1.envelope import error_response, ok, paginate_query, server_error
from app.api_v1.idempotency import idempotent
from app.api_v1.jobs import (
    DOWNLOADABLE_ARTIFACTS, LOG_NAMES, artifact_path,
    get_owned_job_or_404, list_available_artifacts, serialize_job,
)
from app.api_v1.openapi import build_spec
from app.config import Config
from app.extensions import db, limiter
from app.models import ApiToken, Job
from app.services.security_utils import validate_safe_file_path
from app.workers.queue import enqueue_job, enqueue_recompute_job


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
VALID_ALIGNERS    = {"mafft", "muscle", "clustalo", "iqtree_builtin", "default"}
VALID_TRIMMERS    = {"none", "trimal", "bmge"}

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
    "mcmc_generations":    (1_000, 100_000_000),
    "mcmc_nruns":          (1,     8),
    "mcmc_nchains":        (1,     16),
}

# Fields a recompute may override. Sequence/accessions are intentionally
# excluded -- recompute is "re-run with different parameters on the same
# input data," not "submit a new dataset." Use POST /jobs for that.
RECOMPUTE_ALLOWED_FIELDS = frozenset({
    "tree_method", "tree_model",
    "alignment_method", "trimming_method",
    "bootstrap", "mcmc_generations", "mcmc_nruns", "mcmc_nchains",
    "outgroup", "notes",
})


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
    mcmc_generations, mcmc_nruns, mcmc_nchains, notes}.
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
        "tree_method", data.get("tree_method", "fasttree"), VALID_TREE_METHODS)
    if err: return err
    aligner, err = _validate_categorical(
        "alignment_method", data.get("alignment_method", "mafft"), VALID_ALIGNERS)
    if err: return err
    trimmer, err = _validate_categorical(
        "trimming_method", data.get("trimming_method", "none"), VALID_TRIMMERS)
    if err: return err

    # Clamped integers.
    bootstrap, err = _validate_clamped_int("bootstrap", data.get("bootstrap"), default=1000)
    if err: return err
    mcmc_generations, err = _validate_clamped_int(
        "mcmc_generations", data.get("mcmc_generations"), default=50_000)
    if err: return err
    mcmc_nruns, err = _validate_clamped_int(
        "mcmc_nruns", data.get("mcmc_nruns"), default=2)
    if err: return err
    mcmc_nchains, err = _validate_clamped_int(
        "mcmc_nchains", data.get("mcmc_nchains"), default=4)
    if err: return err

    # Strings.
    notes, err = _validate_string("notes", data.get("notes"),
                                  max_len=LIMITS["notes_max"])
    if err: return err
    tree_model, err = _validate_string(
        "tree_model", data.get("tree_model", "GTR+G"),
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
    raw_input_type = (data.get("input_type") or "").strip().lower() or None
    if raw_input_type in ("fasta", "fasta_upload") and not sequence_text:
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
    if sequence_text and raw_input_type in (None, "fasta", "sequence", "pasted_sequence"):
        input_type = "pasted_sequence"
    elif accessions and raw_input_type in (None, "accession_list", "accessions"):
        input_type = "accession_list"
    elif raw_input_type:
        input_type = raw_input_type
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
        "alignment_options": alignment_options,
        "tree_method":       tree_method,
        "tree_model":        tree_model,
        "bootstrap":         bootstrap,
        "mcmc_generations":  mcmc_generations,
        "mcmc_nruns":        mcmc_nruns,
        "mcmc_nchains":      mcmc_nchains,
    }

    try:
        job_id = enqueue_job(job_params)
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
                "via": "api_v1",
                "api_token_id": g.api_token.id,
            },
        )
        db.session.add(job_record)
        db.session.commit()
        return ok(serialize_job(job_record), status=202)
    except Exception as e:
        db.session.rollback()
        return server_error(e, where="create_job")


@bp.route('/jobs', methods=['GET'])
@require_api_token(scope='jobs:read')
@limiter.limit("600 per minute", key_func=api_token_key_func)
def list_jobs():
    """List the caller's jobs. Filters: ?status=, ?since=, ?until=.
    Pagination: ?page=, ?per_page= (max 100)."""
    q = Job.query.filter_by(user_id=g.api_user.id)

    status = request.args.get("status")
    if status:
        q = q.filter(Job.status == status)

    since = request.args.get("since")
    if since:
        try:
            q = q.filter(Job.created_at >= datetime.fromisoformat(since.replace("Z", "+00:00")))
        except ValueError:
            return error_response(code="bad_request",
                                  message="`since` must be ISO-8601.", status=400)
    until = request.args.get("until")
    if until:
        try:
            q = q.filter(Job.created_at < datetime.fromisoformat(until.replace("Z", "+00:00")))
        except ValueError:
            return error_response(code="bad_request",
                                  message="`until` must be ISO-8601.", status=400)

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


@bp.route('/jobs/<job_id>', methods=['DELETE'])
@require_api_token(scope='jobs:write')
@limiter.limit("60 per hour", key_func=api_token_key_func)
def delete_job(job_id):
    """Delete job DB record and its on-disk directory."""
    import shutil
    job = get_owned_job_or_404(job_id)
    if not job:
        return error_response(code="not_found", message="Job not found.", status=404)
    try:
        if job.job_dir:
            p = Path(job.job_dir)
            if p.exists() and p.is_dir():
                # Guard against deleting anything outside JOB_DIR.
                if p.resolve().is_relative_to(Config.JOB_DIR.resolve()):
                    shutil.rmtree(p, ignore_errors=True)
        db.session.delete(job)
        db.session.commit()
        return ok({"deleted": True, "id": job_id})
    except Exception as e:
        db.session.rollback()
        return server_error(e, where="delete_job")


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
        for field, default in (("bootstrap", 1000), ("mcmc_generations", 50_000),
                               ("mcmc_nruns", 2), ("mcmc_nchains", 4)):
            if field in body:
                v, err = _validate_clamped_int(field, body[field], default=default)
                if err: return err
                overrides[field] = v
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
        # Always async for the public API.
        rq_job_id = enqueue_recompute_job(job_id, params)
        job.status = "queued"
        metrics = job.metrics or {}
        metrics["recompute_requested_at"] = datetime.utcnow().isoformat()
        job.metrics = metrics
        db.session.commit()
        return ok({
            "id": job_id,
            "status": "queued",
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

SSE_MAX_DURATION_SECONDS = 30 * 60       # hard cap per connection
SSE_MAX_CONCURRENT_PER_TOKEN = 5          # max simultaneous streams per token
SSE_HEARTBEAT_SECONDS = 15                # ping interval


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
    token_id = g.api_token.id
    conn_key = f"sse:conn:{token_id}"
    try:
        r_gate = _redis.from_url(Config.REDIS_URL)
        current = r_gate.incr(conn_key)
        r_gate.expire(conn_key, SSE_MAX_DURATION_SECONDS + 60)
    except Exception:
        # If Redis is unreachable we fall back to enforcing only the per-token
        # rate limit -- which is itself sufficient to bound abuse.
        current = 1
        r_gate = None

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
        from app.models import ApiToken as _ApiToken
        started = time.monotonic()
        r = _redis.from_url(Config.REDIS_URL)
        pubsub = r.pubsub()
        channel = f"job:{job_id}:events"
        pubsub.subscribe(channel)
        try:
            snapshot = _build_snapshot(job_id)
            yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
            job_status = snapshot["job"]["status"]
            last_ping = time.monotonic()
            last_db_poll = 0.0
            last_token_check = time.monotonic()
            while True:
                # Hard duration cap. Clients should reconnect.
                if time.monotonic() - started > SSE_MAX_DURATION_SECONDS:
                    yield (
                        "event: timeout\n"
                        "data: {\"reason\": \"max_duration_reached\", "
                        f"\"max_seconds\": {SSE_MAX_DURATION_SECONDS}}}\n\n"
                    )
                    break

                message = pubsub.get_message(timeout=0.1)
                if message and message['type'] == 'message':
                    data = message['data']
                    if isinstance(data, bytes):
                        data = data.decode('utf-8')
                    yield f"data: {data}\n\n"
                    try:
                        ev = json.loads(data)
                        if ev.get('type') == 'job_state' and ev.get('status') in ('completed', 'failed'):
                            time.sleep(0.5)
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
                                break
                        except Exception:
                            db.session.rollback()
                if job_status not in ('completed', 'failed'):
                    if now - last_db_poll >= 1.0:
                        last_db_poll = now
                        db.session.expire_all()
                        db_job_check = Job.query.get(job_id)
                        if db_job_check and db_job_check.status in ('completed', 'failed'):
                            job_status = db_job_check.status
                            time.sleep(1)
                            break
                time.sleep(0.05)
        finally:
            try:
                pubsub.unsubscribe()
                pubsub.close()
            except Exception:
                pass
            if r_gate is not None:
                try:
                    r_gate.decr(conn_key)
                except Exception:
                    pass

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
    "max_name_len":  256,
}

# Characters that would corrupt Newick syntax if written verbatim as a tip
# name. Reject these in *new* tip names (rename target, prune target list)
# so a malformed name can't break tree exports.
_NEWICK_UNSAFE = set("()[];,:'\"\t\n\r")


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
                    f"commas, colons, semicolons, quotes, and whitespace "
                    f"other than spaces."
                ),
                status=422,
                details={"field": field, "invalid_chars": bad},
            )
    return s, None


def _mutation(handler, job_id, *, where, scope="jobs:write"):
    """Common wrapper for prune/rename/reroot/midpoint endpoints. Performs
    auth, calls the underlying tree_edit_service function, returns envelope.
    """
    job = get_owned_job_or_404(job_id)
    if not job:
        return error_response(code="not_found", message="Job not found.", status=404)
    job_dir = Config.JOB_DIR / job_id
    body = request.get_json(silent=True) or {}
    try:
        result = handler(job_dir, body)
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
        from app.services.tree_edit_service import load_tree_state, rename_tip, save_tree_state
        max_len = TREE_MUTATION_LIMITS["max_name_len"]
        old_name = body.get("old_name")
        new_name = body.get("new_name")
        if not isinstance(old_name, str) or not old_name:
            raise ValueError(
                "`old_name` must be a non-empty string matching an existing "
                "tip in the tree."
            )
        if not isinstance(new_name, str) or not new_name:
            raise ValueError(
                "`new_name` must be a non-empty string."
            )
        if len(old_name) > max_len or len(new_name) > max_len:
            raise ValueError(
                f"Tip names are limited to {max_len:,} characters "
                f"(old_name={len(old_name)}, new_name={len(new_name)})."
            )
        bad = sorted({c for c in new_name if c in _NEWICK_UNSAFE})
        if bad:
            raise ValueError(
                f"`new_name` contains characters that are invalid in Newick "
                f"tip names: {bad}. Avoid parentheses, brackets, commas, "
                f"colons, semicolons, quotes, and whitespace other than spaces."
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
    body = request.get_json(silent=True) or {}
    query = (body.get("query") or "").strip()
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
    body = request.get_json(silent=True) or {}
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
    body = request.get_json(silent=True) or {}
    raw = body.get("observation") or body.get("url") or body.get("input") or ""
    resolved_type = (body.get("resolved_type") or "").strip().lower()
    try:
        parsed = parse_inaturalist_tree_input(raw)
        if parsed.get("type") == "single_observation":
            result = create_job_from_inat_observation(raw, user=g.api_user)
            job_ids = [result["job_id"]]
        else:
            if not resolved_type:
                return error_response(
                    code="ambiguous_scope",
                    message="Provide resolved_type='user' or 'project' for username/project inputs.",
                    status=409,
                )
            result = create_jobs_from_inat_scope(raw, resolved_type=resolved_type, user=g.api_user)
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
        code = "validation_failed" if e.status in (400, 422) else "upstream_error"
        return error_response(code=code, message=str(e), status=e.status)
    except Exception as e:
        db.session.rollback()
        return server_error(e, where="tools_inaturalist_tree")
