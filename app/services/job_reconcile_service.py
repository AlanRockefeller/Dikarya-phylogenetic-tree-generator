"""
Reconcile Postgres job status against RQ.

`run_phylo_job` only marks a job failed from inside an `except Exception` block.
A work horse that is *killed* -- by `systemctl restart dikarya-worker`, the OOM
killer, or RQ's death penalty -- never reaches that handler, so RQ records the
job as failed while the Postgres row stays `queued`/`running` forever.

Those rows are not merely untidy. Each non-terminal job keeps its SSE stream
alive, and every open stream holds one of the (workers x threads) request slots,
so enough of them will make the whole site stop responding.

This module closes the gap by treating RQ as the source of truth for liveness.
It is deliberately conservative: a job is only marked failed when RQ positively
says it is dead, or when RQ has forgotten it entirely *and* it is old enough that
it cannot still be pending.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# RQ statuses that mean "this job will never run again".
RQ_DEAD_STATUSES = {"failed", "stopped", "canceled"}

# RQ statuses that mean "still legitimately in flight" -- never touch these.
RQ_LIVE_STATUSES = {"queued", "started", "deferred", "scheduled"}

# If RQ has no record at all (the key expired), only treat the job as dead once
# it is older than this. Guards against reconciling a job in the split second
# between the DB row being written and RQ registering it.
MISSING_RECORD_GRACE = timedelta(hours=1)

# How many times a job may be automatically resubmitted after being *killed*
# (worker restart, OOM). Deliberately 1: a job that dies deterministically -- an
# OOM on an alignment too big for this host, say -- would otherwise thrash,
# burning a full pipeline run each time and potentially taking the worker with it.
MAX_RESTART_REQUEUES = 1


def _rq_was_killed(redis_conn, job_id: str) -> bool:
    """True when RQ recorded a death with no Python traceback.

    RQ stores `exc_info` when a task raises. A work horse that is SIGKILLed --
    `systemctl restart dikarya-worker`, the OOM killer, RQ's death penalty --
    dies without raising, so RQ marks the job failed with no exc_info. That
    absence is the signal that the job was interrupted rather than genuinely
    broken, and is the only case worth retrying automatically: a real exception
    will just happen again.
    """
    try:
        raw = redis_conn.hget(f"rq:job:{job_id}", "exc_info")
    except Exception:
        logger.exception("Redis exc_info lookup failed for job %s", job_id)
        return False
    if raw is None:
        return True
    # Stored value may be zlib-compressed; either way, non-empty means a real
    # traceback was recorded.
    return not bytes(raw).strip()


def _requeue_killed_job(job, now: datetime) -> Optional[str]:
    """Resubmit a job that was killed mid-run. Returns an error string on failure.

    Re-enqueues under the SAME id so the user's status URL keeps working. There
    is no resume logic in the pipeline (and a killed job can leave a 0-byte
    alignment behind), so this deliberately re-runs from the original inputs.
    """
    import json
    from app.config import Config
    from app.workers.queue import enqueue_job

    input_info = Config.JOB_DIR / job.id / "input_info.json"
    if not input_info.exists():
        return "original inputs are no longer on disk"
    try:
        with open(input_info) as handle:
            job_params = json.load(handle)
    except Exception as exc:
        return f"could not read stored inputs ({exc})"

    if not (job_params.get("sequence") or job_params.get("accessions")):
        return "stored inputs contain no sequences or accessions"

    # Clear derived artifacts so a half-written file (e.g. the 0-byte
    # alignment_raw.fasta a killed MAFFT leaves) can't be mistaken for output.
    for relative in ("alignment/alignment_raw.fasta", "alignment/alignment_trimmed.fasta"):
        stale = Config.JOB_DIR / job.id / relative
        try:
            if stale.exists():
                stale.unlink()
        except Exception:
            logger.warning("Could not remove stale artifact %s", stale)

    enqueue_job(job_params, job_id=job.id)
    return None


def _rq_status(redis_conn, job_id: str) -> Optional[str]:
    """Return the RQ status string for a job id, or None if RQ has no record.

    The RQ job id and the Postgres Job.id are the same value on every enqueue
    path, so a direct key lookup is enough and avoids loading the full job.
    """
    try:
        raw = redis_conn.hget(f"rq:job:{job_id}", "status")
    except Exception:
        logger.exception("Redis lookup failed for job %s", job_id)
        return None
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


def reconcile_job_statuses(
    dry_run: bool = False,
    missing_record_grace: timedelta = MISSING_RECORD_GRACE,
) -> List[Dict[str, Any]]:
    """Mark DB jobs failed when RQ says they are dead.

    Returns a list of dicts describing what was (or would be) changed. Safe to
    call repeatedly; jobs still live in RQ are left alone.
    """
    from app.extensions import db
    from app.models import Job
    from app.workers.queue import get_redis_connection

    candidates = (
        Job.query
        .filter(Job.status.in_(("queued", "running")))
        .order_by(Job.created_at)
        .all()
    )
    if not candidates:
        return []

    try:
        redis_conn = get_redis_connection()
    except Exception:
        logger.exception("Could not reach Redis; skipping job reconciliation.")
        return []

    now = datetime.utcnow()
    changed: List[Dict[str, Any]] = []

    for job in candidates:
        status = _rq_status(redis_conn, job.id)

        if status in RQ_LIVE_STATUSES:
            continue

        if status in RQ_DEAD_STATUSES:
            killed = _rq_was_killed(redis_conn, job.id)
            attempts = int((job.metrics or {}).get("restart_requeue_count") or 0)

            if killed and attempts < MAX_RESTART_REQUEUES and not dry_run:
                failure = _requeue_killed_job(job, now)
                if failure is None:
                    metrics = dict(job.metrics or {})
                    metrics["restart_requeue_count"] = attempts + 1
                    metrics["requeued_at"] = now.isoformat()
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
                        "from_status": "running",
                        "rq_status": status,
                        "action": "requeued",
                        "reason": "Killed mid-run (no traceback recorded); resubmitted once.",
                    })
                    continue
                logger.warning("Could not requeue killed job %s: %s", job.id, failure)
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
            elif killed:
                # dry run
                reason = "Killed mid-run; would be resubmitted."
            else:
                reason = (
                    f"RQ reported this job as '{status}' but the database still had "
                    f"'{job.status}'. The job stopped without recording a failure."
                )
        elif status is None:
            age = now - (job.created_at or now)
            if age < missing_record_grace:
                # Too new to judge -- RQ may not have registered it yet.
                continue
            reason = (
                f"RQ has no record of this job and the database still had "
                f"'{job.status}' after {str(age).split('.')[0]}. Treating it as dead."
            )
        else:
            # An RQ status we don't recognise; leave it rather than guess.
            logger.info("Job %s has unrecognised RQ status %r; leaving alone.",
                        job.id, status)
            continue

        entry = {
            "job_id": job.id,
            "from_status": job.status,
            "rq_status": status,
            "action": "failed",
            "reason": reason,
        }
        changed.append(entry)

        if dry_run:
            continue

        metrics = dict(job.metrics or {})
        metrics["reconciled_at"] = now.isoformat()
        metrics["reconciled_from_status"] = job.status
        metrics["reconciled_rq_status"] = status
        metrics["reconciled_reason"] = reason
        # The status page has no 'error' to show for a job that died without
        # raising, so give it one the user can actually act on.
        if not metrics.get("error"):
            metrics["interrupted_reason"] = reason
        job.metrics = metrics
        job.status = "failed"
        job.updated_at = now

    if changed and not dry_run:
        db.session.commit()
        logger.info("Reconciled %d job(s) from RQ into the database.", len(changed))

    return changed


def count_jobs_in_flight() -> Dict[str, Any]:
    """Report jobs RQ still considers live.

    Used as a preflight before restarting the worker: restarting kills the work
    horse, and anything mid-run is lost.
    """
    from app.models import Job
    from app.workers.queue import get_redis_connection

    candidates = (
        Job.query
        .filter(Job.status.in_(("queued", "running")))
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
        status = _rq_status(redis_conn, job.id)
        if status in RQ_LIVE_STATUSES:
            in_flight.append({
                "job_id": job.id,
                "db_status": job.status,
                "rq_status": status,
                "created_at": job.created_at.isoformat() if job.created_at else None,
            })
        elif status is None:
            unknown.append(job.id)
    return {"in_flight": in_flight, "unknown": unknown}
