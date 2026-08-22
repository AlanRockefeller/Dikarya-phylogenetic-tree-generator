#!/usr/bin/env python3
"""Seed the Claude review usage log from reviews already cached on disk.

The usage log only exists from the moment it was added, but every review ever
produced left a `analysis/claude_review.json` behind carrying its own usage and
timestamp. This replays those into the log so the monitoring page has history
on day one.

Only reviews missing from the log are added, keyed on (job_id, ts), so it is
safe to run more than once. Must run as a user that can write var/logs --
in practice `dikarya`.

    .venv/bin/python scripts/dikarya_backfill_review_usage.py --dry-run
    .venv/bin/python scripts/dikarya_backfill_review_usage.py --apply
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOG = ROOT / "var" / "logs" / "claude_reviews.jsonl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.error("pass --dry-run or --apply")

    existing = set()
    if LOG.is_file():
        for line in LOG.read_text().splitlines():
            try:
                rec = json.loads(line)
                existing.add((rec.get("job_id"), rec.get("ts")))
            except ValueError:
                continue

    new = []
    for path in sorted((ROOT / "var" / "jobs").glob("*/analysis/claude_review.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        job_id = path.parent.parent.name
        ts = payload.get("generated_at")
        if ts is None or (job_id, ts) in existing:
            continue
        usage = payload.get("usage") or {}
        new.append({
            "ts": ts,
            "job_id": job_id,
            "model": payload.get("model"),
            "effort": None,
            "backend": "cli",
            "elapsed_seconds": payload.get("elapsed_seconds"),
            "cost_usd": usage.get("cost_usd"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
            "rating": (payload.get("review") or {}).get("overall_rating"),
            "backfilled": True,
        })

    new.sort(key=lambda r: r["ts"])
    print(f"{len(existing)} already logged, {len(new)} to add")
    for rec in new:
        print(f"  + {rec['job_id'][:8]} {rec['model']} ${rec.get('cost_usd')}")
    if not args.apply or not new:
        return

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as handle:
        for rec in new:
            handle.write(json.dumps(rec) + "\n")
    print(f"appended {len(new)} records to {LOG}")


if __name__ == "__main__":
    main()
