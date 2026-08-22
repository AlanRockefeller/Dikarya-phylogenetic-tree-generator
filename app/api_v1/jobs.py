"""Job-related helpers shared by /api/v1 routes."""
import json
from pathlib import Path

from flask import g, url_for

from app.config import Config
from app.models import Job
from app.services.artifact_storage import (
    artifact_exists,
    artifact_size,
    resolve_artifact,
)
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
            "alrt_replicates":  info.get("alrt_replicates"),
            "mcmc_generations": info.get("mcmc_generations"),
            "mcmc_nruns":       info.get("mcmc_nruns", Config.DEFAULT_MCMC_NRNS),
            "mcmc_nchains":     info.get("mcmc_nchains", Config.DEFAULT_MCMC_CHAINS),
            "mcmc_burnin_fraction": info.get(
                "mcmc_burnin_fraction", Config.DEFAULT_MCMC_BURNIN_FRACTION
            ),
            # False, not the current default: a job stored without this key ran
            # before the stop rule existed and must not be reported as using it.
            "mcmc_stop_early":  bool(info.get("mcmc_stop_early", False)),
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
    # These five pointed at paths the pipeline has never written, so the v1
    # downloads for them always 404'd. Verified against all 10,865 job dirs:
    # tree/tree.nexus, alignment/alignment_aligned.fasta,
    # alignment/alignment_aligned_trimmed.fasta, tree/tree_state.json and a
    # root-level blast_results.json exist in exactly zero of them.
    "tree.nexus":              "tree/tree_pruned.nexus",
    "input.fasta":             "input/input_raw.fasta",
    "alignment.fasta":         "alignment/alignment_raw.fasta",
    "trimmed.fasta":           "alignment/alignment_trimmed.fasta",
    "blast_results.json":      "blast/blast_results.json",
    "input_info.json":         "input_info.json",
    "tree_state.json":         "tree_state.json",
    "tree_metadata.json":      "tree/tree_metadata.json",
    "mrbayes.input.nexus":     "tree/mrbayes_input.nex",
    "mrbayes.parameters.p":    "tree/mrbayes_input.nex.p",
    "mrbayes.trees.t":         "tree/mrbayes_input.nex.t",
    "mrbayes.parameters.pstat": "tree/mrbayes_input.nex.pstat",
    "mrbayes.trees.tstat":     "tree/mrbayes_input.nex.tstat",
}

for _run_number in range(1, 9):
    DOWNLOADABLE_ARTIFACTS[f"mrbayes.run{_run_number}.p"] = (
        f"tree/mrbayes_input.nex.run{_run_number}.p"
    )
    DOWNLOADABLE_ARTIFACTS[f"mrbayes.run{_run_number}.t"] = (
        f"tree/mrbayes_input.nex.run{_run_number}.t"
    )


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
        p = _logical_artifact_path(base, name, rel)
        if artifact_exists(p):
            out.append({
                "name": name,
                # Report the uncompressed size: that is what a client
                # downloading this artifact actually receives, whether or not it
                # happens to be gzipped at rest.
                "size_bytes": artifact_size(p),
                "mime": _guess_mime(name),
            })
    return out


def artifact_path(job_id, name):
    """
    Resolve an artifact name to the absolute Path that holds it, or None if the
    name is unknown or nothing is on disk.

    May return a `.gz` path -- callers serve bytes via read_artifact_bytes
    rather than sending the file directly.
    """
    rel = DOWNLOADABLE_ARTIFACTS.get(name)
    if not rel:
        return None
    path = _logical_artifact_path(Config.JOB_DIR / job_id, name, rel)
    return resolve_artifact(path)


def _logical_artifact_path(base, name, rel):
    """Prefer the edited Nexus tree while retaining the original fallback."""
    path = base / rel
    if name == "tree.nexus" and not artifact_exists(path):
        return base / "tree" / "tree_original.nexus"
    return path


def _guess_mime(name):
    if name.endswith(".json"): return "application/json"
    if name.endswith(".fasta"): return "text/plain"
    if name.endswith((".newick", ".nexus", ".p", ".t", ".pstat", ".tstat")): return "text/plain"
    return "application/octet-stream"
