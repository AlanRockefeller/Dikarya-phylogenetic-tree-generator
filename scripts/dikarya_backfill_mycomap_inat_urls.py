#!/usr/bin/env python3
"""One-off recovery of the September 23 failed MycoMap observation field writes."""

import argparse
import datetime
import json
import re
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path

ROOT = Path('/var/www/dikarya')
sys.path.insert(0, str(ROOT))

from scripts.dikarya_whats_new import _ensure_secret_key, _load_env

PATTERN = re.compile(
    r'Could not write the new MycoMap URL to observation (\d+); '
    r'the tree is still built from BLAST (\d+)'
)
FAILED_DATE = '2026-09-23'
EXPECTED_ENTRIES = 36
EXPECTED_OBSERVATIONS = 34
EXPECTED_LEGACY_VALUES = {
    266453931: 'https://mycomap.com/app=genbank&module=genbank&controller=blast&do=results?db=42&id=536389',
}


def candidates():
    from app.models import Job
    from app.services.mycomap_service import validate_mycomap_url

    grouped = defaultdict(list)
    cutoff = datetime.datetime(2026, 9, 23, tzinfo=datetime.timezone.utc).timestamp()
    for directory in (ROOT / 'var/jobs').iterdir():
        if not directory.is_dir() or directory.stat().st_mtime < cutoff:
            continue
        log = directory / 'logs/pipeline.log'
        if not log.is_file():
            continue
        for line in log.open(errors='replace'):
            if not line.startswith(FAILED_DATE):
                continue
            match = PATTERN.search(line)
            if match:
                grouped[int(match.group(1))].append((directory.name, match.group(2)))

    entries = sum(map(len, grouped.values()))
    if entries != EXPECTED_ENTRIES or len(grouped) != EXPECTED_OBSERVATIONS:
        raise RuntimeError(f'Expected 36 log entries / 34 observations; found {entries} / {len(grouped)}')

    result = {}
    for observation_id, records in sorted(grouped.items()):
        urls = set()
        for job_id, blast_id in records:
            job = Job.query.filter_by(id=job_id).one_or_none()
            if job is None:
                raise RuntimeError(f'Missing job {job_id}')
            metrics = job.metrics or {}
            url = metrics.get('mycomap_blast_url')
            details = metrics.get('mycomap_blast_rerun') or {}
            info_path = Path(job.job_dir) / 'input_info.json'
            info = json.loads(info_path.read_text())
            if (int(metrics.get('inat_observation_id') or 0) != observation_id
                    or details.get('inat_mycomap_field_status') != 'failed'
                    or info.get('mycomap_blast_url') != url
                    or str(validate_mycomap_url(url)) != blast_id):
                raise RuntimeError(f'Job metadata mismatch for {job_id} / {observation_id}')
            urls.add(url)
        if len(urls) != 1:
            raise RuntimeError(f'Conflicting URLs for observation {observation_id}')
        result[observation_id] = urls.pop()
    return result


def current_fields(observation_ids):
    from app.services.inaturalist_tree_service import (
        INAT_API_BASE, MYCOMAP_BLAST_FIELD_NAME, _http_request,
        extract_observation_field_value,
    )
    query = urllib.parse.urlencode({
        'id': ','.join(str(x) for x in sorted(observation_ids)),
        'per_page': 200,
    })
    payload = _http_request(f'{INAT_API_BASE}/observations?{query}')
    observations = {int(x['id']): x for x in payload.get('results') or []}
    if set(observations) != set(observation_ids):
        raise RuntimeError(f'Observation response returned {len(observations)} / {len(observation_ids)}')
    return {
        oid: (extract_observation_field_value(observations[oid], MYCOMAP_BLAST_FIELD_NAME) or '').strip()
        for oid in observation_ids
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    _load_env()
    _ensure_secret_key()
    from app import create_app
    from app.services.inaturalist_tree_service import (
        MYCOMAP_BLAST_FIELD_NAME, extract_observation_field_value,
        fetch_observation, set_observation_field_value,
    )

    with create_app().app_context():
        wanted = candidates()
        existing = current_fields(wanted)
        missing = [oid for oid in wanted if not existing[oid]]
        legacy = [oid for oid in wanted if existing[oid] == EXPECTED_LEGACY_VALUES.get(oid)]
        already = [oid for oid in wanted if existing[oid] == wanted[oid]]
        conflicts = [oid for oid in wanted if existing[oid] and existing[oid] != wanted[oid] and oid not in legacy]
        summary = {
            'mode': 'apply' if args.apply else 'dry-run',
            'failed_writes': EXPECTED_ENTRIES,
            'unique_observations': len(wanted),
            'missing': len(missing),
            'legacy_to_replace': len(legacy),
            'already_correct': len(already),
            'conflicts': len(conflicts),
            'conflict_observation_ids': conflicts,
        }
        print(json.dumps(summary), flush=True)
        if conflicts:
            raise RuntimeError('Existing fields conflict; stopping without writes')
        if not args.apply:
            return summary

        outcomes = {'written': [], 'already_correct': already[:], 'conflict': [], 'failed': []}
        targets = missing + legacy
        for index, oid in enumerate(targets, 1):
            try:
                # Re-read each observation immediately before the write.
                present = (extract_observation_field_value(
                    fetch_observation(oid), MYCOMAP_BLAST_FIELD_NAME
                ) or '').strip()
                if present == wanted[oid]:
                    outcomes['already_correct'].append(oid)
                    print(f'{index}/{len(targets)} already correct: {oid}', flush=True)
                    continue
                if present and present != EXPECTED_LEGACY_VALUES.get(oid):
                    outcomes['conflict'].append(oid)
                    print(f'{index}/{len(targets)} conflicting field: {oid}', flush=True)
                    continue
                set_observation_field_value(oid, MYCOMAP_BLAST_FIELD_NAME, wanted[oid])
                outcomes['written'].append(oid)
                print(f'{index}/{len(targets)} wrote: {oid}', flush=True)
            except Exception as exc:
                outcomes['failed'].append({'observation_id': oid, 'error': str(exc)})
                print(f'{index}/{len(targets)} failed: {oid}: {exc}', flush=True)

        verified = current_fields(wanted)
        outcomes['verified_correct'] = [oid for oid in wanted if verified[oid] == wanted[oid]]
        outcomes['verified_missing'] = [oid for oid in wanted if not verified[oid]]
        outcomes['verified_conflict'] = [oid for oid in wanted if verified[oid] and verified[oid] != wanted[oid]]
        print(json.dumps(outcomes), flush=True)
        # A request may report an error after iNaturalist committed the write.
        # The fresh read-back is authoritative; keep attempt errors in the
        # result for audit, but fail only if the field is still wrong.
        if outcomes['verified_missing'] or outcomes['verified_conflict']:
            raise RuntimeError('Backfill completed with unverified observations')
        return outcomes


def check_worker_auth():
    """Verify the worker's existing site-wide iNaturalist authorization."""
    from app.services.inaturalist_oauth_service import is_authorized

    return {'authorized': is_authorized()}


def run_backfill():
    """RQ entry point; the worker inherits the production app environment."""
    return main(['--apply'])


if __name__ == '__main__':
    main()
