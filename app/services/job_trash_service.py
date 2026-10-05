"""Recoverable deletion of a user's jobs.

Clear All used to remove the rows and the directories in one request, with no
way back. It now only stamps ``Job.deleted_at``: the row and the files stay
where they are, the job disappears from the owner's list and from every job
URL (``check_job_access`` treats it as missing), and the owner can restore it
from "Recently deleted" for ``JOB_DELETE_GRACE_DAYS``. Only ``purge_jobs``
destroys anything, and it keeps the move-aside-then-commit ordering the old
Clear All used, so a failed commit never leaves a live row without its files.

Protected jobs (``Job.protected``) are never soft-deleted or purged here.
"""
import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.config import Config
from app.extensions import db
from app.models import Job
from app.services.security_utils import validate_job_id

logger = logging.getLogger(__name__)

JOB_DELETE_GRACE_DAYS = 30
# Purging runs inside a page load, so bound how much rmtree one request does.
PURGE_BATCH_LIMIT = 50


def purge_cutoff(now: datetime | None = None) -> datetime:
    return (now or datetime.utcnow()) - timedelta(days=JOB_DELETE_GRACE_DAYS)


def soft_delete_user_jobs(user_id: int) -> tuple[int, int]:
    """Mark every live, unprotected job of the user deleted.

    Returns ``(deleted, skipped_protected)``. The caller commits.
    """
    now = datetime.utcnow()
    live = Job.query.filter_by(user_id=user_id).filter(Job.deleted_at.is_(None))
    skipped = live.filter(Job.protected.is_(True)).count()
    deleted = (
        live.filter(Job.protected.is_(False))
        .update({Job.deleted_at: now}, synchronize_session=False)
    )
    return deleted, skipped


def _canonical_job_dir(job) -> Path | None:
    """Return a trusted canonical path, or None for a corrupt row."""
    if not validate_job_id(job.id):
        return None
    candidate = Config.JOB_DIR / job.id
    try:
        if Path(job.job_dir).resolve() != candidate.resolve():
            return None
        if not candidate.resolve().is_relative_to(Config.JOB_DIR.resolve()):
            return None
    except (OSError, TypeError, ValueError):
        return None
    return candidate


def purge_jobs(jobs) -> dict:
    """Permanently remove soft-deleted jobs: rows and directories.

    Each directory is moved into ``var/jobs/.trash`` first, the row deletions
    are committed, and only then are the moved copies reclaimed. A commit
    failure puts every staged directory back. Returns counts for reporting.
    """
    from app.services.log_context import log_degradation

    trash_root = Config.JOB_DIR / ".trash"
    staged_entries = []  # (job_id, source, staged)
    removed = 0
    recovery_required = 0
    for selected_job in jobs:
        # Refresh under a row lock: an owner may have restored/protected the
        # job since the expired-job query. Hold the lock through staging/commit.
        job = (Job.query.filter_by(id=selected_job.id).populate_existing()
               .with_for_update().first())
        if job is None:
            continue
        if job.deleted_at is None or job.protected:
            continue
        # Soft deletion does not cancel work. Automatic page-load/CLI purges
        # must wait for it to finish, rather than interrupting a user's job.
        if job.status not in ("completed", "failed"):
            logger.warning("event=jobs.purge_active_skipped job=%s status=%s",
                           job.id, job.status)
            continue
        candidate = _canonical_job_dir(job)
        if candidate is not None and any(trash_root.glob(f"{job.id}.*")):
            # A prior rollback may have left the only artifacts staged. Never
            # delete their surviving row on a retry, even if source is missing.
            recovery_required += 1
            logger.error("event=jobs.purge_recovery_required job=%s", job.id)
            continue
        if candidate is None:
            logger.warning(
                "event=jobs.purge_invalid_job_path job=%s dir=%s "
                "The row was removed; its invalid or non-canonical path was "
                "left untouched.",
                job.id, job.job_dir,
            )
        elif candidate.exists():
            try:
                trash_root.mkdir(parents=True, exist_ok=True)
                staged = trash_root / f"{job.id}.{int(time.time() * 1000)}"
                candidate.rename(staged)
                staged_entries.append((job.id, candidate, staged))
            except OSError as e:
                # Nothing destroyed yet; keep the row so the purge is retried.
                logger.warning(
                    "event=jobs.purge_stage_failed job=%s error=%s",
                    job.id, type(e).__name__,
                )
                continue
        db.session.delete(job)
        removed += 1

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        for job_id, source, staged in staged_entries:
            try:
                staged.rename(source)
            except OSError:
                recovery_required += 1
                logger.exception(
                    "event=jobs.purge_restore_failed job=%s staged=%s source=%s "
                    "Move staged back to source to complete the rollback.",
                    job_id, staged, source,
                )
        logger.exception("event=jobs.purge_commit_failed error=%s", type(e).__name__)
        return {"removed": 0, "files_left": recovery_required, "failed": True,
                "recovery_required": recovery_required}

    files_left = 0
    for job_id, _source, staged in staged_entries:
        failure = None
        try:
            if staged.is_dir():
                shutil.rmtree(staged)
            else:
                staged.unlink(missing_ok=True)
        except OSError as exc:
            failure = type(exc).__name__
        if failure or staged.exists():
            files_left += 1
            log_degradation(
                logger, "job_trash_cleanup_failed",
                "Purged job files could not be removed from var/jobs/.trash",
                job_id=job_id, staged=str(staged),
                error=failure or "still_present",
            )
    result = {"removed": removed, "files_left": files_left, "failed": False}
    if recovery_required:
        result["recovery_required"] = recovery_required
    return result


def expired_deleted_jobs(user_id: int | None = None, limit: int | None = None):
    q = Job.query.filter(
        Job.deleted_at.isnot(None),
        Job.deleted_at < purge_cutoff(),
        Job.protected.is_(False),
    )
    if user_id is not None:
        q = q.filter_by(user_id=user_id)
    q = q.order_by(Job.deleted_at)
    if limit:
        q = q.limit(limit)
    return q.all()
