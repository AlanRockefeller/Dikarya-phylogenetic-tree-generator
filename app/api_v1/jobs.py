"""Job-related helpers shared by /api/v1 routes."""
import json
from pathlib import Path

from flask import g, url_for

from app.config import Config
from app.models import Job
from app.services.security_utils import validate_job_id


def get_owned_job_or_404(job_id):
    """Return the Job row if it exists AND is owned by the calling user.

    Anything else returns None -- caller should 404. We intentionally do not
    distinguish "doesn't exist" from "exists but not yours" so the API can't
    be used to enumerate UUIDs.
    """
    if not validate_job_id(job_id):
        return None
    job = Job.query.get(job_id)
    if job is None:
        return None
    user = getattr(g, "api_user", None)
    if user is None or job.user_id != user.id:
        return None
    return job


def _load_input_info(job_id):
    """Best-effort read of var/jobs/{id}/input_info.json."""
    path = Config.JOB_DIR / job_id / "input_info.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def serialize_job(job):
    """Build the v1 job resource for a Job row."""
    info = _load_input_info(job.id)
    return {
        "id": job.id,
        "status": job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "input_type": job.input_type,
        "notes": (job.metrics or {}).get("notes", ""),
        "params": {
            "alignment_method": info.get("alignment_method") or (job.metrics or {}).get("alignment_method"),
            "trimming_method":  info.get("trimming_method")  or (job.metrics or {}).get("trimming_method"),
            "trim_terminal_overhangs": (
                info.get("trim_terminal_overhangs")
                if info.get("trim_terminal_overhangs") is not None
                else (job.metrics or {}).get("trim_terminal_overhangs")
            ),
            "tree_method":      info.get("tree_method")      or (job.metrics or {}).get("tree_method"),
            "tree_model":       info.get("tree_model"),
            "bootstrap":        info.get("bootstrap"),
            "mcmc_generations": info.get("mcmc_generations"),
        },
        "metrics": job.metrics or {},
        "links": {
            "self":   url_for("api_v1.get_job", job_id=job.id, _external=False),
            "events": url_for("api_v1.job_events", job_id=job.id, _external=False),
            "files":  url_for("api_v1.list_job_files", job_id=job.id, _external=False),
            "view":   url_for("main.job_viewer", job_id=job.id, _external=True),
        },
    }


# The set of files a v1 client is allowed to enumerate / download. Mapping
# from a stable artifact name to a relative path inside job_dir. We never
# leak arbitrary files from the dir; only this allowlist is exposed.
DOWNLOADABLE_ARTIFACTS = {
    "tree.newick":             "tree/tree_pruned.newick",
    "tree.original.newick":    "tree/tree_original.newick",
    "tree.nexus":              "tree/tree.nexus",
    "input.fasta":             "input/input_raw.fasta",
    "alignment.fasta":         "alignment/alignment_aligned.fasta",
    "trimmed.fasta":           "alignment/alignment_aligned_trimmed.fasta",
    "blast_results.json":      "blast_results.json",
    "input_info.json":         "input_info.json",
    "tree_state.json":         "tree/tree_state.json",
    "tree_metadata.json":      "tree/tree_metadata.json",
}


LOG_NAMES = {
    "pipeline":     "pipeline.log",
    "alignment":    "alignment.log",
    "tree_builder": "tree_builder.log",
}


def list_available_artifacts(job_id):
    """Return [{name, size, mime}, ...] for artifacts that actually exist."""
    base = Config.JOB_DIR / job_id
    out = []
    for name, rel in DOWNLOADABLE_ARTIFACTS.items():
        p = base / rel
        if p.exists() and p.is_file():
            out.append({
                "name": name,
                "size_bytes": p.stat().st_size,
                "mime": _guess_mime(name),
            })
    return out


def artifact_path(job_id, name):
    """Resolve an artifact name to an absolute Path, or None if unknown/missing."""
    rel = DOWNLOADABLE_ARTIFACTS.get(name)
    if not rel:
        return None
    p = Config.JOB_DIR / job_id / rel
    return p if p.exists() and p.is_file() else None


def _guess_mime(name):
    if name.endswith(".json"): return "application/json"
    if name.endswith(".fasta"): return "text/plain"
    if name.endswith(".newick") or name.endswith(".nexus"): return "text/plain"
    return "application/octet-stream"
