import json
import os
import re
import time
import psutil
from datetime import datetime, timedelta
from pathlib import Path
from sqlalchemy import text, func
from flask import current_app
from app.extensions import db
from app.models import Job
import logging

logger = logging.getLogger(__name__)
_HEALTH_STATES = {}
_HEALTH_LAST_EMIT = {}
# Last observed value per metric, for reporting movement between emissions.
_HEALTH_LAST_VALUE = {}
HEALTH_COOLDOWN_SECONDS = 6 * 3600


def _transition(name, unhealthy, detail, now=None):
    """Log state changes immediately and persistent faults at most every 6h."""
    # `now or time.monotonic()` treated a caller-supplied 0 as "unset", which made
    # the cooldown arithmetic compare a passed-in clock against the real monotonic
    # clock and silently suppress every reminder.
    now = time.monotonic() if now is None else now
    previous = _HEALTH_STATES.get(name)
    last = _HEALTH_LAST_EMIT.get(name, 0)
    changed = previous is None or previous != unhealthy
    due = unhealthy and now - last >= HEALTH_COOLDOWN_SECONDS
    _HEALTH_STATES[name] = unhealthy
    if not changed and not due:
        return
    _HEALTH_LAST_EMIT[name] = now
    if unhealthy:
        logger.warning("event=health.%s.unhealthy Health threshold crossed %s", name, detail)
    elif previous:
        logger.warning("event=health.%s.recovered Health recovered %s", name, detail)


def emit_health_transitions(metrics):
    """Perform cheap health checks and emit only transitions/cooldown reminders."""
    disk_percent = float(metrics.get("disk_usage") or 0)
    disk_free = int(metrics.get("disk_free_bytes") or 0)
    disk_was_bad = _HEALTH_STATES.get("disk", False)
    disk_bad = disk_percent >= (85 if disk_was_bad else 90)
    # Free space at the previous emission, so a repeat warning says whether the
    # situation is getting worse or merely still true. A flat number repeated
    # every six hours told nobody whether to act.
    previous_free = _HEALTH_LAST_VALUE.get("disk_free_bytes")
    delta = "" if previous_free is None else f" delta_bytes={disk_free - previous_free:+d}"
    _transition(
        "disk", disk_bad,
        f"percent={disk_percent:.1f} free_bytes={disk_free}"
        f" mount={metrics.get('disk_mountpoint') or '?'}{delta}",
    )
    if disk_bad:
        _HEALTH_LAST_VALUE["disk_free_bytes"] = disk_free
    memory_percent = float(metrics.get("memory_percent") or 0)
    memory_was_bad = _HEALTH_STATES.get("memory", False)
    _transition(
        "memory", memory_percent >= (85 if memory_was_bad else 90),
        f"percent={memory_percent:.1f} available_bytes={int(metrics.get('memory_available_bytes') or 0)}",
    )

    workers = get_worker_status().get("workers", [])
    healthy = [worker for worker in workers if worker.get("status") == "healthy"]
    _transition(
        "worker_heartbeat", not healthy,
        f"healthy={len(healthy)} total={len(workers)} states={','.join(sorted({w.get('status', 'unknown') for w in workers})) or 'missing'}",
    )

    try:
        from app.workers.queue import get_redis_connection, QUEUE_HIGH, QUEUE_BULK
        from rq.registry import StartedJobRegistry
        redis_conn = get_redis_connection()
        redis_conn.ping()
        queue_depth = sum(int(redis_conn.llen(f"rq:queue:{name}")) for name in (QUEUE_HIGH, QUEUE_BULK))
        wip = sum(StartedJobRegistry(name=name, connection=redis_conn).count for name in (QUEUE_HIGH, QUEUE_BULK))
        _transition("redis", False, "ping=ok")
        queue_was_bad = _HEALTH_STATES.get("queue_depth", False)
        _transition("queue_depth", queue_depth >= (10 if queue_was_bad else 25), f"queued={queue_depth} wip={wip}")
    except Exception as exc:
        _transition("redis", True, f"exception={type(exc).__name__}")

    try:
        db.session.execute(text("SELECT 1"))
        _transition("database", False, "query=ok")
        cutoff = datetime.utcnow() - timedelta(days=1)
        # coalesce, to agree with `flask reap-stuck-jobs`, which uses the same
        # expression. `Job.updated_at < cutoff` is NULL -- and therefore never
        # true -- for rows written before updated_at existed, so the jobs most
        # likely to be genuinely abandoned were the ones this check could not
        # see, and the reaper's list and the health check's count disagreed.
        stuck = Job.query.filter(
            Job.status.in_(("queued", "running")),
            func.coalesce(Job.updated_at, Job.created_at) < cutoff,
        ).count()
        _transition("stuck_jobs", stuck > 0, f"older_than_1d={stuck}")
    except Exception as exc:
        _transition("database", True, f"exception={type(exc).__name__}")
    finally:
        # These reads open a transaction that nothing else closes: run-metrics
        # loops forever inside one app context, so without this the process sits
        # "idle in transaction" permanently and holds back the xmin horizon,
        # stopping VACUUM from reclaiming dead tuples on the job table.
        try:
            db.session.rollback()
        except Exception:
            pass


