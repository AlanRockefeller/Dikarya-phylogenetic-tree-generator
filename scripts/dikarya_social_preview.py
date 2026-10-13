#!/usr/bin/env python3
"""Inspect, regenerate or export a job's link-preview card.

The card behind og:image (/job/<id>/social-preview.png) is drawn lazily on the
first request and cached in var/jobs/<id>/social/ until the tree or its state
changes; see app/services/social_preview_service.py. This script shows what
the card would contain and can redraw it without going through the web app:

    # What the page title and card would be, plus which tips are shown
    .venv/bin/python scripts/dikarya_social_preview.py aq7c

    # Write the card to a file to look at (does not touch the job's cache)
    .venv/bin/python scripts/dikarya_social_preview.py aq7c --out /tmp/aq7c.png

    # Discard the cached card and draw it again in place
    .venv/bin/python scripts/dikarya_social_preview.py aq7c --force

Regenerating the cache does not make Facebook refetch: it caches per URL. The
og:image URL changes by itself whenever the tree changes; otherwise use the
Sharing Debugger's "Scrape Again".
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.services import social_preview_service as preview  # noqa: E402
from app.services.security_utils import validate_job_id  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("job_id")
    parser.add_argument("--out", type=Path, help="write the card here instead of the cache")
    parser.add_argument("--force", action="store_true",
                        help="redraw the cached card even if it is current")
    args = parser.parse_args(argv)

    if not validate_job_id(args.job_id):
        print(f"not a job id: {args.job_id!r}", file=sys.stderr)
        return 2
    job_dir = Config.JOB_DIR / args.job_id
    if not job_dir.is_dir():
        print(f"no such job directory: {job_dir}", file=sys.stderr)
        return 1

    try:
        model = preview.build_preview_model(job_dir)
    except preview.PreviewUnavailable as exc:
        print(f"no card for this job ({exc}); the page uses the generic image")
        return 1
    print(f"title:     {model.title}")
    print(f"mode:      {model.mode}  ({model.shown} of {model.total} tips shown)")
    print(f"focal:     {model.labels[id(model.focal)] if model.focal else '(none)'}")
    print(f"signature: {preview.source_signature(job_dir)}")
    for tip in preview._tips_under(model.root):
        marker = "*" if tip is model.focal else " "
        print(f"  {marker} {model.labels[id(tip)]}")

    if args.out:
        args.out.write_bytes(preview.render_preview_png(model))
        print(f"wrote {args.out}")
    elif args.force:
        data, signature = preview.preview_png(job_dir, force=True)
        if data is None:
            print("redraw failed; see var/logs/errors.log", file=sys.stderr)
            return 1
        print(f"redrew {job_dir / preview.CACHE_DIRNAME} (signature {signature})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
