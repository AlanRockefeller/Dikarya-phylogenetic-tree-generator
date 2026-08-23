"""JSON endpoints behind the /voucher-sync page.

Every endpoint requires a Dikarya login and every run lookup is scoped to
``current_user`` (404, not 403, so run ids cannot be probed). Nothing here
returns an iNaturalist token; the worker reads it from the DB itself.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from flask import Response, jsonify, request
from flask_login import current_user, login_required

from app.api import bp
from app.config import Config
from app.extensions import db, limiter
from app.models import VoucherSyncRun
from app.services.security_utils import validate_job_id

logger = logging.getLogger(__name__)

MAX_APPLY_IDS = 5000


def _rate_key() -> str:
    from flask_limiter.util import get_remote_address
    if current_user.is_authenticated:
        return f"user:{current_user.id}"
    return f"ip:{get_remote_address()}"


def _scan_rate_limit() -> str:
    if current_user.is_authenticated:
        email = (current_user.email or "").strip().lower()
        if email in Config.INAT_OAUTH_ADMIN_EMAILS:
            return "10000 per minute"
    return "6 per hour; 30 per day"


def _apply_rate_limit() -> str:
    if current_user.is_authenticated:
        email = (current_user.email or "").strip().lower()
        if email in Config.INAT_OAUTH_ADMIN_EMAILS:
            return "10000 per minute"
    return "10 per hour"


def _own_run(run_id: str) -> VoucherSyncRun:
    from flask import abort
    if not validate_job_id(run_id):
        abort(404)
    run = VoucherSyncRun.query.filter_by(id=run_id, user_id=current_user.id).first()
    if run is None:
        abort(404)
    return run


def _active_run() -> Optional[VoucherSyncRun]:
    return (VoucherSyncRun.query
            .filter(VoucherSyncRun.user_id == current_user.id,
                    VoucherSyncRun.status.in_(VoucherSyncRun.ACTIVE_STATUSES))
            .order_by(VoucherSyncRun.created_at.desc())
            .first())


def _reconcile_stale(run: VoucherSyncRun) -> None:
    """A worker restart SIGKILLs the horse and the run stays 'running'. The
    phylo reconciler only knows the Job table, so check RQ here: if RQ says
    the job is gone or failed and the row has not moved for two minutes, mark
    it failed so the page stops polling."""
    if run.status not in VoucherSyncRun.ACTIVE_STATUSES:
        return
    stale_after = datetime.utcnow() - timedelta(minutes=2)
    if (run.updated_at or run.created_at) > stale_after:
        return
    from app.workers.queue import get_voucher_run_rq_status
    rq_status = get_voucher_run_rq_status(run.id)
    if rq_status in (None, "failed", "stopped", "canceled"):
        run.status = "failed"
        run.error = ("The scan was interrupted on the server (worker restart). "
                     "Start a new preview.")
        run.finished_at = datetime.utcnow()
        db.session.commit()
        logger.warning("event=voucher_sync.reconciled_stale run=%s rq=%s", run.id, rq_status)


def _redis():
    from app.workers.queue import get_redis_connection
    return get_redis_connection()


def _live_slice(run_id: str, cursor_rows: int, cursor_log: int):
    from app.workers.voucher_sync_tasks import run_keys
    keys = run_keys(run_id)
    try:
        r = _redis()
        raw_rows = r.lrange(keys["rows"], cursor_rows, -1)
        raw_log = r.lrange(keys["log"], cursor_log, -1)
    except Exception as exc:
        logger.warning("event=voucher_sync.redis_read_failed run=%s error=%s", run_id, exc)
        return [], [], False
    rows = []
    for item in raw_rows:
        try:
            rows.append(json.loads(item))
        except (TypeError, ValueError):
            continue
    log = [l.decode("utf-8", "replace") if isinstance(l, bytes) else str(l) for l in raw_log]
    return rows, log, True


# ---------------------------------------------------------------------------
# Status / lookups
# ---------------------------------------------------------------------------
@bp.route('/voucher-sync/status', methods=['GET'])
@login_required
def voucher_sync_status():
    from app.services.inat_user_credential_service import credential_status
    status = credential_status(current_user.id)
    active = _active_run()
    if active is not None:
        _reconcile_stale(active)
        if active.status not in VoucherSyncRun.ACTIVE_STATUSES:
            active = None
    status["active_run_id"] = active.id if active else None
    return jsonify(status)


@bp.route('/voucher-sync/fields', methods=['GET'])
@login_required
@limiter.limit("30 per minute", key_func=_rate_key)
def voucher_sync_fields():
    from app.services.voucher_sync_service import INatClient
    q = (request.args.get("q") or "").strip()[:80]
    if len(q) < 2:
        return jsonify({"fields": []})
    try:
        fields = INatClient().search_observation_fields(q)
    except Exception as exc:
        logger.warning("event=voucher_sync.field_search_failed error=%s", type(exc).__name__)
        return jsonify({"fields": [], "error": "iNaturalist field search is unavailable right now."}), 502
    return jsonify({"fields": fields[:25]})


@bp.route('/voucher-sync/runs', methods=['GET'])
@login_required
def voucher_sync_runs():
    runs = (VoucherSyncRun.query
            .filter_by(user_id=current_user.id)
            .order_by(VoucherSyncRun.created_at.desc())
            .limit(10).all())
    for run in runs:
        _reconcile_stale(run)
    return jsonify({"runs": [r.to_dict() for r in runs]})


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
@bp.route('/voucher-sync/scan', methods=['POST'])
@login_required
@limiter.limit(_scan_rate_limit, key_func=_rate_key)
def voucher_sync_scan():
    from app.services.inat_user_credential_service import get_credential
    from app.services.voucher_sync_service import validate_scan_params

    if get_credential(current_user.id) is None:
        return jsonify({"error": "Connect your iNaturalist account first."}), 409

    active = _active_run()
    if active is not None:
        _reconcile_stale(active)
        if active.status in VoucherSyncRun.ACTIVE_STATUSES:
            return jsonify({"error": "A run is already in progress.",
                            "active_run_id": active.id}), 409

    params, err = validate_scan_params(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400

    run = VoucherSyncRun(id=str(uuid.uuid4()), user_id=current_user.id,
                         kind="scan", status="queued", params=params)
    db.session.add(run)
    db.session.commit()

    try:
        from app.workers.queue import enqueue_voucher_sync_run
        enqueue_voucher_sync_run(run.id, "scan")
    except Exception as exc:
        logger.exception("event=voucher_sync.enqueue_failed run=%s", run.id)
        run.status = "failed"
        run.error = "Could not queue the scan (background worker unavailable)."
        run.finished_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"error": run.error}), 503

    logger.info("event=voucher_sync.scan_queued run=%s field=%s window=%s..%s ocr=%s overwrite=%s",
                run.id, params["field_id"], params["date_start"], params["date_end"],
                params["use_ocr"], params["allow_overwrite"])
    return jsonify({"run_id": run.id, "status": run.status}), 202


# ---------------------------------------------------------------------------
# Run progress / control
# ---------------------------------------------------------------------------
@bp.route('/voucher-sync/runs/<run_id>', methods=['GET'])
@login_required
@limiter.limit("120 per minute", key_func=_rate_key)
def voucher_sync_run_detail(run_id):
    run = _own_run(run_id)
    _reconcile_stale(run)
    try:
        cursor_rows = max(0, int(request.args.get("cursor", 0)))
        cursor_log = max(0, int(request.args.get("log_cursor", 0)))
    except ValueError:
        return jsonify({"error": "Invalid cursor."}), 400

    payload = run.to_dict()
    live_rows, live_log, live_ok = _live_slice(run.id, cursor_rows, cursor_log)
    if run.status in VoucherSyncRun.ACTIVE_STATUSES or live_ok and (live_rows or live_log):
        payload["rows"] = live_rows
        payload["log"] = live_log
        payload["next_cursor"] = cursor_rows + len(live_rows)
        payload["next_log_cursor"] = cursor_log + len(live_log)
        payload["source"] = "live"
    else:
        # Redis expired (or was never written): serve the persisted rows.
        persisted = run.rows or []
        payload["rows"] = persisted[cursor_rows:]
        payload["log"] = []
        payload["next_cursor"] = len(persisted)
        payload["next_log_cursor"] = cursor_log
        payload["source"] = "db"
    if run.status not in VoucherSyncRun.ACTIVE_STATUSES and cursor_rows == 0 and run.rows is not None \
            and payload["source"] == "live":
        # Finished run, first poll: the DB copy is canonical (includes
        # post-apply updates), so prefer it over the Redis stream.
        payload["rows"] = run.rows
        payload["next_cursor"] = len(run.rows)
        payload["source"] = "db"
    return jsonify(payload)


@bp.route('/voucher-sync/runs/<run_id>/cancel', methods=['POST'])
@login_required
def voucher_sync_run_cancel(run_id):
    run = _own_run(run_id)
    if run.status not in VoucherSyncRun.ACTIVE_STATUSES:
        return jsonify({"status": run.status, "cancelled": False})
    from app.workers.voucher_sync_tasks import run_keys
    try:
        _redis().set(run_keys(run.id)["cancel"], "1", ex=3600)
    except Exception as exc:
        logger.warning("event=voucher_sync.cancel_failed run=%s error=%s", run.id, exc)
        return jsonify({"error": "Could not reach the worker to cancel."}), 503
    return jsonify({"status": run.status, "cancelled": True})


@bp.route('/voucher-sync/runs/<run_id>/apply', methods=['POST'])
@login_required
@limiter.limit(_apply_rate_limit, key_func=_rate_key)
def voucher_sync_run_apply(run_id):
    from app.services.inat_user_credential_service import get_credential
    from app.services.voucher_sync_service import UPDATE

    parent = _own_run(run_id)
    if parent.kind != "scan" or parent.status not in ("completed", "cancelled"):
        return jsonify({"error": "Only a finished preview can be applied."}), 409
    if get_credential(current_user.id) is None:
        return jsonify({"error": "Connect your iNaturalist account first."}), 409
    active = _active_run()
    if active is not None:
        _reconcile_stale(active)
        if active.status in VoucherSyncRun.ACTIVE_STATUSES:
            return jsonify({"error": "A run is already in progress.",
                            "active_run_id": active.id}), 409

    data = request.get_json(silent=True) or {}
    confirm_overwrite = bool(data.get("confirm_overwrite"))
    ids_raw = data.get("observation_ids")
    selected: Optional[set] = None
    if ids_raw is not None:
        if not isinstance(ids_raw, list) or len(ids_raw) > MAX_APPLY_IDS:
            return jsonify({"error": "observation_ids must be a list."}), 400
        try:
            selected = {int(x) for x in ids_raw}
        except (TypeError, ValueError):
            return jsonify({"error": "observation_ids must be integers."}), 400

    rows: List[Dict[str, Any]] = [r for r in (parent.rows or [])
                                  if r.get("action") == UPDATE and r.get("detected_voucher")]
    if selected is not None:
        rows = [r for r in rows if int(r.get("observation_id") or 0) in selected]
    if not rows:
        return jsonify({"error": "Nothing to apply: no rows are marked Update."}), 409

    overwrite_rows = [r for r in rows if r.get("current_value")]
    ocr_rows = [r for r in rows if "ocr" in (r.get("reason") or "")]
    if overwrite_rows and not confirm_overwrite:
        return jsonify({
            "error": "confirm_overwrite_required",
            "counts": {"total": len(rows), "overwrite": len(overwrite_rows), "ocr": len(ocr_rows)},
        }), 409

    run = VoucherSyncRun(
        id=str(uuid.uuid4()), user_id=current_user.id, kind="apply", status="queued",
        parent_run_id=parent.id,
        params={
            "field_id": int(parent.params.get("field_id")),
            "allow_overwrite": bool(overwrite_rows) and confirm_overwrite,
            "observation_ids": sorted(int(r["observation_id"]) for r in rows),
        },
    )
    db.session.add(run)
    db.session.commit()
    try:
        from app.workers.queue import enqueue_voucher_sync_run
        enqueue_voucher_sync_run(run.id, "apply")
    except Exception:
        logger.exception("event=voucher_sync.enqueue_failed run=%s", run.id)
        run.status = "failed"
        run.error = "Could not queue the apply (background worker unavailable)."
        run.finished_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"error": run.error}), 503

    logger.info("event=voucher_sync.apply_queued run=%s parent=%s count=%s overwrite=%s",
                run.id, parent.id, len(rows), len(overwrite_rows))
    return jsonify({"run_id": run.id, "status": run.status,
                    "counts": {"total": len(rows), "overwrite": len(overwrite_rows),
                               "ocr": len(ocr_rows)}}), 202


@bp.route('/voucher-sync/runs/<run_id>/export.csv', methods=['GET'])
@login_required
def voucher_sync_run_export(run_id):
    from app.services.voucher_sync_service import rows_to_csv_text
    run = _own_run(run_id)
    rows = run.rows
    if rows is None:
        live_rows, _, _ = _live_slice(run.id, 0, 0)
        rows = live_rows
    text = rows_to_csv_text(rows or [])
    filename = f"voucher-sync-{run.id[:8]}.csv"
    return Response(text, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