def check_system_health():
    """Lightweight health checks for DB + filesystem."""
    health = {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "components": {
            "database": "unknown",
            "filesystem": "unknown",
        },
    }

    # Database round-trip. SQLAlchemy 2.x requires text() for raw SQL.
    try:
        db.session.execute(text("SELECT 1"))
        health["components"]["database"] = "ok"
    except Exception as e:
        # Log internally; do not echo the exception text to clients (DB error
        # messages can include connection-string fragments).
        current_app.logger.warning("health: db check failed: %s", e)
        health["status"] = "degraded"
        health["components"]["database"] = "error"

    # Job storage filesystem.
    try:
        job_dir = Path(current_app.config["JOB_DIR"])
        if job_dir.exists() and os.access(job_dir, os.W_OK):
            health["components"]["filesystem"] = "ok"
        else:
            health["status"] = "degraded"
            health["components"]["filesystem"] = "error: not writable"
    except Exception as e:
        current_app.logger.warning("health: fs check failed: %s", e)
        health["status"] = "degraded"
        health["components"]["filesystem"] = "error"

    return health


def get_worker_status():
    """Inspect heartbeat files in the configured worker directory."""
    worker_dir = Path(current_app.config.get("WORKER_DIR", "var/workers"))

    if not worker_dir.exists():
        return {"workers": [], "status": "no_workers_dir"}

    now = time.time()
    workers = []

    for hb_file in worker_dir.glob("*.heartbeat"):
        try:
            mtime = hb_file.stat().st_mtime
            age = now - mtime
            if age <= 60:
                status = "healthy"
            elif age <= 300:
                status = "stale"
            else:
                status = "dead"
            workers.append({
                "id": hb_file.stem,
                # UTC, like every other timestamp this module emits.
                # fromtimestamp() rendered server-local time, so a
                # heartbeat looked hours off from the "timestamp" field
                # sitting next to it in the same payload.
                "last_heartbeat": datetime.utcfromtimestamp(mtime).isoformat(),
                "age_seconds": round(age, 1),
                "status": status,
            })
        except Exception:
            continue

    # Sort: healthy first, then stale, then dead; within each by freshest.
    rank = {"healthy": 0, "stale": 1, "dead": 2}
    workers.sort(key=lambda w: (rank.get(w["status"], 9), w["age_seconds"]))
    return {"workers": workers}


def get_global_metrics():
    """Aggregate job counts across the DB."""
    total_jobs = Job.query.count()

    status_counts = {}
    rows = db.session.query(Job.status, func.count(Job.status)).group_by(Job.status).all()
    for status, count in rows:
        status_counts[status] = count

    tracked_statuses = ("failed", "completed", "queued", "running")
    now = datetime.utcnow()
    cutoffs = {
        "24h": now - timedelta(hours=24),
        "7d": now - timedelta(days=7),
        "30d": now - timedelta(days=30),
    }
    status_period_counts = {
        status: {"24h": 0, "7d": 0, "30d": 0, "all_time": 0}
        for status in tracked_statuses
    }

    # Aggregate in the database. Materializing every job row here made these
    # (unauthenticated) endpoints scale with table size.
    all_time_rows = db.session.query(
        Job.status, func.count(Job.id)
    ).filter(
        Job.status.in_(tracked_statuses)
    ).group_by(Job.status).all()
    for status, count in all_time_rows:
        status_period_counts[status]["all_time"] = count

    for period, cutoff in cutoffs.items():
        period_rows = db.session.query(
            Job.status, func.count(Job.id)
        ).filter(
            Job.status.in_(tracked_statuses),
            Job.created_at > cutoff,
        ).group_by(Job.status).all()
        for status, count in period_rows:
            status_period_counts[status][period] = count

    return {
        "total_jobs": total_jobs,
        "status_counts": status_counts,
        "status_period_counts": status_period_counts,
        # Preserve the existing JSON field for clients already using /metrics.
        "recent_failed_24h": status_period_counts["failed"]["24h"],
    }


