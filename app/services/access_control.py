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
        # Edit Access:
        # If job has an owner, ONLY that owner can edit.
        if db_job and db_job.user_id is not None:
            if not current_user.is_authenticated:
                return None, "Authentication required to edit this job", 401
            if current_user.id != db_job.user_id:
                return None, "You do not have permission to edit this job", 403
        
        # If job has no owner (db_job.user_id is None) OR (db_job is None but dir exists):
        # Allow anonymous edits (Legacy behavior for anonymous jobs)
        return db_job, None, 200

    return None, f"Invalid access mode: {mode}", 400
