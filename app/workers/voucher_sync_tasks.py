"""RQ entry points for Voucher Sync runs.

Both tasks receive only a ``run_id``. Everything else -- the scan
parameters, the rows to apply and the user's iNaturalist token -- is loaded
from the database inside the worker, so nothing sensitive is ever serialized
into Redis or printed in RQ's job description.

Live progress goes to Redis lists the API polls with a cursor:

    voucher_sync:run:<id>:rows   one JSON row per scanned observation
    voucher_sync:run:<id>:log    one text line per event
    voucher_sync:run:<id>:cancel presence = user asked to stop

The finished ``rows``/``summary`` are persisted to ``voucher_sync_run`` so
Apply can re-read the server-held preview instead of trusting the browser.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.extensions import db
from app.models import VoucherSyncRun
from app.services.log_context import background_job_context
from app.services.inaturalist_oauth_service import InatAuthError
from app.workers.queue import get_redis_connection

logger = logging.getLogger(__name__)

ROWS_KEY = "voucher_sync:run:{run_id}:rows"
LOG_KEY = "voucher_sync:run:{run_id}:log"
CANCEL_KEY = "voucher_sync:run:{run_id}:cancel"


def run_keys(run_id: str) -> Dict[str, str]:
    """Redis keys holding a run's streamed rows, log lines and cancel flag."""
    return {
        "rows": ROWS_KEY.format(run_id=run_id),
        "log": LOG_KEY.format(run_id=run_id),
        "cancel": CANCEL_KEY.format(run_id=run_id),
    }


def _config(name: str, default: Any) -> Any:
    """Read a Voucher Sync setting off the app config with a fallback."""
    from app.config import Config
    return getattr(Config, name, default)