def collect_system_metrics():
    """Snapshot CPU / memory / disk usage."""
    # interval=0.2 gives a real reading; interval=None returns 0.0 on first
    # call after process start because psutil has no prior sample to diff.
    memory = psutil.virtual_memory()
    job_dir = str(current_app.config["JOB_DIR"])
    disk = psutil.disk_usage(job_dir)
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "memory_percent": memory.percent,
        "memory_available_bytes": memory.available,
        "disk_usage": disk.percent,
        "disk_free_bytes": disk.free,
        # The alert reported a bare percentage, which read as "Dikarya is
        # filling the disk" even when the growth was somewhere else entirely on
        # the same filesystem. Naming the mount and the job tree's own share
        # makes it clear whether reclaiming job space can even help.
        "disk_mountpoint": _mountpoint_for(job_dir),
        "timestamp": datetime.utcnow().isoformat(),
    }


def _mountpoint_for(path):
    """Return the mount point the given path lives on."""
    try:
        path = os.path.abspath(path)
        while not os.path.ismount(path):
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
        return path
    except Exception:
        return "?"


def get_ai_usage(days=30):
    """Summarise Claude review usage from the append-only usage log.

    Reads `var/logs/claude_reviews.jsonl`, one record per *billed* review --
    cache hits are never written, so these totals are what the feature actually
    consumed rather than how often the button was pressed.

    Returns totals for the window plus a per-day series for the chart. A missing
    or unreadable log is reported as an empty window rather than an error: the
    monitoring page must still render on a box where nobody has run a review.
    """
    from app.services.tree_analysis_service import _usage_log_path, is_configured

    empty = {
        "enabled": False,
        "days": days,
        "available": False,
        "total_reviews": 0,
        "total_cost_usd": 0.0,
        "cost_available": True,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "avg_seconds": 0.0,
        "series": [],
        "max_daily_reviews": 0,
        "max_daily_cost": 0.0,
        "by_rating": {},
        "models": {},
        "last_review": None,
    }
    try:
        enabled = is_configured()
    except Exception:
        enabled = False
    empty["enabled"] = enabled

    try:
        path = _usage_log_path()
        if not path.is_file():
            return empty
        cutoff = time.time() - days * 86400
        records = []
        with open(path, "r") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = rec.get("ts")
                if not isinstance(ts, (int, float)) or ts < cutoff:
                    continue
                records.append(rec)
    except OSError as exc:
        logger.warning("Could not read Claude review usage log: %s", exc)
        return empty

    empty["available"] = True
    if not records:
        return empty

    def _num(value):
        return value if isinstance(value, (int, float)) else 0

    cost_available = all(
        isinstance(rec.get("cost_usd"), (int, float)) for rec in records
    )

    # Bucket by UTC day so the series lines up with the rest of the dashboard.
    buckets = {}
    for rec in records:
        day = datetime.utcfromtimestamp(rec["ts"]).strftime("%Y-%m-%d")
        slot = buckets.setdefault(day, {"reviews": 0, "cost": 0.0, "cost_available": True})
        slot["reviews"] += 1
        if isinstance(rec.get("cost_usd"), (int, float)):
            slot["cost"] += rec["cost_usd"]
        else:
            slot["cost_available"] = False

    today = datetime.utcnow().date()
    series = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        slot = buckets.get(day, {"reviews": 0, "cost": 0.0, "cost_available": True})
        series.append({
            "date": day,
            "label": day[5:],
            "reviews": slot["reviews"],
            "cost": round(slot["cost"], 4),
            "cost_available": slot["cost_available"],
        })

    by_rating = {}
    models = {}
    for rec in records:
        if rec.get("rating"):
            by_rating[rec["rating"]] = by_rating.get(rec["rating"], 0) + 1
        if rec.get("model"):
            models[rec["model"]] = models.get(rec["model"], 0) + 1

    durations = [_num(r.get("elapsed_seconds")) for r in records if _num(r.get("elapsed_seconds"))]
    latest = max(records, key=lambda r: r["ts"])

    return {
        "enabled": enabled,
        "days": days,
        "available": True,
        "total_reviews": len(records),
        "total_cost_usd": (
            round(sum(r["cost_usd"] for r in records), 2)
            if cost_available else None
        ),
        "cost_available": cost_available,
        "total_input_tokens": sum(
            _num(r.get("input_tokens")) + _num(r.get("cache_read_tokens"))
            + _num(r.get("cache_creation_tokens")) for r in records
        ),
        "total_output_tokens": sum(_num(r.get("output_tokens")) for r in records),
        "avg_seconds": round(sum(durations) / len(durations), 1) if durations else 0.0,
        "series": series,
        "max_daily_reviews": max((s["reviews"] for s in series), default=0),
        "max_daily_cost": max((s["cost"] for s in series), default=0.0),
        "by_rating": by_rating,
        "models": models,
        "last_review": datetime.utcfromtimestamp(latest["ts"]).strftime("%Y-%m-%d %H:%M UTC"),
    }


