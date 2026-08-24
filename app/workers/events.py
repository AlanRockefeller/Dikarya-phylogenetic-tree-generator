"""
Real-time job event publishing module.

Provides utilities for publishing SSE events from the worker to Flask
via Redis PubSub. All events are JSON-encoded and include timestamps.

Event Types:
- job_state: Job lifecycle (running, completed, failed)
- step_start/step_done/step_failed: Pipeline step lifecycle
- log: Streaming log lines from tools
- overview: Human-friendly status messages
- metric: Optional metrics (bootstrap count, alignment stats, etc.)

Pipeline Steps (stable keys):
- input: Input Processing
- orient: Orientation Check (auto-fix reversed ITS sequences)
- blast: BLAST Search
- align: Alignment (MAFFT/MUSCLE/ClustalO)
- trim: Trimming (trimAl/BMGE)
- tree: Tree Building (NJ/RAxML/IQ-TREE/MrBayes)
- post: Post-Processing
"""

import json
import logging
import time
import threading
from typing import Optional, List, Dict, Any

import redis

from app.config import Config

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

# Stable pipeline step keys
STEP_INPUT = "input"
STEP_ORIENT = "orient"
STEP_BLAST = "blast"
STEP_ITS = "its"
STEP_ALIGN = "align"
STEP_TRIM = "trim"
STEP_TREE = "tree"
STEP_POST = "post"

PIPELINE_STEPS = [STEP_INPUT, STEP_ORIENT, STEP_BLAST, STEP_ITS, STEP_ALIGN, STEP_TRIM, STEP_TREE, STEP_POST]

# Step states
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_SKIPPED = "skipped"
STATE_FAILED = "failed"

# Event types
EVENT_JOB_STATE = "job_state"
EVENT_STEP_START = "step_start"
EVENT_STEP_DONE = "step_done"
EVENT_STEP_FAILED = "step_failed"
EVENT_LOG = "log"
EVENT_OVERVIEW = "overview"
EVENT_METRIC = "metric"

# Rate limiting settings
MAX_LOG_LINE_LENGTH = 4096  # 4KB max per line
MAX_LINES_PER_SECOND = 200  # Rate limit per (job_id, step, stream)
OVERFLOW_WARNING_INTERVAL = 5.0  # Seconds between overflow warnings
RATE_LIMITER_IDLE_TTL = 3600.0  # Defensive cleanup for jobs missing a terminal event
RATE_LIMITER_CLEANUP_INTERVAL = 60.0

# -----------------------------------------------------------------------------
# Redis Connection (worker-side, no Flask app context)
# -----------------------------------------------------------------------------

_redis_client: Optional[redis.Redis] = None
_redis_lock = threading.Lock()


def _get_redis() -> redis.Redis:
    """Get or create Redis client for worker context (no Flask app)."""
    global _redis_client
    if _redis_client is None:
        with _redis_lock:
            if _redis_client is None:
                _redis_client = redis.from_url(
                    Config.REDIS_URL,
                    socket_connect_timeout=Config.EVENT_REDIS_CONNECT_TIMEOUT_SECONDS,
                    socket_timeout=Config.EVENT_REDIS_SOCKET_TIMEOUT_SECONDS,
                    retry_on_timeout=False,
                )
    return _redis_client


def get_event_channel(job_id: str) -> str:
    """Get Redis PubSub channel name for a job."""
    return f"job:{job_id}:events"


# -----------------------------------------------------------------------------
# Rate Limiting
# -----------------------------------------------------------------------------

