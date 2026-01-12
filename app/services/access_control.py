from typing import Optional, Tuple
from flask_login import current_user
from app.config import Config
from app.models import Job
from app.services.security_utils import validate_job_id

def check_job_access(job_id: str) -> Tuple[Optional[Job], Optional[str], int]:
    """
    Check if current user can access the given job.
    
    Returns:
        (db_job, error_message, status_code)
        
        If access is granted: (db_job, None, 200)
        If error: (None, error_message, status_code)
    """
    if not validate_job_id(job_id):
        return None, "Invalid job ID format", 400

    db_job = Job.query.get(job_id)
    job_dir = Config.JOB_DIR / job_id
    
    # Check if job exists
    if not db_job and not job_dir.exists():
        return None, "Job not found", 404
    
    # If job has an owner, verify the current user is that owner
    if db_job and db_job.user_id is not None:
        # ALLOW PUBLIC SHARING:
        # We intentionally allow anyone with the link (job_id) to view the tree.
        # Finished trees generally aren't secret, and people should be able to share them easily.
        # The only check strictly required here would be if we wanted to prevent *editing* (pruning/rerooting)
        # by non-owners, but for now, the "view" mode implies full interaction.
        # OLD STRICT CHECK REMOVED:
        # if not current_user.is_authenticated:
        #     return None, "Authentication required", 401
        # if current_user.id != db_job.user_id:
        #     return None, "Access denied", 403
        pass
    
    return db_job, None, 200
