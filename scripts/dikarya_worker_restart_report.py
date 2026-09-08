"""Read-only worker report, invoked as dikarya, never as root."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from redis import Redis
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.artifact_storage import open_artifact


def report(queue):
    if queue not in ("phylo_high", "phylo_bulk"):
        raise ValueError("Invalid queue")
    redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                            socket_timeout=5, socket_connect_timeout=5,
                            decode_responses=True)
    # Do not deserialize RQ pickle payloads: Redis is not a code trust boundary.
    members = redis.zrange(f"rq:wip:{queue}", 0, -1)
    ids = sorted({member.split(":", 1)[0] for member in members})
    jobs = []
    engine = create_engine(os.environ["DATABASE_URL"], connect_args={"connect_timeout": 5})
    with engine.connect() as conn:
        for job_id in ids:
            row = conn.execute(text(
                'SELECT j.user_id, u.email, j.metrics FROM job j '
                'LEFT JOIN "user" u ON u.id=j.user_id WHERE j.id=:id'
            ), {"id": job_id}).mappings().first()
            raw = redis.hmget(f"rq:job:{job_id}", "started_at", "description", "timeout")
            elapsed = None
            if raw[0]:
                started = datetime.fromisoformat(raw[0].replace("Z", "+00:00"))
                elapsed = max(0, int((datetime.now(timezone.utc) - started.replace(tzinfo=timezone.utc)).total_seconds()))
            metrics = (row["metrics"] or {}) if row else {}
            details = {}
            # Never use unvalidated Redis IDs as paths.
            import uuid
            if str(uuid.UUID(job_id)) == job_id:
                try:
                    with open_artifact(Path("/var/www/dikarya/var/jobs") / job_id / "input_info.json", "rt") as stream:
                        params = json.load(stream)
                    sequence = params.get("sequence") or ""
                    details = {"sequence_count": sequence.count(">"),
                               "alignment_method": params.get("alignment_method"),
                               "tree_method": params.get("tree_method"),
                               "observation": (params.get("_inat_tree_preparation") or {}).get("observation_id")}
                except FileNotFoundError:
                    pass
            limit = int(raw[2]) if raw[2] else None
            jobs.append({"id": job_id, "owner": (row["email"] or "anonymous") if row else "unknown",
                         "description": raw[1], "elapsed_seconds": elapsed,
                         "tree_method": metrics.get("tree_method"),
                         "input": details,
                         "remaining_estimate": "Unknown; input size and convergence determine runtime",
                         "timeout_seconds": limit,
                         "remaining_timeout_seconds_not_eta": max(0, limit - elapsed) if limit and elapsed is not None else None})
    return {"queue": queue, "pending": redis.llen(f"rq:queue:{queue}"), "jobs": jobs}


if __name__ == "__main__":
    print(json.dumps(report(sys.argv[1])))