class RateLimiter:
    """
    Simple token bucket rate limiter for log lines.
    
    Tracks tokens per (job_id, step, stream) tuple.
    Refills at MAX_LINES_PER_SECOND tokens per second.
    """
    
    def __init__(
        self,
        idle_ttl: float = RATE_LIMITER_IDLE_TTL,
        cleanup_interval: float = RATE_LIMITER_CLEANUP_INTERVAL,
        clock=time.monotonic,
    ):
        self._buckets: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()
        self._last_overflow_warning: Dict[str, float] = {}
        self._job_keys: Dict[str, set] = {}
        self._idle_ttl = idle_ttl
        self._cleanup_interval = cleanup_interval
        self._clock = clock
        self._last_cleanup = clock()
    
    def _bucket_key(self, job_id: str, step: str, stream: str) -> str:
        return f"{job_id}:{step}:{stream}"

    def _remember_key(self, job_id: str, key: str) -> None:
        self._job_keys.setdefault(job_id, set()).add(key)

    def _evict_stale(self, now: float) -> None:
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        stale_jobs = {
            job_id
            for job_id, keys in self._job_keys.items()
            if keys and all(
                now - max(
                    self._buckets.get(key, {}).get("last_seen", 0),
                    self._last_overflow_warning.get(key, 0),
                ) >= self._idle_ttl
                for key in keys
            )
        }
        for job_id in stale_jobs:
            self._forget_job_locked(job_id)

    def _forget_job_locked(self, job_id: str) -> None:
        for key in self._job_keys.pop(job_id, ()):
            self._buckets.pop(key, None)
            self._last_overflow_warning.pop(key, None)

    def forget_job(self, job_id: str) -> None:
        """Remove rate-limit and warning state for exactly one terminal job."""
        with self._lock:
            self._forget_job_locked(job_id)
    
    def try_consume(self, job_id: str, step: str, stream: str) -> bool:
        """
        Try to consume one token. Returns True if allowed, False if rate limited.
        """
        key = self._bucket_key(job_id, step, stream)
        now = self._clock()
        
        with self._lock:
            self._evict_stale(now)
            if key not in self._buckets:
                self._buckets[key] = {
                    "tokens": MAX_LINES_PER_SECOND,
                    "last_refill": now,
                    "last_seen": now,
                }
                self._remember_key(job_id, key)
            
            bucket = self._buckets[key]
            bucket["last_seen"] = now
            
            # Refill tokens based on elapsed time
            elapsed = now - bucket["last_refill"]
            refill = elapsed * MAX_LINES_PER_SECOND
            bucket["tokens"] = min(MAX_LINES_PER_SECOND, bucket["tokens"] + refill)
            bucket["last_refill"] = now
            
            if bucket["tokens"] >= 1:
                bucket["tokens"] -= 1
                return True
            return False
    
    def should_warn_overflow(self, job_id: str, step: str, stream: str) -> bool:
        """Check if we should emit an overflow warning (throttled)."""
        key = self._bucket_key(job_id, step, stream)
        now = self._clock()
        
        with self._lock:
            self._evict_stale(now)
            self._remember_key(job_id, key)
            last_warning = self._last_overflow_warning.get(key, 0)
            if now - last_warning >= OVERFLOW_WARNING_INTERVAL:
                self._last_overflow_warning[key] = now
                return True
            return False


_rate_limiter = RateLimiter()


# -----------------------------------------------------------------------------
# Core Publishing Functions
# -----------------------------------------------------------------------------

def publish_event(job_id: str, payload: dict) -> None:
    """
    Publish an event to Redis PubSub channel.
    
    Automatically adds:
    - ts: Unix timestamp (float)
    - job_id: Job identifier
    
    Args:
        job_id: Job identifier
        payload: Event payload dict (must include 'type')
    """
    event = {
        **payload,
        "ts": time.time(),
        "job_id": job_id,
    }
    
    channel = get_event_channel(job_id)
    message = json.dumps(event)
    
    try:
        _get_redis().publish(channel, message)
    except Exception as e:
        # Log but don't fail the job
        from app.services.log_context import log_degradation_rate_limited
        log_degradation_rate_limited(
            logger, "sse_publish_failed",
            "Worker could not publish a live status event; job processing continued",
            event_type=payload.get("type"), exception=type(e).__name__,
        )


