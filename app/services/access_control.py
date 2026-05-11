from typing import Optional, Tuple
from flask_login import current_user
from app.config import Config
from app.models import Job
from app.services.security_utils import validate_job_id

def check_job_access(job_id: str, mode: str = "view") -> Tuple[Optional[Job], Optional[str], int]:
    """
    Check if current user can access the given job.
    
    Args:
        job_id: The job ID to check.
        mode: "view" (default) or "edit".
        
    Returns:
        (db_job, error_message, status_code)
        
        If access is granted: (db_job, None, 200)
        If error: (None, error_message, status_code)
    """
    if not validate_job_id(job_id):
        return None, "Invalid job ID format", 400

    db_job = Job.query.get(job_id)
    job_dir = Config.JOB_DIR / job_id
    
    # Check if job exists (either in DB or on disk)
    if not db_job and not job_dir.exists():
        return None, "Job not found", 404
    
    if mode == "view":
        # Public View Access:
        # Anyone with the link (job_id) can view the tree.
        return db_job, None, 200

    elif mode == "edit":
        # Edit Access policy (intentional, do not "fix" without product sign-off):
        #
        # 1. If a job has an owner (user_id is set), ONLY that owner can edit it.
        # 2. If a job has no owner (anonymous job: user_id is NULL, or DB row
        #    missing but job_dir exists on disk), ANYONE with the job UUID can
        #    edit it. The UUID itself is treated as a capability token.
        #
        # Rationale: Dikarya allows fully anonymous use. Anonymous users can
        # create, edit, prune, reroot, rename, and recompute their own jobs
        # without signing up. The tradeoff they accept is that the job link
        # IS the access control — anyone with the URL can also edit. If a
        # user wants exclusive edit rights, they should create an account
        # and log in before submitting the job; the job will then be owned
        # by that account and edits will be locked to them.
        #
        # Abuse on expensive endpoints (e.g. /tree/recompute) is mitigated
        # separately via per-route rate limits, not by tightening this policy.
        if db_job and db_job.user_id is not None:
            if not current_user.is_authenticated:
                return None, "Authentication required to edit this job", 401
            if current_user.id != db_job.user_id:
                return None, "You do not have permission to edit this job", 403

        # Anonymous / legacy job: allow edit by anyone holding the UUID.
        return db_job, None, 200

    return None, f"Invalid access mode: {mode}", 400
