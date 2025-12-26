import redis
from rq import Queue
from flask import current_app
from typing import Any, Dict, Optional

def get_redis_connection():
    redis_url = current_app.config.get('REDIS_URL', 'redis://localhost:6379/0')
    return redis.from_url(redis_url)

def get_queue(name='phylo_jobs') -> Queue:
    conn = get_redis_connection()
    return Queue(name, connection=conn)

def enqueue_job(job_params: Dict[str, Any]) -> str:
    """Enqueue a phylo analysis job and return the job ID."""
    q = get_queue()
    from app.workers.tasks import run_phylo_job
    job = q.enqueue(run_phylo_job, job_params, job_timeout='1h')
    return job.id

def get_job_status(job_id: str) -> Dict[str, Any]:
    """Return a dict with at least: id, status, error (optional), progress (optional)."""
    try:
        q = get_queue()
        job = q.fetch_job(job_id)
        
        if job is None:
            return {"id": job_id, "status": "unknown", "error": "Job not found"}
        
        status = job.get_status()
        result = job.result
        
        response = {
            "id": job_id,
            "status": status,
            "enqueued_at": job.enqueued_at.isoformat() if job.enqueued_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "ended_at": job.ended_at.isoformat() if job.ended_at else None,
        }
        
        if job.exc_info:
            response["error"] = job.exc_info
            
        if result:
            response["result"] = result
            
        return response
        
    except Exception as e:
        return {"id": job_id, "status": "error", "error": str(e)}