def publish_log(job_id: str, step: str, stream: str, line: str) -> None:
    """
    Publish a log line event with rate limiting and truncation.
    
    Args:
        job_id: Job identifier
        step: Pipeline step key (align, tree, etc.)
        stream: 'stdout' or 'stderr'
        line: Log line content
    """
    # Truncate long lines
    if len(line) > MAX_LOG_LINE_LENGTH:
        line = line[:MAX_LOG_LINE_LENGTH - 20] + "... (truncated)"
    
    # Strip trailing whitespace
    line = line.rstrip()
    
    # Skip empty lines
    if not line:
        return
    
    # Rate limiting
    if not _rate_limiter.try_consume(job_id, step, stream):
        # Rate limited - check if we should warn
        if _rate_limiter.should_warn_overflow(job_id, step, stream):
            # Get log file name for the step
            log_file = "pipeline.log"
            if step == STEP_ALIGN:
                log_file = "alignment.log"
            elif step == STEP_TREE:
                log_file = "tree_builder.log"
            
            publish_overview(
                job_id,
                f"(Log output truncated due to high volume; see {log_file} for full output)"
            )
        return
    
    publish_event(job_id, {
        "type": EVENT_LOG,
        "step": step,
        "stream": stream,
        "line": line,
    })


def publish_command(job_id: str, step: str, args: list) -> None:
    """
    Publish a command line event (displayed in green in the terminal).
    
    Args:
        job_id: Job identifier
        step: Pipeline step key (align, tree, etc.)
        args: Command arguments list (will be joined with spaces)
    """
    cmd_line = " ".join(str(arg) for arg in args)
    
    publish_event(job_id, {
        "type": EVENT_LOG,
        "step": step,
        "stream": "cmd",
        "line": f"$ {cmd_line}",
    })


def publish_step_start(
    job_id: str,
    step: str,
    label: str,
    detail: str = "",
    tool: str = ""
) -> None:
    """
    Publish step_start event.
    
    Args:
        job_id: Job identifier
        step: Pipeline step key
        label: Human-readable label (e.g., "Alignment (MAFFT)")
        detail: Additional detail (e.g., "8 threads, --auto")
        tool: Tool name (e.g., "mafft", "raxml-ng")
    """
    from app.services.log_context import set_pipeline_context
    set_pipeline_context(step=step, tool=tool or None)
    payload = {
        "type": EVENT_STEP_START,
        "step": step,
        "label": label,
    }
    if detail:
        payload["detail"] = detail
    if tool:
        payload["tool"] = tool
    
    publish_event(job_id, payload)


def publish_step_done(job_id: str, step: str, detail: str = "") -> None:
    """
    Publish step_done event.
    
    Args:
        job_id: Job identifier
        step: Pipeline step key
        detail: Summary detail (e.g., "8 sequences, 450 columns")
    """
    payload = {
        "type": EVENT_STEP_DONE,
        "step": step,
    }
    if detail:
        payload["detail"] = detail
    
    publish_event(job_id, payload)


def publish_step_failed(
    job_id: str,
    step: str,
    error: str,
    tool: str = "",
    exit_code: Optional[int] = None,
    stderr_tail: Optional[List[str]] = None
) -> None:
    """
    Publish step_failed event with rich context.
    
    Args:
        job_id: Job identifier
        step: Pipeline step key
        error: Error message
        tool: Tool name that failed
        exit_code: Process exit code if available
        stderr_tail: Last ~30 lines of stderr for debugging
    """
    payload = {
        "type": EVENT_STEP_FAILED,
        "step": step,
        "error": error,
    }
    if tool:
        payload["tool"] = tool
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if stderr_tail:
        payload["stderr_tail"] = stderr_tail[:30]  # Cap at 30 lines
    
    publish_event(job_id, payload)


def publish_overview(job_id: str, message: str) -> None:
    """
    Publish a human-friendly overview message.
    
    These appear in the Overview feed on the status page.
    
    Args:
        job_id: Job identifier
        message: Human-readable message
    """
    publish_event(job_id, {
        "type": EVENT_OVERVIEW,
        "message": message,
    })


def publish_metric(job_id: str, step: str, key: str, value: Any) -> None:
    """
    Publish an optional metric event.
    
    Args:
        job_id: Job identifier
        step: Pipeline step key
        key: Metric key (e.g., "bootstrap", "sequences", "columns")
        value: Metric value
    """
    publish_event(job_id, {
        "type": EVENT_METRIC,
        "step": step,
        "key": key,
        "value": value,
    })