# -----------------------------------------------------------------------------
# Live job detail
#
# Everything below is rendered on /admin/monitoring and served by /health/jobs,
# both of which are unauthenticated. A Dikarya job UUID is a capability token --
# `check_job_access(mode="view")` hands the tree to anyone holding it -- so no
# function here may emit a full job id, and none may emit anything derived from
# the submitted payload: no sequence headers, no notes, no outgroup name, no
# file contents. What is safe is the shape of the work: counts, option names,
# pipeline step states, tool names, timings and process statistics. Keep new
# fields on that side of the line.
# -----------------------------------------------------------------------------

# Options copied verbatim out of input_info.json. Whitelisted rather than
# filtered, because the same file holds `sequence`, `notes`, `outgroup` and
# `sequence_metadata`, all of which are the submitter's own text.
PUBLIC_JOB_OPTION_KEYS = (
    "input_type", "alignment_method", "trimming_method",
    "trim_terminal_overhangs", "fix_orientation", "its_region",
    "tree_method", "tree_model", "bootstrap", "bootstrap_cap",
    "enable_bootstrap", "alrt_replicates", "run_preset", "bootstrap_preset",
    "mcmc_generations", "mcmc_nruns", "mcmc_nchains", "mcmc_stop_early",
    "moose_enabled", "early_stopping", "blast_mode", "include_ncbi",
    "include_local",
)

# Job-directory files worth watching for liveness, most specific first. A tool
# that is working rewrites one of these every few seconds even when the step it
# is in reports no progress of its own.
JOB_ACTIVITY_GLOBS = (
    "tree/*.log", "tree/*.raxml.*", "tree/*.ckp", "tree/*.treefile",
    "alignment/*", "blast/*", "logs/*.log",
)
MAX_ACTIVITY_FILES = 400
# How much of a tool log to read when looking for a progress line.
PROGRESS_TAIL_BYTES = 8192


def _job_ref(job_id):
    """The short, non-actionable handle shown in place of a job UUID."""
    return str(job_id or "")[:8]


