"""Reconcile PostgreSQL job status against RQ's public job APIs.

The pipeline normally updates PostgreSQL itself.  A worker/workhorse death can
prevent that final update, leaving a DB row queued/running after RQ has made the
job terminal.  Reconciliation is intentionally conservative: automatic retry
requires RQ's positive, persisted evidence of an unexpected workhorse death.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.services.artifact_storage import discard_artifact, gz_path

logger = logging.getLogger(__name__)

RQ_DEAD_STATUSES = {"failed", "stopped", "canceled"}
RQ_LIVE_STATUSES = {"created", "queued", "started", "deferred", "scheduled"}
RQ_FINISHED_STATUS = "finished"

MISSING_RECORD_GRACE = timedelta(hours=1)
MAX_RESTART_REQUEUES = 1

# RQ 2.6.1 persists these two unexpected-termination cases in the public
# Result.exc_string field.  Normal Python exceptions have a traceback instead;
# intentional stops say "Job stopped by user".  Do not broaden these markers to
# missing/empty exception data: absence is ambiguous and must never cause retry.
_UNEXPECTED_INTERRUPTION_MARKERS = (
    "Work-horse terminated unexpectedly;",
    "Moved to FailedJobRegistry, due to AbandonedJobError, at ",
)


@dataclass
class RQJobInspection:
    """One verified RQ lookup, keeping missing separate from lookup failure."""

    verified: bool
    status: Optional[str] = None
    rq_job: Any = None
    missing: bool = False
    error: Optional[str] = None


def inspect_rq_job(redis_conn, job_id: str) -> RQJobInspection:
    """Fetch an RQ job and status through supported APIs.

    ``missing=True`` means RQ positively reported that no Job exists.  It is
    deliberately distinct from ``verified=False``, which means Redis/RQ could
    not be queried reliably and no destructive action is safe.
    """
    try:
        from rq.exceptions import NoSuchJobError
        from rq.job import Job

        try:
            rq_job = Job.fetch(job_id, connection=redis_conn)
        except NoSuchJobError:
            return RQJobInspection(verified=True, missing=True)

        raw_status = rq_job.get_status(refresh=True)
        status = raw_status.value if hasattr(raw_status, "value") else str(raw_status)
        return RQJobInspection(verified=True, status=status, rq_job=rq_job)
    except Exception as exc:
        logger.exception("RQ lookup failed for job %s", job_id)
        return RQJobInspection(verified=False, error=str(exc))


def classify_reap_candidate(
    inspection: RQJobInspection, already_reconciled: bool = False
) -> str:
    """Classify one old DB row without allowing age to override RQ state."""
    if already_reconciled:
        return "ordinary_rq"
    if not inspection.verified:
        return "unverified"
    if inspection.status in RQ_LIVE_STATUSES:
        return "live"
    if (
        inspection.status == RQ_FINISHED_STATUS
        or inspection.status in RQ_DEAD_STATUSES
    ):
        return "ordinary_rq"
    if inspection.missing:
        return "reap"
    return "unverified"


def _rq_was_killed(rq_job) -> bool:
    """Return True only for RQ's positive unexpected-workhorse evidence."""
    try:
        from rq.results import Result

        result = rq_job.latest_result()
        if result is None or result.type != Result.Type.FAILED:
            return False
        exc_string = result.exc_string or ""
        return any(
            exc_string.startswith(marker)
            for marker in _UNEXPECTED_INTERRUPTION_MARKERS
        )
    except Exception:
        # Failure to read a result is ambiguity, not evidence of interruption.
        logger.exception("Could not inspect the RQ result for job %s", rq_job.id)
        return False


