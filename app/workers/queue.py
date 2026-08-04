import uuid

import redis
from rq import Queue, Retry
from flask import current_app
from typing import Any, Dict, Optional

QUEUE_HIGH = "phylo_high"
QUEUE_BULK = "phylo_bulk"
VALID_QUEUE_NAMES = {QUEUE_HIGH, QUEUE_BULK}

def get_redis_connection():
    redis_url = current_app.config.get('REDIS_URL', 'redis://localhost:6379/0')
    return redis.from_url(redis_url)

def get_queue(name=QUEUE_HIGH) -> Queue:
    if name not in VALID_QUEUE_NAMES:
        name = QUEUE_HIGH
    conn = get_redis_connection()
    return Queue(name, connection=conn)

def enqueue_job(job_params: Dict[str, Any], queue_name: str = QUEUE_HIGH,
                meta: Optional[Dict[str, Any]] = None,
                job_id: Optional[str] = None,
                job_timeout: Any = '1h') -> str:
    """Enqueue a phylo analysis job and return the job ID."""
    q = get_queue(queue_name)
    from app.workers.tasks import run_phylo_job
    job = q.enqueue(
        run_phylo_job,
        job_params,
        job_timeout=job_timeout,
        meta=meta or {},
        job_id=job_id,
    )
    return job.id


def enqueue_mycomap_blast_refresh_job(params: Dict[str, Any], job_timeout: Any = '1h') -> str:
    """Enqueue a MycoMap BLAST refresh (not a full pipeline job) and return its job ID."""
    q = get_queue(QUEUE_HIGH)
    from app.workers.tasks import run_mycomap_blast_refresh_job
    job_id = str(uuid.uuid4())
    job = q.enqueue(
        run_mycomap_blast_refresh_job,
        params,
        job_timeout=job_timeout,
        meta={},
        job_id=job_id,
    )
    return job.id


def enqueue_recompute_job(job_id: str, params_dict: Dict[str, Any]) -> str:
    """Enqueue a recompute job under the existing job ID so status pages stream normally."""
    q = get_queue(QUEUE_HIGH)
    from app.workers.events import (
        STEP_INPUT, STEP_ORIENT, STEP_BLAST,
        STATE_QUEUED, STATE_SKIPPED, get_initial_steps_meta,
    )
    from app.workers.tasks import run_recompute_job

    steps = get_initial_steps_meta()
    steps[STEP_INPUT] = {"label": "Sequence Queue", "state": STATE_QUEUED}
    steps[STEP_ORIENT] = {"label": "Orientation Check (skipped)", "state": STATE_SKIPPED}
    steps[STEP_BLAST] = {"label": "BLAST Search (skipped)", "state": STATE_SKIPPED}

    job = q.enqueue_call(
        run_recompute_job,
        args=(job_id, params_dict),
        timeout='1h',
        job_id=job_id,
        meta={
            "steps": steps,
            "current_step": None,
            "current_tool": None,
            "recompute": True,
        }
    )
    return job.id

def get_job_status(job_id: str) -> Dict[str, Any]:
    """Return a dict with at least: id, status, error (optional), progress (optional)."""
    try:
        q = get_queue()
        job = q.fetch_job(job_id)
        
        if job is None:
            return {"id": job_id, "status": "unknown", "error": "Job not found"}
        
        status = job.get_status()
        if status in ("scheduled", "deferred"):
            status = "queued"
        result = job.result
        
        response = {
            "id": job_id,
            "status": status,
            "enqueued_at": job.enqueued_at.isoformat() if job.enqueued_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "ended_at": job.ended_at.isoformat() if job.ended_at else None,
        }
        
        if status == "failed" and job.exc_info:
            response["error"] = job.exc_info
            
        # RQ stores a Retry marker as the result while a job waits to be
        # scheduled again. It is internal state and cannot be JSON encoded.
        if result and not isinstance(result, Retry):
            response["result"] = result
            
        return response
        
    except Exception as e:
        return {"id": job_id, "status": "error", "error": str(e)}
