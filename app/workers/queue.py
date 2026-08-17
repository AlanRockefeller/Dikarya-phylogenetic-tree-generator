import uuid
import logging

import redis
from rq import Queue, Retry
from flask import current_app
from typing import Any, Dict, Optional

QUEUE_HIGH = "phylo_high"
QUEUE_BULK = "phylo_bulk"
VALID_QUEUE_NAMES = {QUEUE_HIGH, QUEUE_BULK}
logger = logging.getLogger(__name__)

def get_redis_connection():
    redis_url = current_app.config.get('REDIS_URL', 'redis://localhost:6379/0')
    return redis.from_url(redis_url)

def get_queue(name=QUEUE_HIGH) -> Queue:
    if name not in VALID_QUEUE_NAMES:
        name = QUEUE_HIGH
    conn = get_redis_connection()
    return Queue(name, connection=conn)

def resolve_job_timeout(job_params: Dict[str, Any]) -> str:
    """Pick the RQ job_timeout for a job based on the tree method it will run.

    RAxML-NG needs longer than the already-generous general allowance. This
    lives here, rather than
    at each submission site, so every path that creates a job (web /tree, API
    v1, iNaturalist auto-tree, Mushroom Observer, rebuild) gets the same budget
    and a new entry point cannot silently fall back to one hour.
    """
    from app.config import Config

    if str((job_params or {}).get("tree_method") or "").lower() == "raxml":
        hours = float(getattr(Config, "RAXML_TIME_LIMIT_HOURS", 15) or 15)
        # A little above the tool's own limit so the subprocess timeout fires
        # first and produces the specific "RAxML ran out of time" message
        # instead of RQ killing the horse with no explanation.
        return f"{int(hours * 3600) + 600}s"

    hours = float(getattr(Config, "GENERAL_JOB_TIME_LIMIT_HOURS", 8) or 8)
    # Generic subprocess CPU limiting is disabled by default because CPU time
    # accumulates across threads. RQ remains the ordinary wall-clock guard.
    return f"{int(hours * 3600) + 600}s"


def safe_job_description(kind: str, job_params: Optional[Dict[str, Any]] = None,
                         job_id: Optional[str] = None) -> str:
    """Return the one-line string RQ prints for a job, with no user payload in it.

    Alan 8/15/26 - RQ's default description is get_call_string(func, args,
    kwargs), which renders the *whole* argument tuple. For run_phylo_job that is
    the job_params dict, so every "phylo_high: ... (uuid)" line the worker logged
    at job start contained the submitter's raw FASTA, their specimen notes, and
    any imported metadata -- written to worker.log and, on failure, to error.log.
    Every enqueue path therefore passes an explicit description built only from
    bounded, non-sensitive values.
    """
    parts = [kind]
    if job_id:
        parts.append(f"job={str(job_id)[:40]}")
    if isinstance(job_params, dict):
        # summarize_job_params is the same bounded summary used for
        # event=job.started: counts and option names only, never payloads.
        from app.workers.tasks import summarize_job_params

        summary = summarize_job_params(job_params)
        options = summary.get("options") or {}
        parts.append(f"input={summary.get('input_type')}")
        parts.append(f"sequences={summary.get('sequence_count')}")
        if summary.get("accession_count"):
            parts.append(f"accessions={summary['accession_count']}")
        if options.get("tree_method"):
            parts.append(f"tree={options['tree_method']}")
    return " ".join(str(part) for part in parts)[:200]


def enqueue_job(job_params: Dict[str, Any], queue_name: str = QUEUE_HIGH,
                meta: Optional[Dict[str, Any]] = None,
                job_id: Optional[str] = None,
                job_timeout: Any = None) -> str:
    """Enqueue a phylo analysis job and return the job ID."""
    # Collapse near-identical records that share an observation number. This
    # lives here rather than in each caller so every job-creation path gets it
    # (web /tree, API v1, iNaturalist auto-tree, Mushroom Observer) instead of
    # only the web one, and so a future entry point cannot silently skip it.
    from app.services.sequence_dedup_service import apply_observation_dedup
    apply_observation_dedup(job_params)

    # Flag input that cannot produce an informative tree (two sequences, or a set
    # that is all one sequence). Runs after dedup so the count is the one the
    # pipeline will actually align, and here rather than in create_job so every
    # submission path gets it. Callers read it back off job_params to show the
    # user; it is advisory only and never blocks the job.
    from app.services.fasta_utils import describe_degenerate_input
    input_warnings = describe_degenerate_input(
        job_params.get("sequence", ""),
        accession_count=len(job_params.get("accessions") or []),
        blast_mode=job_params.get("blast_mode"),
    )
    if input_warnings:
        job_params["input_warnings"] = input_warnings

    if job_timeout is None:
        job_timeout = resolve_job_timeout(job_params)

    q = get_queue(queue_name)
    from app.workers.tasks import run_phylo_job
    job = q.enqueue(
        run_phylo_job,
        job_params,
        job_timeout=job_timeout,
        meta=meta or {},
        job_id=job_id,
        description=safe_job_description("phylo pipeline", job_params, job_id),
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
        description=safe_job_description("mycomap blast refresh", job_id=job_id),
    )
    return job.id


def enqueue_recompute_job(job_id: str, params_dict: Dict[str, Any]) -> str:
    """Enqueue a recompute job under the existing job ID so status pages stream normally."""
    q = get_queue(QUEUE_HIGH)
    from app.workers.events import (
        STEP_INPUT, STEP_ORIENT, STEP_BLAST, STEP_ITS,
        STATE_QUEUED, STATE_SKIPPED, get_initial_steps_meta,
    )
    from app.workers.tasks import run_recompute_job

    steps = get_initial_steps_meta()
    steps[STEP_INPUT] = {"label": "Sequence Queue", "state": STATE_QUEUED}
    steps[STEP_ORIENT] = {"label": "Orientation Check (skipped)", "state": STATE_SKIPPED}
    steps[STEP_BLAST] = {"label": "BLAST Search (skipped)", "state": STATE_SKIPPED}
    # Recompute reuses sequences that were already region-extracted, so the
    # extraction step never re-runs here.
    steps[STEP_ITS] = {"label": "ITS Region Extraction (skipped)", "state": STATE_SKIPPED}

    job = q.enqueue_call(
        run_recompute_job,
        args=(job_id, params_dict),
        # Recompute re-runs the tree step, so it needs the same budget a fresh
        # RAxML job gets.
        timeout=resolve_job_timeout(params_dict),
        job_id=job_id,
        description=safe_job_description("phylo recompute", params_dict, job_id),
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
        from app.services.log_context import log_degradation_rate_limited
        log_degradation_rate_limited(
            logger, "rq_status_lookup_failed",
            "RQ status lookup failed; returning the existing error response",
            job_id=job_id, exception=type(e).__name__,
        )
        return {"id": job_id, "status": "error", "error": str(e)}
