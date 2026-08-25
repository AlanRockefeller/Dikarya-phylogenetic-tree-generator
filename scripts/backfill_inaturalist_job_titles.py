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
# How many resolved titles the dry run prints so an operator can eyeball them
# before committing. Sampled from the real resolved set, not a fixed list.
PREVIEW_LIMIT = 10


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
    """The observation this job is about, or 0 when there is not a usable one.

    metrics is a free-form JSON column, so inat_observation_id can be a string,
    a float, or something that is not a number at all. Anything unusable is 0 --
    the same answer as "absent" -- so main() has exactly one condition to skip
    on, rather than a skip for the missing case and a crash for the malformed one.
    """
    metrics = job.metrics if isinstance(job.metrics, dict) else {}
    value = metrics.get("inat_observation_id")
    if value:
        try:
            if isinstance(value, bool):
                raise ValueError("boolean is not an observation id")
            observation_id = int(value)
            if isinstance(value, float) and not value.is_integer():
                raise ValueError("fractional observation id")
        except (TypeError, ValueError, OverflowError):
            observation_id = 0
        if observation_id > 0:
            return observation_id
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


def _update_input_info(job, title, journal=None):
    """Rewrite one job's on-disk notes, recording what to undo it with.

    When ``journal`` is given, the file's previous bytes are appended to it as
    ``(path, original_bytes)`` on every successful write, so a later failure
    can put the artifact back. See main() for why the ordering matters.
    """
    from app.config import Config

    job_dir = Path(job.job_dir).resolve()
    jobs_root = Path(Config.JOB_DIR).resolve()
    if not job_dir.is_relative_to(jobs_root):
        return "unsafe"
    input_info_path = job_dir / "input_info.json"
    if not input_info_path.is_file():
        return "missing"
    try:
        original = input_info_path.read_bytes()
        payload = json.loads(original.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "invalid"
    payload["notes"] = title
    temp_path = input_info_path.with_suffix(".json.tmp")

    def discard_temp():
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(input_info_path)
    except PermissionError:
        discard_temp()
        return "permission_denied"
    except OSError:
        discard_temp()
        return "write_failed"
    if journal is not None:
        journal.append((input_info_path, original))
    return "updated"


def _restore_input_info(journal):
    """Put every journalled artifact back. Returns the paths that would not."""
    failures = []
    for path, original in journal:
        temp_path = path.with_suffix(".json.tmp")
        try:
            temp_path.write_bytes(original)
            temp_path.replace(path)
        except OSError:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            failures.append(str(path))
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit the title backfill")
    args = parser.parse_args(argv)

    app = _build_app()
    from app.extensions import db
    from app.services.inaturalist_tree_service import _build_inat_job_title

    with app.app_context():
        # Filter once, here, so the preview, the database loop and the
        # input_info loop all operate on the same population. _matching_jobs()
        # accepts any job tagged via=inat_phylogenetic_tree, including ones that
        # carry neither an observation id nor a legacy title; _observation_id()
        # returns 0 for those, they never enter `genera`, and the apply loops
        # used to raise KeyError on them -- the second one after the commit had
        # already renamed the database rows, leaving input_info.json behind.
        jobs = []
        jobs_by_observation = defaultdict(list)
        skipped_without_observation = 0
        for job in _matching_jobs():
            observation_id = _observation_id(job)
            if not observation_id:
                skipped_without_observation += 1
                continue
            jobs.append(job)
            jobs_by_observation[observation_id].append(job)

        observations = _fetch_observations(jobs_by_observation)
        genera, unresolved = _resolve_genera(jobs_by_observation, observations)
        if unresolved:
            print(json.dumps({"unresolved": unresolved}, indent=2))
            return 1

        # Built from the observations actually resolved in this run. A fixed
        # list of ids produced an empty preview on any dataset that did not
        # happen to contain them, which is every dataset but the author's.
        preview = {
            str(observation_id): _build_inat_job_title(observation_id, genera[observation_id])
            for observation_id in sorted(genera)[:PREVIEW_LIMIT]
        }
        summary = {
            "mode": "apply" if args.apply else "dry-run",
            "job_count": len(jobs),
            "observation_count": len(jobs_by_observation),
            "resolved_genus_count": len(genera),
            "skipped_without_observation": skipped_without_observation,
            "preview": preview,
        }
        if not args.apply:
            print(json.dumps(summary, indent=2))
            return 0

        # Artifact first, database second, and only for the jobs whose artifact
        # actually followed. Committing the rows first and rewriting
        # input_info.json afterwards meant a write failure -- a read-only
        # directory, a job whose file had been removed -- left the run
        # reporting failure with the two stores permanently disagreeing about
        # the same job's title, and nothing to undo it with. Now a job whose
        # file could not be rewritten simply keeps its old title in both
        # places, and a failed commit restores every file this run touched.
        journal = []
        input_info_counts = defaultdict(int)
        updated_jobs = []
        for job in jobs:
            observation_id = _observation_id(job)
            genus = genera[observation_id]
            title = _build_inat_job_title(observation_id, genus)
            status = _update_input_info(job, title, journal=journal)
            input_info_counts[status] += 1
            if status != "updated":
                continue
            metrics = dict(job.metrics or {})
            metrics["notes"] = title
            metrics["inat_genus"] = genus
            job.metrics = metrics
            updated_jobs.append(job)

        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            summary["database_jobs_updated"] = 0
            summary["input_info"] = dict(sorted(input_info_counts.items()))
            summary["error"] = f"database commit failed: {type(exc).__name__}"
            summary["input_info_restore_failures"] = _restore_input_info(journal)
            print(json.dumps(summary, indent=2))
            return 1

        summary["database_jobs_updated"] = len(updated_jobs)
        summary["input_info"] = dict(sorted(input_info_counts.items()))
        print(json.dumps(summary, indent=2))
        # Every job counted here kept its old title in *both* stores, so the two
        # never disagree -- but the operator still asked for a backfill that did
        # not fully happen, so the run must not report success.
        failed = sum(count for status, count in input_info_counts.items() if status != "updated")
        if failed:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
