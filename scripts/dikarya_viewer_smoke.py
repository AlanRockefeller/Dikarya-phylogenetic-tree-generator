#!/usr/bin/env python3
"""End-to-end smoke test for the tree viewer, against a live server, via curl.

WHY
---
On 2026-08-24 the viewer bootstrap died on a temporal-dead-zone ReferenceError
and /job/<id>/view hung on "loading" forever for every visitor. Note what an
ordinary uptime check saw during the outage:

    $ curl -o /dev/null -w '%{http_code}' https://dikarya.us/job/<id>/view
    200

The page was fine. The JavaScript was broken. Any check that stops at the HTTP
status of the HTML is blind to the entire class of failure that actually takes
this viewer down, because the viewer is a client-side application and the server
is merely handing over its parts.

So this script fetches the page, then fetches every script tag the page
references - at the exact ?v= URL production serves - and *executes* that
downloaded bundle through the same init harness the unit tests use
(tests/js/viewer_init_smoke.test.js).

That last step is the point. The unit tests check your working tree; this
checks what users are being served. Those differ exactly when it matters most -
during the 2026-08-24 incident the working tree was already fixed while
production still served the broken file, and a stale ?v= cache-buster can keep
serving a fixed file's old version indefinitely.

USAGE
    scripts/dikarya_viewer_smoke.py                          # default job
    scripts/dikarya_viewer_smoke.py --job <uuid>
    scripts/dikarya_viewer_smoke.py --base-url https://dikarya.us
    scripts/dikarya_viewer_smoke.py --quiet                  # cron/CI mode

Exits 0 if the viewer boots, 1 otherwise. Needs network egress, so agents run
it outside the sandbox.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tests" / "js" / "viewer_init_smoke.test.js"

DEFAULT_BASE = "https://dikarya.us"
# A small, public, long-lived job. Anything anonymous and viewable works; this
# is only the default so the script is runnable with no arguments.
DEFAULT_JOB = "3abfc9b9-5aea-4db6-acde-323148f41361"

# DOM anchors the controller reaches for during bootstrap. If the template stops
# emitting these, the viewer wires itself to nothing and silently does nothing.
REQUIRED_ELEMENT_IDS = ["tree-container", "status-message"]

SCRIPT_SRC_RE = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.I)


class Failure(Exception):
    pass


def curl(url, *, binary=False, timeout=45):
    """Fetch a URL with curl, returning (http_code, body, content_type)."""
    # Body and metadata are separated by a sentinel rather than parsed from
    # headers, so a Set-Cookie with a newline cannot confuse the split.
    sentinel = "===CURLMETA==="
    proc = subprocess.run(
        [
            "curl", "-sS", "--compressed", "--max-time", str(timeout),
            "-w", f"{sentinel}%{{http_code}}\t%{{content_type}}\t%{{size_download}}",
            url,
        ],
        capture_output=True,
        timeout=timeout + 15,
    )
    if proc.returncode != 0:
        raise Failure(
            f"curl failed for {url}: exit {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    raw = proc.stdout
    idx = raw.rfind(sentinel.encode())
    if idx == -1:
        raise Failure(f"curl produced no status line for {url}")
    body = raw[:idx]
    meta = raw[idx + len(sentinel):].decode("utf-8", "replace").split("\t")
    code, ctype = meta[0], (meta[1] if len(meta) > 1 else "")
    if not binary:
        body = body.decode("utf-8", "replace")
    return code, body, ctype


def check(results, name, ok, detail=""):
    results.append({"name": name, "ok": bool(ok), "detail": detail})
    return ok


def local_path_for(src_url):
    """Map a served /static/... URL back onto its repo-relative path.

    The harness loads scripts by repo-relative path, so the mirror of the
    downloaded bundle has to reproduce that layout.
    """
    path = urlparse(src_url).path
    marker = "/static/"
    if marker not in path:
        return None
    return "app/static/" + path.split(marker, 1)[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--job", default=DEFAULT_JOB)
    ap.add_argument("--quiet", action="store_true",
                    help="print only failures and a one-line verdict")
    ap.add_argument("--json", action="store_true", help="emit results as JSON")
    ap.add_argument("--keep", action="store_true",
                    help="keep the downloaded bundle for inspection")
    args = ap.parse_args()

    node = shutil.which("node")
    if not node:
        print("node is not installed; cannot execute the served bundle", file=sys.stderr)
        return 2

    results = []
    view_url = f"{args.base_url.rstrip('/')}/job/{args.job}/view"

    # --- 1. The page itself.
    try:
        code, html, ctype = curl(view_url)
    except Failure as exc:
        check(results, "fetch-viewer-page", False, str(exc))
        return report(results, args)

    if not check(results, "fetch-viewer-page", code == "200",
                 f"{view_url} returned HTTP {code}"):
        return report(results, args)
    check(results, "page-is-html", "html" in ctype.lower(),
          f"unexpected content-type: {ctype!r}")

    # --- 2. The DOM anchors the controller binds to.
    for el_id in REQUIRED_ELEMENT_IDS:
        check(results, f"page-has-#{el_id}",
              f'id="{el_id}"' in html or f"id='{el_id}'" in html,
              f"the template no longer emits #{el_id}; the controller would "
              f"bind to nothing")

    # --- 3. Every script tag, at the exact URL production serves.
    srcs = [s for s in SCRIPT_SRC_RE.findall(html)]
    if not check(results, "page-references-scripts", bool(srcs),
                 "the page referenced no external scripts at all"):
        return report(results, args)

    workdir = Path(tempfile.mkdtemp(prefix="dikarya-viewer-smoke-"))
    fetched = []
    try:
        for src in srcs:
            url = urljoin(view_url, src)
            rel = local_path_for(url)
            if rel is None:
                continue  # a CDN or inline-adjacent script; not ours to mirror
            try:
                code, body, ctype = curl(url, binary=True)
            except Failure as exc:
                check(results, f"fetch:{rel}", False, str(exc))
                continue
            ok = check(results, f"fetch:{rel}", code == "200",
                       f"{url} returned HTTP {code} - the page references an "
                       f"asset the server will not serve")
            if not ok:
                continue
            check(results, f"nonempty:{rel}", len(body) > 0,
                  f"{url} served an empty body")
            dest = workdir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
            fetched.append(rel)

            # Syntax-check what was actually served. Catches a truncated or
            # half-deployed file that the unit tests, reading the working tree,
            # would never see.
            proc = subprocess.run([node, "--check", str(dest)],
                                  capture_output=True, text=True)
            check(results, f"parses:{rel}", proc.returncode == 0,
                  f"the SERVED copy of {rel} is not valid JavaScript:\n"
                  f"{proc.stderr.strip()}")

        # --- 4. Execute the served bundle. The actual test.
        missing = [p for p in ("app/static/js/tree_viewer_controller.js",)
                   if p not in fetched]
        if check(results, "controller-was-served", not missing,
                 f"the page never referenced {missing}"):
            proc = subprocess.run(
                [node, str(HARNESS), str(workdir), "--json"],
                capture_output=True, text=True, timeout=180,
            )
            if proc.returncode != 0:
                check(results, "served-bundle-boots", False,
                      f"the harness could not run:\n{proc.stdout}\n{proc.stderr}")
            else:
                for r in json.loads(proc.stdout):
                    check(results, f"served/{r['name']}", r["ok"], r["detail"])
    finally:
        if args.keep:
            print(f"downloaded bundle kept at {workdir}", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    return report(results, args)


def report(results, args):
    if args.json:
        print(json.dumps(results, indent=2))
        return 0 if all(r["ok"] for r in results) else 1

    bad = [r for r in results if not r["ok"]]
    if not args.quiet:
        for r in results:
            print(f"  {'ok  ' if r['ok'] else 'FAIL'}  {r['name']}")
    for r in bad:
        print(f"\nFAIL {r['name']}\n{r['detail']}", file=sys.stderr)

    if bad:
        print(f"\nviewer smoke FAILED: {len(bad)} of {len(results)} checks",
              file=sys.stderr)
        return 1
    print(f"\nviewer smoke OK: {len(results)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