def _iso(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _age_seconds(value, now=None):
    """Seconds since a naive-UTC datetime, or None."""
    if not isinstance(value, datetime):
        return None
    now = now or datetime.utcnow()
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    return round(max(0.0, (now - value).total_seconds()), 1)


def _read_job_option_summary(job_id):
    """Bounded submission summary read off disk for a queued or running job."""
    summary = {"options": {}, "sequence_count": None, "accession_count": None,
               "total_bases": None, "warnings": 0}
    try:
        path = Path(current_app.config["JOB_DIR"]) / str(job_id) / "input_info.json"
        if not path.is_file():
            return summary
        with open(path, "r") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return summary
    if not isinstance(data, dict):
        return summary

    for key in PUBLIC_JOB_OPTION_KEYS:
        value = data.get(key)
        if isinstance(value, bool) or isinstance(value, (int, float)):
            summary["options"][key] = value
        elif isinstance(value, str) and value:
            # Truncated: every whitelisted key is a short enum in practice, so
            # a long value means something unexpected landed in it.
            summary["options"][key] = value[:60]

    sequence = data.get("sequence")
    if isinstance(sequence, str):
        count = sequence.count(">")
        summary["sequence_count"] = count
        summary["total_bases"] = max(0, len(sequence) - count)
    accessions = data.get("accessions")
    if isinstance(accessions, list):
        summary["accession_count"] = len(accessions)
    warnings = data.get("validation_warnings")
    if isinstance(warnings, list):
        summary["warnings"] = len(warnings)
    return summary


def _describe_from_rq_description(description):
    """Fall back to the bounded description RQ already stores for the job.

    A job directory does not exist until the worker starts, so a queued job has
    no input_info.json to read. `safe_job_description` builds exactly the
    summary needed here -- kind, input type, sequence/accession counts, tree
    method -- and is already guaranteed to contain no payload.
    """
    text_value = str(description or "")
    kind, _, rest = text_value.partition(" job=")
    parsed = {"kind": kind.strip()[:40] or "job", "sequence_count": None,
              "accession_count": None, "options": {}}
    for token in rest.split(" ")[1:]:
        key, _, value = token.partition("=")
        if not value:
            continue
        if key == "sequences" and value.isdigit():
            parsed["sequence_count"] = int(value)
        elif key == "accessions" and value.isdigit():
            parsed["accession_count"] = int(value)
        elif key == "input":
            parsed["options"]["input_type"] = value[:40]
        elif key == "tree":
            parsed["options"]["tree_method"] = value[:40]
    return parsed


def _job_work_dir(job_dir):
    """Watch the isolated recompute workspace while its outputs are staged."""
    stages = [p for p in job_dir.glob('.recompute-*')
              if p.is_dir() and not p.is_symlink()]
    return max(stages, key=lambda p: p.stat().st_mtime) if stages else job_dir


def _job_activity(job_id):
    """Newest touched file in the job directory, as a liveness signal.

    Only the path, size and age are reported -- never the contents, and the
    paths themselves are pipeline-generated names, not the submitter's.
    """
    try:
        job_dir = Path(current_app.config["JOB_DIR"]) / str(job_id)
        if not job_dir.is_dir():
            return None
        newest = None
        seen = 0
        work_dir = _job_work_dir(job_dir)
        for pattern in JOB_ACTIVITY_GLOBS:
            for path in work_dir.glob(pattern):
                seen += 1
                if seen > MAX_ACTIVITY_FILES:
                    break
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if not path.is_file():
                    continue
                if newest is None or stat.st_mtime > newest[1]:
                    newest = (path, stat.st_mtime, stat.st_size)
        if newest is None:
            return None
        path, mtime, size = newest
        return {
            "file": str(path.relative_to(job_dir)),
            "size_bytes": size,
            "age_seconds": round(max(0.0, time.time() - mtime), 1),
        }
    except Exception:
        return None


# Progress lines emitted by the long-running tree builders. Each pattern must
# capture numbers only: these logs also contain taxon labels, and nothing but
# the counts may leave this function.
_PROGRESS_PATTERNS = (
    # RAxML-NG: "[00:12:03] Bootstrap tree #850, logLikelihood: -3156.4"
    ("raxml", re.compile(r"Bootstrap tree #(\d+)"), "Bootstrap replicate", "bootstrap"),
    ("raxml", re.compile(r"ML tree search #(\d+)"), "ML tree search", "ml_search"),
    # IQ-TREE: "BOOTSTRAP REPLICATE 120" / "Iteration 250 / LogL: ..."
    ("iqtree", re.compile(r"BOOTSTRAP REPLICATE (\d+)"), "Bootstrap replicate", "bootstrap"),
    ("iqtree", re.compile(r"Iteration (\d+) / LogL"), "Search iteration", "iteration"),
    # MrBayes: "      500000 -- (-3211.123) ..."
    ("mrbayes", re.compile(r"^\s*(\d+) -- "), "MCMC generation", "generation"),
)
_PROGRESS_LOG_GLOBS = ("tree/*.raxml.log", "tree/*.iqtree.log", "tree/*.log",
                       "logs/tree_builder.log")


def _tool_progress(job_id, options):
    """Best-effort "how far along is the tree builder" reading.

    Tails the tool's own log for a counter line. Returns None when the running
    tool publishes no countable progress (FastTree, MAFFT, trimAl), which is
    normal and not an error.
    """
    try:
        job_dir = Path(current_app.config["JOB_DIR"]) / str(job_id)
        if not job_dir.is_dir():
            return None
        work_dir = _job_work_dir(job_dir)
        candidates = []
        for pattern in _PROGRESS_LOG_GLOBS:
            for path in work_dir.glob(pattern):
                try:
                    if path.is_file() and path.stat().st_size:
                        candidates.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        if not candidates:
            return None
        candidates.sort(reverse=True)
        path = candidates[0][1]
        from app.services.artifact_storage import open_artifact
        with open_artifact(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - PROGRESS_TAIL_BYTES))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return None

    best = None
    for line in reversed(tail.splitlines()):
        for tool, pattern, label, kind in _PROGRESS_PATTERNS:
            match = pattern.search(line)
            if match:
                best = (tool, label, kind, int(match.group(1)))
                break
        if best:
            break
    if not best:
        return None

    tool, label, kind, current = best
    total = None
    options = options or {}
    if kind == "bootstrap":
        for key in ("bootstrap_cap", "bootstrap", "alrt_replicates"):
            value = options.get(key)
            if isinstance(value, int) and value > 0:
                total = value
                break
    elif kind == "generation":
        value = options.get("mcmc_generations")
        if isinstance(value, int) and value > 0:
            total = value

    percent = None
    if total:
        percent = round(min(100.0, current / total * 100), 1)
    return {
        "tool": tool,
        "label": label,
        "current": current,
        "total": total,
        "percent": percent,
        "source": str(path.name)[:80],
    }


