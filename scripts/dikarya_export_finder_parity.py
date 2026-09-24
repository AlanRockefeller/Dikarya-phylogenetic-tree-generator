#!/usr/bin/env python3
"""Export candidate-plan parity fixtures from the canonical ``inat_finder.py``.

The browser finder in ``app/static/js/inat_finder.js`` reimplements
``inat.finder.py``'s auto-mode candidate ladder in JavaScript so the search can
run in the visitor's browser and talk to iNaturalist directly.  Two independent
implementations of the same combinatorial enumeration drift silently: a plan
that yields the candidates in a different *order* still finds the same
observations, but it breaks resuming a paused deep search, and a plan that
yields a slightly different *set* changes what the site will and will not find.

This writes both facts into a fixture the Node test suite reads:

* the exact per-stage totals, class sizes and human label, and
* a SHA-256 of the ordered candidate sequence,

so any divergence in either content or order fails a test instead of quietly
shipping.  Re-run it after syncing ``inat_finder.py`` to a new release:

    .venv/bin/python scripts/dikarya_export_finder_parity.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "inat_finder_candidate_parity.json"

# Numbers chosen to exercise the branches that decide a plan's shape: the
# length switches for insertions (< 9 digits) and removals (> 5 digits), repeated
# adjacent digits (which transpositions skip), and a trailing-zero number whose
# removals collide with each other.
CASES = ["12345", "123456", "1223334", "123456789", "1000000000", "111", "987654321"]
MAX_DIGITS = 3


def _stub_progress_bar():
    """Satisfy the CLI's ``tqdm`` import without adding it as a dependency.

    Only the candidate generators are exercised here, and none of them draws a
    progress bar. Requiring tqdm in requirements.txt so a fixture script can
    import a file the web app never imports would be the wrong trade.
    """
    if "tqdm" in sys.modules:
        return
    module = types.ModuleType("tqdm")

    class _Bar:  # pragma: no cover - never instantiated by this script
        def __init__(self, *args, **kwargs):
            raise RuntimeError("the parity export must not run a progress bar")

    module.tqdm = _Bar
    sys.modules["tqdm"] = module


def load_cli():
    """Import the vendored CLI by path; it is a script, not an installed module."""
    _stub_progress_bar()
    spec = importlib.util.spec_from_file_location("inat_finder_cli", REPO / "inat_finder.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def describe_plan(cli, number, digits_off):
    plan = cli.build_candidate_plan(number, digits_off)
    digest = hashlib.sha256()
    count = 0
    first = []
    for candidate in plan:
        digest.update(candidate.encode("ascii"))
        digest.update(b"\n")
        if count < 12:
            first.append(candidate)
        count += 1
    return {
        "digits_off": digits_off,
        "total": plan.total,
        "yielded": count,
        "replacement_count": plan.replacement_count,
        "additions": len(plan.additions),
        "removals": len(plan.removals),
        "transpositions": len(plan.transpositions),
        "extras": len(plan.extras),
        "label": cli.auto_stage_label(digits_off, plan),
        "first_candidates": first,
        "sha256": digest.hexdigest(),
    }


def describe_stages(cli, number):
    """Walk the ladder exactly as run_auto_mode() does, sharing one seen set."""
    seen = set()
    stages = []
    for index in range(1, MAX_DIGITS + 1):
        plan = cli.build_candidate_plan(number, index)
        stage = cli.AutoStage(index, plan, seen)
        digest = hashlib.sha256()
        yielded = 0
        for candidate in stage:
            digest.update(candidate.encode("ascii"))
            digest.update(b"\n")
            yielded += 1
        stages.append(
            {
                "index": index,
                "total": stage.total,
                "yielded": yielded,
                "label": stage.label,
                "seen_after": len(seen),
                "sha256": digest.hexdigest(),
            }
        )
    return stages


def main():
    cli = load_cli()
    payload = {
        "generated_from": f"inat_finder.py {cli.VERSION}",
        "candidate_generation_version": cli.CANDIDATE_GENERATION_VERSION,
        "large_search_threshold": cli.LARGE_SEARCH_THRESHOLD,
        "auto_default_max_digits": cli.AUTO_DEFAULT_MAX_DIGITS,
        "batch_size": cli.BATCH_SIZE,
        "max_consecutive_failed_batches": cli.MAX_CONSECUTIVE_FAILED_BATCHES,
        "cases": [
            {
                "number": number,
                "plans": [describe_plan(cli, number, digits) for digits in range(1, MAX_DIGITS + 1)],
                "stages": describe_stages(cli, number),
            }
            for number in CASES
        ],
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {FIXTURE.relative_to(REPO)} from {payload['generated_from']}")


if __name__ == "__main__":
    main()