class _RunContext:
    """Small helper bundling the Redis keys, TTL and DB row for one run."""

    def __init__(self, run: VoucherSyncRun):
        self.run = run
        self.redis = get_redis_connection()
        self.keys = run_keys(run.id)
        self.ttl = int(_config("VOUCHER_SYNC_RUN_TTL_SECONDS", 86400))
        self._last_commit = 0.0

    def log(self, text: str) -> None:
        self.redis.rpush(self.keys["log"], text)
        self.redis.expire(self.keys["log"], self.ttl)

    def push_row(self, row: Dict[str, Any]) -> None:
        self.redis.rpush(self.keys["rows"], json.dumps(row, default=str))
        self.redis.expire(self.keys["rows"], self.ttl)

    def cancelled(self) -> bool:
        try:
            return bool(self.redis.exists(self.keys["cancel"]))
        except Exception:
            return False

    def progress(self, done: int, total: Optional[int], *, force: bool = False) -> None:
        self.run.progress_done = done
        if total is not None:
            self.run.progress_total = total
        now = time.monotonic()
        if force or now - self._last_commit > 2.0 or done % 10 == 0:
            db.session.commit()
            self._last_commit = now

    def finish(self, status: str, *, rows: Optional[List[Dict[str, Any]]] = None,
               summary: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> None:
        self.run.status = status
        if rows is not None:
            self.run.rows = rows
        if summary is not None:
            self.run.summary = summary
        if error:
            self.run.error = error[:2000]
        self.run.finished_at = datetime.utcnow()
        db.session.commit()
        try:
            self.redis.delete(self.keys["cancel"])
        except Exception:
            pass


def _load_run(run_id: str, kind: str) -> Optional[VoucherSyncRun]:
    """Load an active run of the expected kind, or None if it is gone or finished."""
    run = db.session.get(VoucherSyncRun, run_id)
    if run is None:
        logger.warning("event=voucher_sync.run_missing run=%s kind=%s", run_id, kind)
        return None
    if run.kind != kind:
        logger.warning("event=voucher_sync.run_kind_mismatch run=%s expected=%s got=%s",
                       run_id, kind, run.kind)
        return None
    if run.status not in VoucherSyncRun.ACTIVE_STATUSES:
        logger.info("event=voucher_sync.run_not_active run=%s status=%s", run_id, run.status)
        return None
    return run


def _safe_error(exc: BaseException) -> str:
    """A short message safe to show the user: no tokens, no stack traces."""
    if isinstance(exc, InatAuthError):
        return str(exc)[:300]
    import requests
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"iNaturalist returned HTTP {exc.response.status_code}."
    if isinstance(exc, requests.RequestException):
        return f"Network error talking to iNaturalist ({type(exc).__name__})."
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _row_log_line(done: int, total: int, row: Dict[str, Any]) -> str:
    """Format one scanned row for the run log."""
    ocr_note = " [OCR]" if "ocr" in (row.get("reason") or "") else ""
    line = (f"[{done:>3}/{total}]  #{row.get('observation_id')}  "
            f"{(row.get('taxon') or '')[:36]}  ->  "
            f"{str(row.get('action', '')).upper()} ({row.get('reason')}){ocr_note}")
    if row.get("detected_voucher"):
        line += f"  |  {row['detected_voucher']}"
    return line


@background_job_context(0)
def run_voucher_scan_job(run_id: str) -> Dict[str, Any]:
    """Scan a user's observations and persist the review queue.

    Loads the run and the user's credential, verifies the token still belongs
    to the connected login, pages the observations, then decodes each photo.
    Rows stream to Redis as they finish; the final set is stored on the run.
    """
    from app.services import voucher_sync_service as vs
    from app.services.inat_user_credential_service import get_credential, get_user_jwt

    run = _load_run(run_id, "scan")
    if run is None:
        return {"status": "skipped"}
    ctx = _RunContext(run)
    run.status = "running"
    run.started_at = datetime.utcnow()
    db.session.commit()

    try:
        params = run.params or {}
        cred = get_credential(run.user_id)
        if cred is None or not cred.inat_login:
            raise InatAuthError("Connect your iNaturalist account first.")
        jwt = get_user_jwt(run.user_id)
        client = vs.INatClient(jwt)

        me = client.verify_token()
        if not me or not me.get("login"):
            raise InatAuthError("iNaturalist rejected the stored token; please reconnect.")
        if me["login"].lower() != (cred.inat_login or "").lower():
            raise InatAuthError("The connected iNaturalist account changed; please reconnect.")
        ctx.log(f"Authenticated as {me['login']}")

        use_ocr = bool(params.get("use_ocr", True))
        if use_ocr and not vs.ocr_engine_available():
            ctx.log("OCR engine is not installed on the server; scanning QR codes only.")
            use_ocr = False
        elif use_ocr:
            ctx.log("OCR fallback enabled.")

        d1, d2 = params.get("date_start"), params.get("date_end")
        window = d1 if d1 == d2 else f"{d1} to {d2}"
        ctx.log(f"Fetching observations for {me['login']} (uploaded {window})...")

        max_obs = int(_config("VOUCHER_SYNC_MAX_OBSERVATIONS", 2000))
        obs_list = list(client.fetch_observations(me["login"], d1, d2,
                                                  max_observations=max_obs))
        total = len(obs_list)
        if total >= max_obs:
            ctx.log(f"Reached the {max_obs}-observation cap; narrow the date range to scan the rest.")

        if ctx.cancelled():
            ctx.log("Preview stopped before scanning.")
            ctx.progress(0, total, force=True)
            ctx.finish("cancelled", rows=[], summary=vs.summarize_rows([]))
            return {"status": "cancelled"}
        if not total:
            ctx.log("No matching observations found.")
            ctx.progress(0, 0, force=True)
            ctx.finish("completed", rows=[], summary=vs.summarize_rows([]))
            return {"status": "completed", "total": 0}

        ctx.log(f"Found {total} observation(s). Scanning photos...")
        ctx.progress(0, total, force=True)

        voucher_re = re.compile(params["regex"], re.IGNORECASE)

        def on_row(done: int, total_: int, row: Dict[str, Any]) -> None:
            ctx.push_row(row)
            ctx.log(_row_log_line(done, total_, row))
            ctx.progress(done, total_)

        rows, cancelled = vs.scan_observations(
            client, obs_list,
            field_id=int(params["field_id"]),
            voucher_re=voucher_re,
            allow_overwrite=bool(params.get("allow_overwrite")),
            use_ocr=use_ocr,
            workers=int(_config("VOUCHER_SYNC_SCAN_WORKERS", 4)),
            on_row=on_row,
            should_cancel=ctx.cancelled,
        )
        rows.sort(key=lambda r: (r.get("upload_date") or "", r.get("observation_id") or 0))
        summary = vs.summarize_rows(rows)
        if cancelled:
            ctx.log(f"Preview stopped -- {len(rows)} of {total} observation(s) scanned.")
        else:
            ctx.log(f"Scan complete: {summary['update']} to update, "
                    f"{summary['skip']} to skip, {summary['flag']} flagged.")
        ctx.progress(len(rows), total, force=True)
        ctx.finish("cancelled" if cancelled else "completed", rows=rows, summary=summary)
        return {"status": run.status, "total": total, "summary": summary}
    except Exception as exc:
        logger.exception("event=voucher_sync.scan_failed run=%s", run_id)
        db.session.rollback()
        message = _safe_error(exc)
        try:
            ctx.log(f"ERROR: {message}")
            ctx.finish("failed", error=message)
        except Exception:
            logger.exception("event=voucher_sync.scan_finish_failed run=%s", run_id)
        return {"status": "failed", "error": message}
    finally:
        db.session.remove()


def _revalidate_targets(ctx, client, rows, field_id, allow_overwrite):
    """Re-read each target and drop the ones that are no longer safe to write.

    Returns the rows that survived. A row is dropped when the field has since
    been populated (and the user did not confirm an overwrite), or when the
    observation could not be re-read at all -- writing from stale preview data
    is exactly what this pass exists to prevent.
    """
    from app.services import voucher_sync_service as vs

    ids = [int(r["observation_id"]) for r in rows if r.get("observation_id") is not None]
    if not ids:
        return rows
    try:
        fresh_by_id = client.fetch_observations_by_id(ids)
    except Exception as exc:
        ctx.log(f"ERROR: could not re-read observations before applying "
                f"({type(exc).__name__}); nothing was written.")
        raise

    kept = []
    for r in rows:
        obs_id = int(r.get("observation_id") or 0)
        fresh = fresh_by_id.get(obs_id)
        if fresh is None:
            ctx.log(f"  SKIP  #{obs_id}  could not be re-read; not applying from stale data")
            continue
        value, ofv_id = vs.existing_ofv(fresh, field_id)
        r["ofv_id"] = ofv_id
        r["current_value"] = value
        r["field_state"] = "populated" if value else "empty"
        if value and not allow_overwrite:
            if str(value).strip().upper() == str(r.get("detected_voucher") or "").strip().upper():
                ctx.log(f"  SKIP  #{obs_id}  already holds {value}")
            else:
                ctx.log(f"  SKIP  #{obs_id}  now holds {value}; overwrite was not confirmed")
            continue
        kept.append(r)

    dropped = len(rows) - len(kept)
    if dropped:
        ctx.log(f"{dropped} row(s) changed on iNaturalist since the preview and were skipped.")
    return kept


@background_job_context(0)
def run_voucher_apply_job(run_id: str) -> Dict[str, Any]:
    """Write the confirmed rows of a finished preview back to iNaturalist.

    Every target is re-read first (see `_revalidate_targets`), so a value
    written since the preview is never silently overwritten.
    """
    from app.services import voucher_sync_service as vs
    from app.services.inat_user_credential_service import get_credential, get_user_jwt

    run = _load_run(run_id, "apply")
    if run is None:
        return {"status": "skipped"}
    ctx = _RunContext(run)
    run.status = "running"
    run.started_at = datetime.utcnow()
    db.session.commit()

    try:
        params = run.params or {}
        parent = db.session.get(VoucherSyncRun, run.parent_run_id) if run.parent_run_id else None
        if parent is None or parent.user_id != run.user_id or parent.kind != "scan":
            raise vs.VoucherSyncError("The preview this apply was based on is no longer available.")

        cred = get_credential(run.user_id)
        if cred is None:
            raise InatAuthError("Connect your iNaturalist account first.")
        jwt = get_user_jwt(run.user_id)
        client = vs.INatClient(jwt)
        me = client.verify_token()
        if not me or (me.get("login") or "").lower() != (cred.inat_login or "").lower():
            raise InatAuthError("iNaturalist rejected the stored token; please reconnect.")

        field_id = int(params.get("field_id") or parent.params.get("field_id"))
        allow_overwrite = bool(params.get("allow_overwrite"))
        selected = params.get("observation_ids")
        selected_set = {int(x) for x in selected} if selected else None

        parent_rows: List[Dict[str, Any]] = list(parent.rows or [])
        to_apply = [r for r in parent_rows
                    if r.get("action") == vs.UPDATE and r.get("detected_voucher")
                    and (selected_set is None or int(r.get("observation_id") or 0) in selected_set)]
        if not allow_overwrite:
            # Never write over a populated field unless the user confirmed it
            # at apply time, whatever the preview's overwrite switch said.
            to_apply = [r for r in to_apply if not r.get("current_value")]

        if ctx.cancelled():
            ctx.log("Apply cancelled before anything was written.")
            ctx.finish("cancelled", rows=[], summary={"applied": 0, "failed": 0, "total": 0})
            return {"status": "cancelled", "applied": 0, "failed": 0}

        # Revalidate EVERY target against iNaturalist before writing, not just
        # the ones the preview saw as populated. A preview can be minutes or
        # hours old; if anything wrote to the field in the meantime, applying
        # from preview data would silently overwrite the newer value without
        # the confirmation the overwrite gate exists to require. Batched by id,
        # so this costs a few requests rather than one per row.
        to_apply = _revalidate_targets(ctx, client, to_apply, field_id, allow_overwrite)

        total = len(to_apply)
        ctx.log(f"Applying {total} update(s) as {me['login']}...")
        ctx.progress(0, total, force=True)
        if not total:
            ctx.finish("completed", rows=[], summary={"applied": 0, "failed": 0, "total": 0})
            return {"status": "completed", "applied": 0, "failed": 0}

        if ctx.cancelled():
            ctx.log("Apply cancelled before anything was written.")
            ctx.finish("cancelled", rows=[], summary={"applied": 0, "failed": 0, "total": total})
            return {"status": "cancelled", "applied": 0, "failed": 0}

        def on_result(i: int, total_: int, row: Dict[str, Any], error: Optional[str]) -> None:
            if error:
                ctx.log(f"  FAIL  #{row['observation_id']}  {row['detected_voucher']}  --  {error}")
            else:
                ctx.log(f"  OK    #{row['observation_id']}  {row['detected_voucher']}")
            ctx.push_row(row)
            ctx.progress(i, total_)
            if ctx.cancelled():
                raise _ApplyCancelled()

        applied = failed = 0
        cancelled = False
        try:
            applied, failed = vs.apply_rows(
                client, to_apply, field_id=field_id, allow_overwrite=allow_overwrite,
                pause=float(_config("VOUCHER_SYNC_WRITE_PAUSE_SECONDS", 1.0)),
                on_result=on_result,
            )
        except _ApplyCancelled:
            cancelled = True
            applied = sum(1 for r in to_apply if r.get("reason") == "applied")
            failed = sum(1 for r in to_apply if str(r.get("reason", "")).startswith("apply_failed"))
            ctx.log(f"Apply stopped -- {applied} written, {failed} failed.")

        # Reflect results back into the parent preview so a reload shows
        # "applied" rows instead of offering them again.
        by_id = {r["observation_id"]: r for r in to_apply}
        merged = [by_id.get(r.get("observation_id"), r) for r in parent_rows]
        parent.rows = merged
        parent.summary = vs.summarize_rows(merged)

        summary = {"applied": applied, "failed": failed, "total": total}
        ctx.log(f"Apply {'stopped' if cancelled else 'complete'}: {applied} written, {failed} failed.")
        ctx.progress(applied + failed, total, force=True)
        ctx.finish("cancelled" if cancelled else "completed", rows=to_apply, summary=summary)
        return {"status": run.status, **summary}
    except Exception as exc:
        logger.exception("event=voucher_sync.apply_failed run=%s", run_id)
        db.session.rollback()
        message = _safe_error(exc)
        try:
            ctx.log(f"ERROR: {message}")
            ctx.finish("failed", error=message)
        except Exception:
            logger.exception("event=voucher_sync.apply_finish_failed run=%s", run_id)
        return {"status": "failed", "error": message}
    finally:
        db.session.remove()


class _ApplyCancelled(Exception):
    pass