def _process_stats(pid):
    """CPU/memory for a worker's work horse and the tool it spawned.

    Average CPU is derived from cpu_times over the process lifetime rather than
    sampled, so this costs no wall-clock delay in the request.
    """
    processes = []
    try:
        parent = psutil.Process(int(pid))
    except (psutil.Error, TypeError, ValueError):
        return processes
    try:
        candidates = [parent] + parent.children(recursive=True)
    except psutil.Error:
        candidates = [parent]
    now = time.time()
    for proc in candidates[:20]:
        try:
            with proc.oneshot():
                cpu_times = proc.cpu_times()
                runtime = max(0.001, now - proc.create_time())
                cpu_seconds = cpu_times.user + cpu_times.system
                processes.append({
                    # Executable name only. The command line carries job paths
                    # and, for some tools, label arguments.
                    "name": proc.name()[:40],
                    "pid": proc.pid,
                    "role": "worker" if proc.pid == parent.pid else "tool",
                    "status": proc.status(),
                    "threads": proc.num_threads(),
                    "rss_bytes": proc.memory_info().rss,
                    "cpu_seconds": round(cpu_seconds, 1),
                    "avg_cpu_percent": round(cpu_seconds / runtime * 100, 1),
                    "runtime_seconds": round(runtime, 1),
                })
        except psutil.Error:
            continue
    # The work horse's own siblings (RQ scheduler, the previous fork) show up
    # here too and are indistinguishable by name, so order by how much CPU each
    # has actually consumed: the tool doing the job sorts to the top.
    processes.sort(key=lambda proc: (proc["role"] != "worker", -proc["cpu_seconds"]))
    return processes[:8]


def _steps_from_meta(meta, now=None):
    """Flatten job.meta['steps'] into an ordered list with durations."""
    from app.workers.events import PIPELINE_STEPS

    now = now if now is not None else time.time()
    raw = meta.get("steps") if isinstance(meta, dict) else None
    if not isinstance(raw, dict):
        return []
    ordered = [key for key in PIPELINE_STEPS if key in raw]
    ordered += [key for key in raw if key not in ordered]
    steps = []
    for key in ordered:
        entry = raw.get(key)
        if not isinstance(entry, dict):
            continue
        started = entry.get("started_at")
        ended = entry.get("ended_at")
        duration = None
        if isinstance(started, (int, float)):
            end = ended if isinstance(ended, (int, float)) else now
            duration = round(max(0.0, end - started), 1)
        steps.append({
            "key": key,
            "label": str(entry.get("label") or key)[:80],
            "state": str(entry.get("state") or "queued")[:20],
            # Step details are counts and option names by construction (see
            # tasks.py); bounded anyway so a future one cannot run long.
            "detail": str(entry.get("detail") or "")[:200],
            "tool": str(entry.get("tool") or "")[:40] or None,
            "started_at": started if isinstance(started, (int, float)) else None,
            "ended_at": ended if isinstance(ended, (int, float)) else None,
            "duration_seconds": duration,
        })
    return steps


