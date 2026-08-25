import uuid
import logging

import redis
from redis.exceptions import LockError
from rq import Queue, Retry
from rq.exceptions import NoSuchJobError
from rq.job import Job as RqJob
from flask import current_app
from typing import Any, Dict, Optional

QUEUE_HIGH = "phylo_high"
QUEUE_BULK = "phylo_bulk"
VALID_QUEUE_NAMES = {QUEUE_HIGH, QUEUE_BULK}
logger = logging.getLogger(__name__)

# Bound the TCP connect only. Without this, a Redis host that accepts no
# connections (down, firewalled, wrong address) blocks the caller for the OS
# connect timeout -- minutes -- and every one of those callers is holding one of
# the site's finite Gunicorn request slots while it waits. `socket_timeout` is
# deliberately NOT set: it applies to reads too, and the SSE endpoints park on a
# blocking pubsub read for the whole life of a stream.
REDIS_CONNECT_TIMEOUT_SECONDS = 5


def get_redis_connection():
    redis_url = current_app.config.get('REDIS_URL', 'redis://localhost:6379/0')
    return redis.from_url(
        redis_url, socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS
    )

def get_queue(name=QUEUE_HIGH) -> Queue:
    if name not in VALID_QUEUE_NAMES:
        name = QUEUE_HIGH
    conn = get_redis_connection()
    return Queue(name, connection=conn)

# Which externally-timed tool bounds each pipeline stage. The names are the
# keys `configured_tool_time_limit_hours` understands, so the RQ backstop and
# the per-subprocess limits can never drift apart. ``None`` means the stage runs
# no external tool and therefore consumes only the general allowance.
_ALIGNMENT_STAGE_TOOL = {
    "mafft": "MAFFT",
    "muscle": "MUSCLE",
    "clustalo": "Clustal Omega",
    "iqtree_builtin": "IQ-TREE alignment",
    "none": None,
}
# Keyed by the exact method names `trimming_service.run_trimming` dispatches on.
# `trimal_gappy` is Dikarya's shipped default (DEFAULT_TRIMMING_METHOD) and the
# /tree form's preselected option; it was missing here, so an ordinary default
# pipeline was budgeted as if it did no external trimming at all while the
# worker went on to run trimAl under its own four-hour limit.
_TRIMMING_STAGE_TOOL = {
    "trimal_gappy": "trimAl",
    "trimal": "trimAl",
    "bmge": "BMGE",
    "none": None,
    "": None,
}
_TREE_STAGE_TOOL = {
    "raxml": "RAxML",
    "iqtree": "IQ-TREE",
    "mrbayes": "MrBayes",
    "fasttree": "FastTree",
    # Neighbour-joining is computed in-process, so it has no tool budget of its
    # own and lives inside the general allowance.
    "nj": None,
}

# Grace on top of the summed stage budgets, so a tool's own timeout fires first
# and produces a structured, user-facing error instead of RQ killing the work
# horse mid-run.
JOB_TIMEOUT_GRACE_SECONDS = 600


def _resolved_method(value, default: str) -> str:
    method = str(value or "default").strip().lower()
    if method == "default":
        method = str(default or "").strip().lower()
    return method


def resolve_job_timeout(job_params: Dict[str, Any]) -> str:
    """Pick the RQ job_timeout covering every independently bounded stage.

    Each external tool carries its own wall-clock limit (MAFFT 8h, trimAl 4h,
    RAxML 15h, ...), and they run one after another. A backstop of "general
    allowance + tree builder" therefore did not cover a legal pipeline: a job
    could stay inside every subprocess timeout it was given and still be killed
    by RQ before the tree builder's own limit was reached, which loses the work
    with no structured error. So the budget is the sum of the stages this
    particular job will run -- not of every tool Dikarya can run -- plus the
    general allowance for the non-tool work (input handling, BLAST, NCBI
    fetches, tree post-processing) and a grace period.

    This lives here, rather than at each submission site, so every enqueue path
    gets the same budget.
    """
    from app.config import Config
    from app.services.subprocess_utils import (
        configured_tool_time_limit_hours,
        resolve_time_limit_hours,
    )
    from app.services.security_utils import coerce_bool

    params = job_params or {}
    hours = resolve_time_limit_hours(
        getattr(Config, "GENERAL_JOB_TIME_LIMIT_HOURS", 8), 8.0
    )

    align_method = _resolved_method(
        params.get("alignment_method"),
        getattr(Config, "BEGINNER_DEFAULT_ALIGNER", "mafft"),
    )
    # Resolved by the same helper the worker's trim step uses, so an absent
    # key lands on DEFAULT_TRIMMING_METHOD and the literal "default" on
    # BEGINNER_DEFAULT_TRIMMING exactly as run_phylo_job does.
    from app.services.trimming_service import resolve_trimming_method

    trim_method = resolve_trimming_method(params)
    tree_method = str(params.get("tree_method") or "").strip().lower()

    stage_tools = []
    # An unrecognized aligner fails fast in run_alignment, but budget it as the
    # configured default rather than as nothing.
    if align_method in _ALIGNMENT_STAGE_TOOL:
        stage_tools.append(_ALIGNMENT_STAGE_TOOL[align_method])
    else:
        stage_tools.append(_ALIGNMENT_STAGE_TOOL.get(
            _resolved_method(None, getattr(Config, "BEGINNER_DEFAULT_ALIGNER", "mafft")),
            "MAFFT",
        ))

    # MUSCLE, Clustal Omega and IQ-TREE's aligner are direction-blind, so
    # run_alignment runs a separate MAFFT pass over the input first. That pass
    # carries MAFFT's full budget, so it is a stage in its own right.
    if (
        align_method not in ("none", "mafft")
        and stage_tools[0] is not None
        and coerce_bool(params.get("fix_orientation"), True)[0]
    ):
        stage_tools.append("MAFFT")

    # An unrecognised trimmer raises in run_trimming before any executable is
    # spawned, so it genuinely gets no tool budget.
    stage_tools.append(_TRIMMING_STAGE_TOOL.get(trim_method))
    stage_tools.append(_TREE_STAGE_TOOL.get(tree_method))

    for tool in stage_tools:
        if tool:
            hours += configured_tool_time_limit_hours(Config, tool)

    return f"{int(hours * 3600) + JOB_TIMEOUT_GRACE_SECONDS}s"


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


