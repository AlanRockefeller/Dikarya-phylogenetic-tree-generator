import os
import time
import psutil
from datetime import datetime, timedelta
from pathlib import Path
from sqlalchemy import text, func
from flask import current_app
from app.extensions import db
from app.models import Job


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

    since = datetime.utcnow() - timedelta(hours=24)
    recent_failed = Job.query.filter(Job.status == "error", Job.updated_at > since).count()

    return {
        "total_jobs": total_jobs,
        "status_counts": status_counts,
        "recent_failed_24h": recent_failed,
    }


def collect_system_metrics():
    """Snapshot CPU / memory / disk usage."""
    # interval=0.2 gives a real reading; interval=None returns 0.0 on first
    # call after process start because psutil has no prior sample to diff.
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "memory_percent": psutil.virtual_memory().percent,
        "disk_usage": psutil.disk_usage(str(current_app.config["JOB_DIR"])).percent,
        "timestamp": datetime.utcnow().isoformat(),
    }