def _describe_rq_job(rq_job, state, now=None, worker=None, position=None,
                     include_processes=True):
    """Render one RQ job as the public live-job record."""
    now_dt = now or datetime.utcnow()
    job_id = rq_job.id
    meta = rq_job.meta if isinstance(rq_job.meta, dict) else {}
    summary = _read_job_option_summary(job_id)
    described = _describe_from_rq_description(rq_job.description)
    if summary["sequence_count"] is None:
        summary["sequence_count"] = described["sequence_count"]
    if summary["accession_count"] is None:
        summary["accession_count"] = described["accession_count"]
    for key, value in described["options"].items():
        summary["options"].setdefault(key, value)
    steps = _steps_from_meta(meta)
    current_key = meta.get("current_step")
    current = next((s for s in steps if s["key"] == current_key), None)
    if current is None:
        current = next((s for s in steps if s["state"] == "running"), None)

    elapsed = _age_seconds(rq_job.started_at, now_dt) if rq_job.started_at else None
    timeout = rq_job.timeout if isinstance(rq_job.timeout, (int, float)) else None

    record = {
        "ref": _job_ref(job_id),
        "state": state,
        "queue": str(rq_job.origin or "")[:40],
        "position": position,
        "worker": _job_ref(rq_job.worker_name) if rq_job.worker_name else None,
        "kind": described["kind"],
        "enqueued_at": _iso(rq_job.enqueued_at),
        "started_at": _iso(rq_job.started_at),
        "wait_seconds": None,
        "elapsed_seconds": elapsed,
        "timeout_seconds": timeout,
        "timeout_used_percent": (
            round(min(100.0, elapsed / timeout * 100), 1)
            if timeout and elapsed is not None else None
        ),
        "input": {
            "sequence_count": summary["sequence_count"],
            "accession_count": summary["accession_count"],
            "total_bases": summary["total_bases"],
            "warnings": summary["warnings"],
        },
        "options": summary["options"],
        "steps": steps,
        "steps_done": sum(1 for s in steps if s["state"] in ("done", "skipped")),
        "steps_total": len(steps),
        "current_step": current["key"] if current else None,
        "current_step_label": current["label"] if current else None,
        "current_step_seconds": current["duration_seconds"] if current else None,
        "current_tool": str(meta.get("current_tool") or "")[:40] or None,
        "activity": None,
        "progress": None,
        "processes": [],
    }

    # Queue wait: enqueued -> started for a running job, enqueued -> now for one
    # still waiting.
    if rq_job.enqueued_at:
        end = rq_job.started_at if rq_job.started_at else None
        if end is not None:
            record["wait_seconds"] = round(
                max(0.0, (end - rq_job.enqueued_at).total_seconds()), 1
            )
        else:
            record["wait_seconds"] = _age_seconds(rq_job.enqueued_at, now_dt)

    if state == "running":
        record["activity"] = _job_activity(job_id)
        record["progress"] = _tool_progress(job_id, summary["options"])
        if include_processes and worker is not None and getattr(worker, "pid", None):
            record["processes"] = _process_stats(worker.pid)
    return record


def get_active_jobs(max_queued=25):
    """Everything currently running or waiting, in pipeline-level detail.

    Reads RQ directly rather than the database: the database knows a job is
    "running", RQ knows which worker has it, which pipeline step it is on, how
    long that step has taken and how much of its timeout budget is gone.
    """
    from app.workers.queue import get_redis_connection, QUEUE_HIGH, QUEUE_BULK
    from rq import Queue, Worker
    from rq.registry import StartedJobRegistry

    payload = {
        "generated_at": datetime.utcnow().isoformat(),
        "available": False,
        "error": None,
        "running": [],
        "queued": [],
        "queued_total": 0,
        "queues": [],
    }

    try:
        conn = get_redis_connection()
        conn.ping()
        workers = Worker.all(connection=conn)
    except Exception as exc:
        logger.warning("live jobs: redis unavailable: %s", exc)
        payload["error"] = "Queue backend unavailable"
        return payload

    payload["available"] = True
    workers_by_name = {worker.name: worker for worker in workers}
    now = datetime.utcnow()

    for queue_name in (QUEUE_HIGH, QUEUE_BULK):
        try:
            queue = Queue(queue_name, connection=conn)
            registry = StartedJobRegistry(name=queue_name, connection=conn)
            started_ids = registry.get_job_ids()
            queued_jobs = queue.get_jobs(0, max_queued)
            serving = [w.name for w in workers if queue_name in
                       {q.name for q in getattr(w, "queues", [])}]
            oldest = None

            for rq_job in queue.get_jobs(0, 1):
                oldest = _age_seconds(rq_job.enqueued_at, now)

            payload["queues"].append({
                "name": queue_name,
                "queued": queue.count,
                "running": len(started_ids),
                "workers": len(serving),
                "failed": len(queue.failed_job_registry.get_job_ids()),
                "deferred": queue.deferred_job_registry.count,
                "scheduled": queue.scheduled_job_registry.count,
                "oldest_wait_seconds": oldest,
            })
            payload["queued_total"] += queue.count

            for job_id in started_ids:
                try:
                    rq_job = queue.fetch_job(job_id)
                except Exception:
                    rq_job = None
                if rq_job is None:
                    continue
                worker = workers_by_name.get(rq_job.worker_name)
                payload["running"].append(
                    _describe_rq_job(rq_job, "running", now=now, worker=worker)
                )

            for position, rq_job in enumerate(queued_jobs, start=1):
                if rq_job is None:
                    continue
                payload["queued"].append(
                    _describe_rq_job(rq_job, "queued", now=now, position=position)
                )
        except Exception as exc:
            logger.warning("live jobs: queue %s unreadable: %s", queue_name, exc)

    payload["running"].sort(key=lambda job: -(job["elapsed_seconds"] or 0))
    payload["queued"].sort(key=lambda job: -(job["wait_seconds"] or 0))
    return payload