def _requeue_killed_job(db_job, rq_job) -> Optional[str]:
    """Safely reset partial output and use RQ's origin-preserving requeue API."""
    from app.config import Config
    from app.workers.queue import VALID_QUEUE_NAMES

    origin = str(rq_job.origin or "")
    if origin not in VALID_QUEUE_NAMES:
        return f"original RQ queue {origin!r} is not a recognized Dikarya queue"

    input_info = Config.JOB_DIR / db_job.id / "input_info.json"
    if not input_info.is_file():
        return "original inputs are no longer on disk"

    # A killed tool can leave a zero-byte file that a rerun might mistake for
    # completed output.  Keep input_info.json, input FASTA, RQ args and all
    # provenance; remove only the same derived alignment artifacts as before.
    for relative in ("alignment/alignment_raw.fasta", "alignment/alignment_trimmed.fasta"):
        stale = Config.JOB_DIR / db_job.id / relative
        try:
            # discard_artifact clears the gzipped form too, so a requeued job
            # cannot pick up a compressed copy of the alignment it is about to
            # rebuild.
            discard_artifact(stale)
            # discard_artifact logs and swallows OSError rather than raising, so
            # its return value says nothing about whether the file is gone.
            # Check both forms directly: this removal *is* the safety mechanism.
            # A SIGKILLed tool can leave a truncated or zero-byte alignment that
            # a rerun would treat as finished output, producing a tree from a
            # partial matrix. If we cannot confirm the stale file is gone, do not
            # requeue -- return a reason so reconciliation fails the job and
            # tells the operator why it was not retried.
            leftover = next(
                (c for c in (stale, gz_path(stale)) if c.exists()), None,
            )
        except Exception as exc:
            logger.warning(
                "Could not remove stale artifact %s: %s", stale, exc, exc_info=True,
            )
            return (
                f"stale alignment output {relative} could not be removed "
                f"({type(exc).__name__}), so rerunning it could reuse partial "
                f"results from the interrupted run"
            )
        if leftover is not None:
            logger.warning(
                "Stale artifact %s survived cleanup; refusing to requeue job %s",
                leftover, db_job.id,
            )
            return (
                f"stale alignment output {relative} could not be removed, so "
                f"rerunning it could reuse partial results from the interrupted run"
            )

    try:
        # Public RQ failed-job machinery preserves ID, callable, args, metadata,
        # timeout and (critically) rq_job.origin, so BULK stays BULK.
        rq_job.requeue()
    except Exception as exc:
        logger.exception("Could not requeue interrupted RQ job %s", db_job.id)
        return str(exc)
    return None


