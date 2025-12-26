import os
import time
import json
import psutil
from datetime import datetime, timedelta
from pathlib import Path
from flask import current_app
from app.extensions import db
from app.models import Job

def check_system_health():
    """
    Perform lightweight health checks.
    """
    health = {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "components": {
            "database": "unknown",
            "filesystem": "unknown"
        }
    }
    
    # 1. Check Database
    try:
        # Simple query
        db.session.execute("SELECT 1")
        health["components"]["database"] = "ok"
    except Exception as e:
        health["status"] = "degraded"
        health["components"]["database"] = f"error: {str(e)}"
        
    # 2. Check Job Storage
    try:
        job_dir = Path(current_app.config["JOB_DIR"])
        if job_dir.exists() and os.access(job_dir, os.W_OK):
            health["components"]["filesystem"] = "ok"
        else:
            health["status"] = "degraded"
            health["components"]["filesystem"] = "error: not writable"
    except Exception as e:
         health["status"] = "degraded"
         health["components"]["filesystem"] = f"error: {str(e)}"

    return health

def get_worker_status():
    """
    Check worker heartbeats in /var/phylojobs/workers/
    """
    worker_dir = Path("/var/phylojobs/workers")
    # Windows fallback for dev
    if os.name == 'nt':
        worker_dir = Path(current_app.config.get("WORKER_DIR", "var/workers"))
        
    workers = []
    if not worker_dir.exists():
        return {"workers": [], "status": "no_workers_dir"}
        
    now = time.time()
    
    for hb_file in worker_dir.glob("*.heartbeat"):
        try:
            mtime = hb_file.stat().st_mtime
            worker_id = hb_file.stem
            age = now - mtime
            
            status = "healthy"
            if age > 60:
                status = "stale"
            if age > 300:
                status = "dead"
                
            workers.append({
                "id": worker_id,
                "last_heartbeat": datetime.fromtimestamp(mtime).isoformat(),
                "age_seconds": round(age, 1),
                "status": status
            })
        except Exception:
            continue
            
    return {"workers": workers}

def get_global_metrics():
    """
    Aggregate global job metrics.
    """
    # Total jobs
    total_jobs = Job.query.count()
    
    # Jobs by status
    status_counts = {}
    from sqlalchemy import func
    rows = db.session.query(Job.status, func.count(Job.status)).group_by(Job.status).all()
    for status, count in rows:
        status_counts[status] = count
        
    # Jobs in last 24h
    since = datetime.utcnow() - timedelta(hours=24)
    recent_failed = Job.query.filter(Job.status == "error", Job.updated_at > since).count()
    
    return {
        "total_jobs": total_jobs,
        "status_counts": status_counts,
        "recent_failed_24h": recent_failed
    }

def collect_system_metrics():
    """
    Collect CPU, Memory, Disk usage.
    """
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "memory_percent": psutil.virtual_memory().percent,
        "disk_usage": psutil.disk_usage(current_app.config["JOB_DIR"]).percent,
        "timestamp": datetime.utcnow().isoformat()
    }