def get_worker_details():
    """Heartbeat files joined with what RQ knows about each worker process."""
    status = get_worker_status()
    workers = {entry["id"]: dict(entry) for entry in status.get("workers", [])}

    try:
        from app.workers.queue import get_redis_connection
        from rq import Worker

        conn = get_redis_connection()
        now = datetime.utcnow()
        for worker in Worker.all(connection=conn):
            entry = workers.setdefault(worker.name, {
                "id": worker.name,
                "status": "no_heartbeat_file",
                "last_heartbeat": None,
                "age_seconds": None,
            })
            entry.update({
                "ref": _job_ref(worker.name),
                "pid": worker.pid,
                "hostname": str(getattr(worker, "hostname", "") or "")[:60],
                "queues": [q.name for q in getattr(worker, "queues", [])],
                "rq_state": str(worker.get_state() or "")[:20],
                "birth_age_seconds": _age_seconds(worker.birth_date, now),
                "successful_jobs": worker.successful_job_count,
                "failed_jobs": worker.failed_job_count,
                "total_working_seconds": round(worker.total_working_time or 0, 1),
                "current_job": _job_ref(worker.get_current_job_id()),
                "current_job_seconds": (
                    round(worker.current_job_working_time, 1)
                    if getattr(worker, "current_job_working_time", None) else None
                ),
                "rq_version": str(getattr(worker, "version", "") or "")[:20],
            })
    except Exception as exc:
        logger.warning("worker details: RQ registry unreadable: %s", exc)

    for entry in workers.values():
        entry.setdefault("ref", _job_ref(entry.get("id")))
        entry.setdefault("queues", [])

    rank = {"healthy": 0, "stale": 1, "dead": 2}
    result = {"workers": sorted(
        workers.values(),
        key=lambda w: (rank.get(w.get("status"), 9), w.get("age_seconds") or 0),
    )}
    # Preserve the "no_workers_dir" signal the template distinguishes from
    # "the directory is there and empty".
    if status.get("status") and not result["workers"]:
        result["status"] = status["status"]
    return result


def get_recent_jobs(limit=12):
    """The last handful of finished jobs, for context under the live list."""
    try:
        rows = (
            Job.query
            .filter(Job.status.notin_(("queued", "running")))
            .order_by(Job.updated_at.desc().nullslast())
            .limit(limit)
            .all()
        )
    except Exception as exc:
        logger.warning("recent jobs: query failed: %s", exc)
        try:
            db.session.rollback()
        except Exception:
            pass
        return []

    recent = []
    for job in rows:
        duration = None
        if job.created_at and job.updated_at:
            duration = round(max(0.0, (job.updated_at - job.created_at).total_seconds()), 1)
        metrics = job.metrics if isinstance(job.metrics, dict) else {}
        recent.append({
            "ref": _job_ref(job.id),
            "status": job.status,
            "input_type": str(job.input_type or "")[:40],
            "finished_age_seconds": _age_seconds(job.updated_at),
            "duration_seconds": duration,
            # A bounded, non-payload field the worker already writes.
            "failed_step": str(metrics.get("failed_step") or "")[:40] or None,
        })
    return recent
