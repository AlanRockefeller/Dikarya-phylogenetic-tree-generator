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


@bp.route('/jobs', methods=['POST'])
@require_api_token(scope='jobs:write')
@limiter.limit("30 per hour; 200 per day", key_func=api_token_key_func)
@idempotent
def create_job():
    """Create a new phylo job. Body: {input_type, sequence, accessions,
    alignment_method, trimming_method, tree_method, tree_model, bootstrap,
    mcmc_generations, mcmc_nruns, mcmc_nchains, notes}.
    """
    data = request.get_json(silent=True) or {}

    # Validation of categorical params (allowlist).
    tree_method = data.get("tree_method", "fasttree")
    if tree_method not in VALID_TREE_METHODS:
        return error_response(
            code="validation_failed",
            message=f"tree_method must be one of: {sorted(VALID_TREE_METHODS)}",
            status=422,
            details={"field": "tree_method", "value": tree_method},
        )

    aligner = data.get("alignment_method", "mafft")
    if aligner not in VALID_ALIGNERS:
        return error_response(
            code="validation_failed",
            message=f"alignment_method must be one of: {sorted(VALID_ALIGNERS)}",
            status=422,
            details={"field": "alignment_method", "value": aligner},
        )

    trimmer = data.get("trimming_method", "none")
    if trimmer not in VALID_TRIMMERS:
        return error_response(
            code="validation_failed",
            message=f"trimming_method must be one of: {sorted(VALID_TRIMMERS)}",
            status=422,
            details={"field": "trimming_method", "value": trimmer},
        )

    # Normalize input_type. The public JSON API only supports inline data:
    # FASTA text in `sequence`, or accessions in `accessions`. Server-side
    # FASTA uploads (the worker's `fasta_upload` mode, which expects a file
    # already staged on disk) are not reachable via this endpoint and would
    # produce a job that's guaranteed to fail at worker time. We map any
    # "fasta"/"sequence"/missing variant to `pasted_sequence` when a sequence
    # body is present, and reject up-front otherwise.
    raw_input_type = (data.get("input_type") or "").strip().lower() or None
    sequence_text = data.get("sequence") or ""

    if raw_input_type in ("fasta", "fasta_upload") and not sequence_text:
        return error_response(
            code="validation_failed",
            message=(
                "The public API does not support server-side FASTA file "
                "uploads here. Send FASTA text in the 'sequence' field with "
                "input_type='pasted_sequence'."
            ),
            status=422,
            details={"field": "input_type", "value": raw_input_type},
        )

    if sequence_text and raw_input_type in (None, "fasta", "sequence", "pasted_sequence"):
        input_type = "pasted_sequence"
    elif raw_input_type:
        # Accession-list and any future modes pass through unchanged so the
        # worker can apply its own routing.
        input_type = raw_input_type
    else:
        input_type = "pasted_sequence"

    job_params = {
        "input_type": input_type[:64],
        "notes": str(data.get("notes", ""))[:2000],
        "sequence": sequence_text,
        "accessions": data.get("accessions", []) or [],
        "alignment_method": aligner,
        "trimming_method": trimmer,
        "alignment_options": data.get("alignment_options", {}) or {},
        "tree_method": tree_method,
        "tree_model": str(data.get("tree_model", "GTR+G"))[:64],
        "bootstrap":         _clamp_int(data.get("bootstrap"),         1000,     0,        10_000),
        "mcmc_generations":  _clamp_int(data.get("mcmc_generations"),  50_000,   1_000,    100_000_000),
        "mcmc_nruns":        _clamp_int(data.get("mcmc_nruns"),        2,        1,        8),
        "mcmc_nchains":      _clamp_int(data.get("mcmc_nchains"),      4,        1,        16),
    }

    # Sequence + accession sanity caps.
    if isinstance(job_params["sequence"], str) and len(job_params["sequence"]) > 5_000_000:
        return error_response(
            code="validation_failed",
            message="sequence payload exceeds 5 MB.",
            status=422,
        )
    if isinstance(job_params["accessions"], list) and len(job_params["accessions"]) > 500:
        return error_response(
            code="validation_failed",
            message="accessions list exceeds 500 entries.",
            status=422,
        )

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
        # Merge stored params with request overrides, same as the UI route.
        params_path = job_dir / "input_info.json"
        params = {}
        if params_path.exists():
            with open(params_path, "r") as f:
                params = json.load(f)
        body = request.get_json(silent=True) or {}
        params.update(body)
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

@bp.route('/jobs/<job_id>/events', methods=['GET'])
@require_api_token(scope='jobs:read')
def job_events(job_id):
    """SSE stream of pipeline progress events. Mirrors the internal /api/job/<id>/events
    stream but enforces strict ownership."""
    import time
    import redis as _redis
    from app.api.routes import _build_snapshot  # reuse existing snapshot builder

    job = get_owned_job_or_404(job_id)
    if not job:
        return error_response(code="not_found", message="Job not found.", status=404)

    def generate():
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
            while True:
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
                if now - last_ping >= 15:
                    yield "event: ping\ndata: {}\n\n"
                    last_ping = now
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
            pubsub.unsubscribe()
            pubsub.close()

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
        tips = body.get("tips", [])
        if not isinstance(tips, list) or not all(isinstance(t, str) for t in tips):
            raise ValueError("`tips` must be a list of strings.")
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
        old_name = body.get("old_name")
        new_name = body.get("new_name")
        if not isinstance(old_name, str) or not isinstance(new_name, str):
            raise ValueError("`old_name` and `new_name` must be strings.")
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
        if not isinstance(outgroup, str) or not outgroup.strip():
            raise ValueError("`outgroup` must be a non-empty string (tip or internal node name).")
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
        return error_response(code="bad_request", message="`query` is required.", status=400)

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
