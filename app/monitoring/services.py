import json
import os
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
        stuck = Job.query.filter(Job.status.in_(("queued", "running")), Job.updated_at < cutoff).count()
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
                "last_heartbeat": datetime.fromtimestamp(mtime).isoformat(),
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