def reconcile_job_statuses(
    dry_run: bool = False,
    missing_record_grace: timedelta = MISSING_RECORD_GRACE,
    reconcile_missing_records: bool = True,
) -> List[Dict[str, Any]]:
    """Reconcile non-terminal DB rows with verified terminal RQ states."""
    from app.extensions import db
    from app.models import Job
    from app.workers.queue import get_redis_connection

    candidates = (
        Job.query.filter(Job.status.in_(("queued", "running")))
        .order_by(Job.created_at)
        .all()
    )
    if not candidates:
        # Nothing to do is the common case on a healthy queue, and it is also the
        # path that ran every maintenance cycle without ever ending the read
        # transaction the query above started. See the rollback at the end.
        db.session.rollback()
        return []

    try:
        redis_conn = get_redis_connection()
    except Exception:
        logger.exception("Could not reach Redis; skipping job reconciliation.")
        db.session.rollback()
        return []

    now = datetime.utcnow()
    changed: List[Dict[str, Any]] = []

    for job in candidates:
        previous_status = job.status
        inspection = inspect_rq_job(redis_conn, job.id)
        if not inspection.verified:
            continue

        status = inspection.status
        if status in RQ_LIVE_STATUSES:
            continue

        if status == RQ_FINISHED_STATUS:
            reason = (
                f"RQ completed this job but the database still had '{previous_status}'."
            )
            entry = {
                "job_id": job.id,
                "from_status": previous_status,
                "rq_status": status,
                "action": "completed",
                "reason": reason,
            }
            changed.append(entry)
            if not dry_run:
                metrics = dict(job.metrics or {})
                metrics["reconciled_at"] = now.isoformat()
                metrics["reconciled_from_status"] = previous_status
                metrics["reconciled_rq_status"] = status
                metrics["reconciled_reason"] = reason
                job.metrics = metrics
                job.status = "completed"
                job.updated_at = now
            continue

        if status in RQ_DEAD_STATUSES:
            # Canceled/stopped are always terminal here.  RQ's normal
            # unexpected-workhorse paths use FAILED; never reinterpret an
            # intentional terminal state from missing metadata.
            killed = status == "failed" and _rq_was_killed(inspection.rq_job)
            attempts = int((job.metrics or {}).get("restart_requeue_count") or 0)

            if killed and attempts < MAX_RESTART_REQUEUES:
                if dry_run:
                    changed.append({
                        "job_id": job.id,
                        "from_status": previous_status,
                        "rq_status": status,
                        "action": "would_requeue",
                        "reason": "RQ recorded an unexpected workhorse termination.",
                        "queue": inspection.rq_job.origin,
                    })
                    continue

                failure = _requeue_killed_job(job, inspection.rq_job)
                if failure is None:
                    metrics = dict(job.metrics or {})
                    metrics["restart_requeue_count"] = attempts + 1
                    metrics["requeued_at"] = now.isoformat()
                    metrics["requeued_queue"] = inspection.rq_job.origin
                    metrics["interrupted_reason"] = (
                        "This job was interrupted by a server restart before it "
                        "finished, and has been automatically resubmitted. No action "
                        "is needed; it will run again from the beginning."
                    )
                    metrics.pop("reconciled_reason", None)
                    job.metrics = metrics
                    job.status = "queued"
                    job.updated_at = now
                    changed.append({
                        "job_id": job.id,
                        "from_status": previous_status,
                        "rq_status": status,
                        "action": "requeued",
                        "reason": "RQ recorded an unexpected workhorse termination; resubmitted once.",
                        "queue": inspection.rq_job.origin,
                    })
                    continue
                logger.warning("Could not requeue interrupted job %s: %s", job.id, failure)
                reason = (
                    "This job was interrupted by a server restart before it "
                    f"finished, and could not be resubmitted automatically ({failure}). "
                    "Please submit it again."
                )
            elif killed and attempts >= MAX_RESTART_REQUEUES:
                reason = (
                    "This job was interrupted by a server restart and was already "
                    "retried once, so it has not been resubmitted again. Please "
                    "submit it again, and let us know if it keeps failing."
                )
            elif status == "canceled":
                reason = "RQ reports that this job was canceled; it was not resubmitted."
            elif status == "stopped":
                reason = "RQ reports that this job was stopped; it was not resubmitted."
            else:
                reason = (
                    f"RQ reported this job as '{status}' but the database still had "
                    f"'{previous_status}'. It was not retried because RQ recorded no "
                    "unexpected-workhorse evidence."
                )
        elif inspection.missing:
            if not reconcile_missing_records:
                continue
            activity_at = job.updated_at or job.created_at or now
            age = now - activity_at
            if age < missing_record_grace:
                continue
            reason = (
                f"RQ has no record of this job and the database still had "
                f"'{previous_status}' after {str(age).split('.')[0]}. Treating it as dead."
            )
        else:
            logger.info("Job %s has unrecognised RQ status %r; leaving alone.", job.id, status)
            continue

        entry = {
            "job_id": job.id,
            "from_status": previous_status,
            "rq_status": status,
            "action": "failed",
            "reason": reason,
        }
        changed.append(entry)

        if dry_run:
            continue

        metrics = dict(job.metrics or {})
        metrics["reconciled_at"] = now.isoformat()
        metrics["reconciled_from_status"] = previous_status
        metrics["reconciled_rq_status"] = status
        metrics["reconciled_reason"] = reason
        if not metrics.get("error"):
            metrics["interrupted_reason"] = reason
        job.metrics = metrics
        job.status = "failed"
        job.updated_at = now

    if changed and not dry_run:
        # This now runs on every RQ maintenance pass inside the worker's
        # single lifetime-long session, not just once at startup. Without the
        # rollback, one failed commit leaves that session in a
        # rollback-required state and every later DB access in the worker main
        # process raises PendingRollbackError until the worker is restarted.
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception(
                "Could not commit reconciliation of %d job(s); rolled back.",
                len(changed),
            )
            raise
        logger.info("Reconciled %d job(s) from RQ into the database.", len(changed))
    else:
        # Alan 8/15/26 - Close the read transaction the Job.query above opened.
        #
        # The worker calls this every RQ maintenance cycle inside one app context
        # held for the life of the process, so with no commit on the quiet path the
        # session's transaction stayed open forever: production showed the worker
        # connection "idle in transaction" for 4h45m. That holds ACCESS SHARE on
        # jobs, which is enough to block a `flask db upgrade` ALTER TABLE behind it.
        #
        # This also un-stages a dry run. --dry-run still assigns job.status/metrics
        # above and merely skips the commit, leaving dirty objects in the session
        # for whatever commits next; rolling back makes "dry" actually mean dry.
        db.session.rollback()

    return changed


def count_jobs_in_flight() -> Dict[str, Any]:
    """Report jobs RQ still considers live for worker-restart preflight."""
    from app.models import Job
    from app.workers.queue import get_redis_connection

    candidates = (
        Job.query.filter(Job.status.in_(("queued", "running")))
        .order_by(Job.created_at)
        .all()
    )
    if not candidates:
        return {"in_flight": [], "unknown": []}

    try:
        redis_conn = get_redis_connection()
    except Exception:
        logger.exception("Could not reach Redis; cannot determine in-flight jobs.")
        return {"in_flight": [], "unknown": [j.id for j in candidates]}

    in_flight, unknown = [], []
    for job in candidates:
        inspection = inspect_rq_job(redis_conn, job.id)
        if inspection.verified and inspection.status in RQ_LIVE_STATUSES:
            in_flight.append({
                "job_id": job.id,
                "db_status": job.status,
                "rq_status": inspection.status,
                "created_at": job.created_at.isoformat() if job.created_at else None,
            })
        elif not inspection.verified or inspection.missing:
            unknown.append(job.id)
    return {"in_flight": in_flight, "unknown": unknown}