def prepare_phylo_job_params(job_params: Dict[str, Any]) -> None:
    """Apply submission-wide normalization before persistence or enqueueing."""
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


def enqueue_job(job_params: Dict[str, Any], queue_name: str = QUEUE_HIGH,
                meta: Optional[Dict[str, Any]] = None,
                job_id: Optional[str] = None,
                job_timeout: Any = None,
                prepare: bool = True) -> str:
    """Enqueue a phylo analysis job and return the job ID."""
    if prepare:
        prepare_phylo_job_params(job_params)

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


def enqueue_recompute_job(job_id: str, params_dict: Dict[str, Any], *,
                          return_created: bool = False):
    """Enqueue at most one active recompute for a job.

    The Redis lock closes the request race where two browser clicks both see
    no active RQ job and then enqueue the same output-producing work.  The
    optional tuple return lets HTTP callers distinguish a new request from a
    harmless duplicate without changing older internal callers.
    """
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

    lock = q.connection.lock(
        f"dikarya:recompute-enqueue:{job_id}", timeout=15, blocking_timeout=10,
    )
    # Acquired and released by hand rather than with `with lock:`. The lock is
    # an optimization -- it collapses two simultaneous clicks -- and must never
    # be able to fail the enqueue it is protecting. `Lock.__enter__` raises
    # LockError when it cannot acquire within blocking_timeout, and
    # `Lock.__exit__` raises LockNotOwnedError when the 15s lease expired
    # first; the latter fires *after* enqueue_call has already created the job,
    # so the caller was told the recompute failed while it was in fact running.
    # Losing the lock only costs us the duplicate-collapse, which fetch_job()
    # below still handles for all but a sub-second race.
    try:
        acquired = bool(lock.acquire())
    except LockError as exc:
        logger.warning(
            "event=recompute.enqueue_lock_unavailable job=%s error=%s", job_id, exc,
        )
        acquired = False
    if not acquired:
        logger.warning(
            "event=recompute.enqueue_unlocked job=%s proceeding without the "
            "duplicate-collapse lock", job_id,
        )

    try:
        existing = q.fetch_job(job_id)
        if existing is not None:
            existing_status = existing.get_status(refresh=True)
            if existing_status in {"queued", "started", "scheduled", "deferred"}:
                return (existing.id, False) if return_created else existing.id

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
    finally:
        if acquired:
            try:
                lock.release()
            except LockError as exc:
                # The lease expired while we were enqueueing. The job exists;
                # saying otherwise would be a lie to the user.
                logger.warning(
                    "event=recompute.enqueue_lock_expired job=%s error=%s",
                    job_id, exc,
                )
    return (job.id, True) if return_created else job.id


def active_recompute_snapshot_mtime(job_id: str):
    """When the active recompute captured tree_state.json, or None.

    ``run_recompute_job`` records this in RQ meta as soon as it reads the
    state, so callers can tell whether the viewer has been edited since. None
    means there is nothing to conflict with: no active job, a job that has not
    reached that step yet (its read will pick the edits up), or an RQ/Redis
    hiccup, all of which should fall through to the normal idempotent path.
    """
    try:
        job = get_queue(QUEUE_HIGH).fetch_job(job_id)
        if job is None:
            return None
        if job.get_status(refresh=True) not in {"queued", "started", "scheduled", "deferred"}:
            return None
        value = (job.meta or {}).get("tree_state_snapshot_mtime")
        return float(value) if value is not None else None
    except Exception as exc:
        logger.warning(
            "event=recompute.snapshot_lookup_failed job=%s error=%s", job_id, exc,
        )
        return None

def get_job_status(job_id: str) -> Dict[str, Any]:
    """Return a dict with at least: id, status, error (optional), progress (optional)."""
    try:
        try:
            job = RqJob.fetch(job_id, connection=get_redis_connection())
        except NoSuchJobError:
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
        
        if status == "failed":
            # exc_info is a complete traceback and can contain paths, source
            # names, submitted sequences, and secrets. Detailed diagnostics
            # remain in server-side logs and the persisted job metrics.
            response["error"] = "Job failed"
            
        # RQ stores a Retry marker as the result while a job waits to be
        # scheduled again. It is internal state and cannot be JSON encoded.
        if status != "failed" and result and not isinstance(result, Retry):
            response["result"] = result
            
        return response
        
    except Exception as e:
        from app.services.log_context import log_degradation_rate_limited
        log_degradation_rate_limited(
            logger, "rq_status_lookup_failed",
            "RQ status lookup failed; returning the existing error response",
            job_id=job_id, exception=type(e).__name__,
        )
        return {"id": job_id, "status": "error", "error": "Job status unavailable"}
