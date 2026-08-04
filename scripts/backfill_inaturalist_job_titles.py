#!/usr/bin/env python3
"""Backfill iNaturalist tree job titles with their observation genus."""

import argparse
import json
import re
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.dikarya_whats_new import (
    _ensure_secret_key,
    _load_env,
    _reexec_in_project_venv,
    _require_database_url,
)


OLD_TITLE_RE = re.compile(r"^iNaturalist obs (\d+) → Phylogenetic Tree$")
FETCH_CHUNK_SIZE = 150


def _build_app():
    _reexec_in_project_venv()
    _load_env()
    _require_database_url()
    _ensure_secret_key()
    from app import create_app

    return create_app()


def _matching_jobs():
    from app.models import Job

    matches = []
    for job in Job.query.order_by(Job.created_at).all():
        metrics = job.metrics if isinstance(job.metrics, dict) else {}
        notes = str(metrics.get("notes") or "")
        if metrics.get("via") == "inat_phylogenetic_tree" or OLD_TITLE_RE.fullmatch(notes):
            matches.append(job)
    return matches


def _observation_id(job) -> int:
    metrics = job.metrics if isinstance(job.metrics, dict) else {}
    value = metrics.get("inat_observation_id")
    if value:
        return int(value)
    match = OLD_TITLE_RE.fullmatch(str(metrics.get("notes") or ""))
    return int(match.group(1)) if match else 0


def _fetch_observations(observation_ids):
    from app.services.inaturalist_tree_service import INAT_API_BASE, _http_request

    observations = {}
    sorted_ids = sorted(observation_ids)
    for offset in range(0, len(sorted_ids), FETCH_CHUNK_SIZE):
        chunk = sorted_ids[offset:offset + FETCH_CHUNK_SIZE]
        query = urllib.parse.urlencode({
            "id": ",".join(str(value) for value in chunk),
            "per_page": 200,
        })
        payload = _http_request(f"{INAT_API_BASE}/observations?{query}")
        for observation in (payload or {}).get("results") or []:
            observations[int(observation["id"])] = observation
    return observations


def _legacy_genus(jobs, observation, observation_id):
    from app.services.inaturalist_tree_service import _extract_genus_from_inat_tip

    taxon = observation.get("taxon") or {}
    current_name = str(taxon.get("name") or "").strip().casefold()
    current_rank = str(taxon.get("rank") or "").strip().casefold()

    for metrics_key in ("inat_source_display_name", "inat_matched_its_tip"):
        for job in jobs:
            metrics = job.metrics if isinstance(job.metrics, dict) else {}
            genus = _extract_genus_from_inat_tip(metrics.get(metrics_key), observation_id)
            if not genus:
                continue
            if current_rank != "genus" and genus.casefold() == current_name:
                continue
            return genus

    for job in jobs:
        metrics = job.metrics if isinstance(job.metrics, dict) else {}
        for tip_name in metrics.get("inat_highlighted_tips") or []:
            genus = _extract_genus_from_inat_tip(tip_name, observation_id)
            if genus:
                return genus
    return ""


def _resolve_genera(jobs_by_observation, observations):
    from app.services.inaturalist_tree_service import _extract_inat_genus, _resolve_inat_genus

    genera = {}
    unresolved = []
    for observation_id, jobs in sorted(jobs_by_observation.items()):
        observation = observations.get(observation_id)
        if not observation:
            unresolved.append({"observation_id": observation_id, "reason": "not found"})
            continue

        genus = _extract_inat_genus(observation)
        if not genus:
            genus = _resolve_inat_genus(observation, observation_id)
        if not genus:
            genus = _legacy_genus(jobs, observation, observation_id)
        if genus:
            genera[observation_id] = genus
        else:
            taxon = observation.get("taxon") or {}
            unresolved.append({
                "observation_id": observation_id,
                "reason": "genus unavailable",
                "taxon": taxon.get("name"),
                "rank": taxon.get("rank"),
            })
    return genera, unresolved


def _update_input_info(job, title):
    from app.config import Config

    job_dir = Path(job.job_dir).resolve()
    jobs_root = Path(Config.JOB_DIR).resolve()
    if not job_dir.is_relative_to(jobs_root):
        return "unsafe"
    input_info_path = job_dir / "input_info.json"
    if not input_info_path.is_file():
        return "missing"
    try:
        payload = json.loads(input_info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    payload["notes"] = title
    temp_path = input_info_path.with_suffix(".json.tmp")
    try:
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(input_info_path)
    except PermissionError:
        return "permission_denied"
    except OSError:
        return "write_failed"
    return "updated"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit the title backfill")
    args = parser.parse_args(argv)

    app = _build_app()
    from app.extensions import db
    from app.services.inaturalist_tree_service import _build_inat_job_title

    with app.app_context():
        jobs = _matching_jobs()
        jobs_by_observation = defaultdict(list)
        for job in jobs:
            observation_id = _observation_id(job)
            if observation_id:
                jobs_by_observation[observation_id].append(job)

        observations = _fetch_observations(jobs_by_observation)
        genera, unresolved = _resolve_genera(jobs_by_observation, observations)
        if unresolved:
            print(json.dumps({"unresolved": unresolved}, indent=2))
            return 1

        preview = {
            str(observation_id): _build_inat_job_title(observation_id, genera[observation_id])
            for observation_id in (110793649, 134803150, 180881786, 360921334, 374117614)
            if observation_id in genera
        }
        summary = {
            "mode": "apply" if args.apply else "dry-run",
            "job_count": len(jobs),
            "observation_count": len(jobs_by_observation),
            "resolved_genus_count": len(genera),
            "preview": preview,
        }
        if not args.apply:
            print(json.dumps(summary, indent=2))
            return 0

        for job in jobs:
            observation_id = _observation_id(job)
            genus = genera[observation_id]
            metrics = dict(job.metrics or {})
            metrics["notes"] = _build_inat_job_title(observation_id, genus)
            metrics["inat_genus"] = genus
            job.metrics = metrics
        db.session.commit()

        input_info_counts = defaultdict(int)
        for job in jobs:
            observation_id = _observation_id(job)
            title = _build_inat_job_title(observation_id, genera[observation_id])
            input_info_counts[_update_input_info(job, title)] += 1

        summary["database_jobs_updated"] = len(jobs)
        summary["input_info"] = dict(sorted(input_info_counts.items()))
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