# -----------------------------------------------------------------------------
# Job State Events
# -----------------------------------------------------------------------------

def publish_job_running(job_id: str) -> None:
    """Publish job_state:running event."""
    logger.info("event=job.running Job entered running state")
    publish_event(job_id, {
        "type": EVENT_JOB_STATE,
        "status": "running",
    })


def publish_job_queued(job_id: str) -> None:
    """Publish job_state:queued when a staged job is waiting to resume."""
    publish_event(job_id, {
        "type": EVENT_JOB_STATE,
        "status": "queued",
    })


def publish_job_completed(job_id: str, view_url: str, result_files: Optional[dict] = None) -> None:
    """
    Publish job_state:completed event.
    
    Args:
        job_id: Job identifier
        view_url: URL to view the tree
        result_files: Dict of result file download URLs
    """
    payload = {
        "type": EVENT_JOB_STATE,
        "status": "completed",
        "view_url": view_url,
    }
    if result_files:
        payload["result_files"] = result_files
    
    try:
        publish_event(job_id, payload)
    finally:
        _rate_limiter.forget_job(job_id)


def publish_job_failed(
    job_id: str,
    failed_step: str,
    failed_step_label: str,
    error_summary: str,
    tool: str = "",
    exit_code: Optional[int] = None,
    stderr_tail: Optional[List[str]] = None
) -> None:
    """
    Publish job_state:failed event with full context.
    
    Args:
        job_id: Job identifier
        failed_step: Step key that failed
        failed_step_label: Human-readable step label
        error_summary: Error description
        tool: Tool that failed
        exit_code: Process exit code
        stderr_tail: Last ~30 lines of stderr
    """
    payload = {
        "type": EVENT_JOB_STATE,
        "status": "failed",
        "failed_step": failed_step,
        "failed_step_label": failed_step_label,
        "error_summary": error_summary,
    }
    if tool:
        payload["tool"] = tool
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if stderr_tail:
        payload["stderr_tail"] = stderr_tail[:30]
    
    try:
        publish_event(job_id, payload)
    finally:
        _rate_limiter.forget_job(job_id)


# -----------------------------------------------------------------------------
# Job Meta Helpers
# -----------------------------------------------------------------------------

def get_initial_steps_meta() -> Dict[str, dict]:
    """
    Get initial steps metadata structure for job.meta['steps'].
    
    Returns:
        Dict with all pipeline steps in 'queued' state
    """
    return {
        STEP_INPUT: {"label": "Input Processing", "state": STATE_QUEUED},
        STEP_ORIENT: {"label": "Orientation Check", "state": STATE_QUEUED},
        STEP_BLAST: {"label": "BLAST Search", "state": STATE_QUEUED},
        STEP_ITS: {"label": "ITS Region Extraction", "state": STATE_QUEUED},
        STEP_ALIGN: {"label": "Alignment", "state": STATE_QUEUED},
        STEP_TRIM: {"label": "Trimming", "state": STATE_QUEUED},
        STEP_TREE: {"label": "Tree Building", "state": STATE_QUEUED},
        STEP_POST: {"label": "Post-Processing", "state": STATE_QUEUED},
    }


def update_job_meta(job, patch: dict) -> None:
    """
    Update RQ job.meta with patch dict and save.
    
    Args:
        job: RQ job object (from get_current_job())
        patch: Dict of keys to update in job.meta
    """
    if job is None:
        return
    
    for key, value in patch.items():
        job.meta[key] = value
    job.save_meta()


def update_step_meta(job, step: str, patch: dict) -> None:
    """
    Update a specific step in job.meta['steps'] and save.
    
    Args:
        job: RQ job object
        step: Pipeline step key
        patch: Dict of keys to update in the step's metadata
    """
    if job is None:
        return
    
    if "steps" not in job.meta:
        job.meta["steps"] = get_initial_steps_meta()
    
    if step in job.meta["steps"]:
        job.meta["steps"][step].update(patch)
    else:
        job.meta["steps"][step] = patch
    
    job.save_meta()
