#!/usr/bin/env python3
"""Print the Claude review system prompt or response schema on stdout.

The sudo wrapper reads both from /etc/dikarya/claude-review/, which is root
owned so the web process cannot rewrite the reviewer's own instructions. That
means the files are a copy of what lives in tree_analysis_service.py, and a copy
can go stale. This script is the one supported way to refresh them:

    sudo mkdir -p /etc/dikarya/claude-review
    .venv/bin/python scripts/dikarya_export_review_prompt.py system \\
        | sudo tee /etc/dikarya/claude-review/system_prompt.txt >/dev/null
    .venv/bin/python scripts/dikarya_export_review_prompt.py schema \\
        | sudo tee /etc/dikarya/claude-review/schema.json >/dev/null

Re-run both after changing SYSTEM_PROMPT or RESPONSE_SCHEMA, and bump
REVIEW_SCHEMA_VERSION at the same time so already-cached reviews are discarded
rather than shown alongside a prompt that no longer produced them.

`--check` compares the installed copies against the code and exits non-zero on
a mismatch, which is what you want in a deploy script.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.tree_analysis_service import (  # noqa: E402
    RESPONSE_SCHEMA,
    REVIEW_SCHEMA_VERSION,
    SYSTEM_PROMPT,
)

INSTALL_DIR = Path("/etc/dikarya/claude-review")
FILES = {"system": "system_prompt.txt", "schema": "schema.json"}


def rendered(what: str) -> str:
    return SYSTEM_PROMPT if what == "system" else json.dumps(RESPONSE_SCHEMA)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("what", choices=["system", "schema"], nargs="?")
    parser.add_argument(
        "--check", action="store_true",
        help="Compare installed copies against the code; exit 1 if they differ.",
    )
    parser.add_argument("--install-dir", type=Path, default=INSTALL_DIR)
    args = parser.parse_args()

    if args.check:
        stale = []
        for what, filename in FILES.items():
            path = args.install_dir / filename
            try:
                on_disk = path.read_text()
            except OSError as exc:
                print(f"MISSING {path}: {exc}", file=sys.stderr)
                stale.append(what)
                continue
            if on_disk.strip() != rendered(what).strip():
                print(f"STALE   {path}", file=sys.stderr)
                stale.append(what)
            else:
                print(f"ok      {path}")
        if stale:
            print(
                f"\nRe-export: {', '.join(sorted(stale))} "
                f"(review schema version {REVIEW_SCHEMA_VERSION})",
                file=sys.stderr,
            )
            return 1
        return 0

    if not args.what:
        parser.error("give 'system' or 'schema', or pass --check")
    # No trailing newline on the schema: the wrapper feeds it straight to
    # --json-schema, and the CLI is happier with exactly the JSON.
    sys.stdout.write(rendered(args.what))
    if args.what == "system":
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
