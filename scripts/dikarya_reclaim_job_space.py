#!/usr/bin/env python3
"""
Reclaim disk space in var/jobs without losing anything a user can ask for.

Five independent passes, each safe to re-run and each skippable:

  scratch    Delete tree/*_input_sanitized.fasta -- header-sanitized copies of
             the trimmed alignment written purely as argv for the tree binary
             and never read back -- plus .recompute-*/ staging directories left
             behind when a worker restart killed a recompute mid-run.
  logs       Strip MAFFT's per-comparison progress chatter from
             logs/alignment.log. ~96% of those bytes; diagnostics are kept.
  json       Re-serialize tree_state.json and input_info.json compactly.
             Identical content, ~68% smaller for tree states.
  reports    gzip alignment/*_report.html (trimAl -htmlout, ~43x).
  alignments gzip the aligned/trimmed FASTAs (~30x).

The gzip passes depend on the readers in app/services/artifact_storage.py, so
this script must not run against a deployment that predates them.

MUST RUN AS THE `dikarya` USER -- var/jobs is dikarya-owned, and files created
by another user would be unreadable or unwritable by the web and worker
processes:

    sudo -u dikarya .venv/bin/python scripts/dikarya_reclaim_job_space.py --dry-run
    sudo -u dikarya .venv/bin/python scripts/dikarya_reclaim_job_space.py --apply

Dry run is the default. Jobs touched within --min-age-hours (default 24) are
skipped entirely so a live or recently finished run is never disturbed.
"""

import argparse
import gzip
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Reuse the production filter so the retroactive pass and the live pipeline
# agree exactly on what counts as MAFFT noise.
from app.services.alignment_service import _keep_mafft_log_line  # noqa: E402

GZIP_LEVEL = 6
MIN_COMPRESS_BYTES = 4096

SCRATCH_GLOB = "tree/*_input_sanitized.fasta"
RECOMPUTE_STAGING_GLOB = ".recompute-*"
REPORT_GLOB = "alignment/*_report.html"

COMPRESSIBLE_ALIGNMENTS = (
    "alignment/alignment_raw.fasta",
    "alignment/alignment_trimmed.fasta",
    "alignment/alignment_pruned_aligned.fasta",
    "alignment/alignment_pruned_trimmed.fasta",
)

# input/input_raw.fasta, tree_state.json and input_info.json are deliberately
# NOT compressed. Each is rewritten in place by a normal user action -- adding
# sequences, every tree-viewer edit, and recompute respectively -- so keeping
# them plain leaves every write path in the app untouched.

COMPACTABLE_JSON = ("tree_state.json", "input_info.json")

PASSES = ("scratch", "logs", "json", "reports", "alignments")


class Tally:
    def __init__(self):
        self.by_pass = {name: [0, 0] for name in PASSES}  # [files, bytes saved]
        self.jobs_visited = 0
        self.jobs_skipped_recent = 0
        self.errors = 0

    def add(self, pass_name, saved, count=1):
        entry = self.by_pass[pass_name]
        entry[0] += count
        entry[1] += saved


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0


def replace_atomically(path: Path, data) -> int:
    """Write data over path via a same-directory temp file, preserving mode.

    ``data`` is either a bytes object or an iterable of bytes chunks. The
    iterable form exists for alignment.log, which is the largest text artifact
    in a job and must never be materialized in full (see pass_logs). Returns the
    number of bytes written.
    """
    chunks = (data,) if isinstance(data, (bytes, bytearray)) else data
    written = 0
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".reclaim.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in chunks:
                written += handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, path.stat().st_mode & 0o7777)
        except OSError:
            os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return written


def gzip_in_place(path: Path, apply: bool) -> int:
    """
    Replace path with path.gz. Returns bytes saved.

    The .gz is renamed into place before the original is unlinked, so an
    interruption leaves a readable plain file plus a redundant archive -- never
    a truncated one. resolve_artifact() prefers the plain file, so that state is
    correct, just not yet compact; the next run finishes the job.
    """
    original = path.stat().st_size
    if original < MIN_COMPRESS_BYTES:
        return 0
    target = path.with_name(path.name + ".gz")

    if not apply:
        # Estimate from the first few MB rather than compressing the whole tree
        # twice. These artifacts are homogeneous (one FASTA record or one <span>
        # after another), so a head sample tracks the full-file ratio closely.
        with open(path, "rb") as src:
            sample = src.read(4 * 1024 * 1024)
        compressed_sample = len(gzip.compress(sample, GZIP_LEVEL))
        if not compressed_sample:
            return 0
        ratio = len(sample) / compressed_sample
        return int(original - original / ratio)

    # A partial .gz.tmp is invisible to every pass in this script -- pass_reports
    # globs *_report.html, pass_alignments uses fixed names, pass_scratch matches
    # neither -- so nothing would ever reclaim it. One full disk would leave a
    # half-written archive in each job it touched, permanently.
    tmp = target.with_name(target.name + ".tmp")
    try:
        with open(path, "rb") as src, gzip.open(tmp, "wb", compresslevel=GZIP_LEVEL) as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        shutil.copymode(path, tmp)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    saved = original - target.stat().st_size
    path.unlink()
    return saved


def _tree_bytes(root: Path) -> int:
    """Bytes held under root, not following symlinks (staging links to logs/)."""
    total = 0
    for base, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(base) / name
            try:
                total += path.lstat().st_size
            except OSError:
                continue
    return total


def pass_scratch(job_dir: Path, apply: bool, tally: Tally) -> None:
    for path in sorted(job_dir.glob(SCRATCH_GLOB)):
        size = path.stat().st_size
        if apply:
            path.unlink()
        tally.add("scratch", size)

    # recompute_tree() stages a whole run inside .recompute-XXXX/ and leaves
    # cleanup to TemporaryDirectory, which a SIGKILL skips -- and
    # restart-dikarya-worker SIGKILLs the work horse by design. A recompute
    # killed that way leaves the full staged alignment and tree behind with
    # nothing to reclaim it. Only jobs untouched for --min-age-hours reach
    # here, so a directory in scope can never belong to a live run.
    for path in sorted(job_dir.glob(RECOMPUTE_STAGING_GLOB)):
        if not path.is_dir():
            continue
        size = _tree_bytes(path)
        if apply:
            # ignore_errors keeps one unreadable staging directory from aborting
            # the whole run, but it also hides the failure. Re-measure instead of
            # trusting it: if anything survives, those bytes were not reclaimed
            # and the run was partial, which the exit status has to reflect.
            shutil.rmtree(path, ignore_errors=True)
            remaining = _tree_bytes(path) if path.exists() else 0
            if remaining or path.exists():
                tally.errors += 1
                print(f"  ! {job_dir.name} [scratch]: {path.name} not fully removed", file=sys.stderr)
            size -= remaining
        tally.add("scratch", size)


def _kept_log_chunks(path: Path):
    """Yield the encoded lines of an alignment log that survive filtering.

    Reproduces exactly what "\\n".join(kept) + "\\n" used to produce, including
    the empty result when nothing is kept, but without holding the file.
    """
    with open(path, encoding="utf-8", errors="replace") as handle:
        first = True
        for line in handle:
            line = line.rstrip("\n")
            # read_text().splitlines(), used by the original buffered version,
            # removes the CR in CRLF input. Preserve that normalization while
            # retaining the streaming implementation.
            if line.endswith("\r"):
                line = line[:-1]
            if not _keep_mafft_log_line(line):
                continue
            yield (line if first else "\n" + line).encode("utf-8")
            first = False
        if not first:
            yield b"\n"


def pass_logs(job_dir: Path, apply: bool, tally: Tally) -> None:
    path = job_dir / "logs" / "alignment.log"
    if not path.is_file():
        return
    original = path.stat().st_size
    if original < MIN_COMPRESS_BYTES:
        return
    # Two streaming passes rather than one buffered one. MAFFT chatter is ~96%
    # of these bytes (see the module docstring), so alignment.log is the biggest
    # text file in a job, and this script walks every job directory in a single
    # process: reading one into a string, then a list of lines, then a bytes copy
    # made peak memory several times the largest log on disk.
    kept_bytes = sum(len(chunk) for chunk in _kept_log_chunks(path))
    if kept_bytes >= original:
        return
    if apply:
        replace_atomically(path, _kept_log_chunks(path))
    tally.add("logs", original - kept_bytes)


def pass_json(job_dir: Path, apply: bool, tally: Tally) -> None:
    for name in COMPACTABLE_JSON:
        path = job_dir / name
        if not path.is_file():
            continue
        original = path.stat().st_size
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # A corrupt or unreadable state file is left exactly as found;
            # rewriting it would destroy the evidence.
            continue
        data = json.dumps(parsed, separators=(",", ":")).encode("utf-8")
        if len(data) >= original:
            continue
        if apply:
            replace_atomically(path, data)
        tally.add("json", original - len(data))


def pass_reports(job_dir: Path, apply: bool, tally: Tally) -> None:
    for path in sorted(job_dir.glob(REPORT_GLOB)):
        saved = gzip_in_place(path, apply)
        if saved:
            tally.add("reports", saved)


def pass_alignments(job_dir: Path, apply: bool, tally: Tally) -> None:
    for relative in COMPRESSIBLE_ALIGNMENTS:
        path = job_dir / relative
        if not path.is_file():
            continue
        saved = gzip_in_place(path, apply)
        if saved:
            tally.add("alignments", saved)


PASS_FUNCS = {
    "scratch": pass_scratch,
    "logs": pass_logs,
    "json": pass_json,
    "reports": pass_reports,
    "alignments": pass_alignments,
}

JOB_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def recently_touched(job_dir: Path, min_age_seconds: float) -> bool:
    """
    True if anything in the job was modified inside the age window.

    Checks the deepest mtime rather than just the directory's, because a run
    writing into tree/ does not necessarily bump the job directory itself.
    """
    cutoff = time.time() - min_age_seconds
    try:
        if job_dir.stat().st_mtime > cutoff:
            return True
        for child in job_dir.rglob("*"):
            try:
                if child.stat().st_mtime > cutoff:
                    return True
            except OSError:
                continue
    except OSError:
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jobs-dir", default=str(REPO_ROOT / "var" / "jobs"))
    parser.add_argument("--apply", action="store_true", help="Actually modify files (default is a dry run).")
    parser.add_argument("--dry-run", action="store_true", help="Explicit no-op mode; the default.")
    parser.add_argument(
        "--min-age-hours", type=float, default=24.0,
        help="Skip jobs with anything modified more recently than this (default: 24).",
    )
    parser.add_argument(
        "--passes", default=",".join(PASSES),
        help=f"Comma-separated subset of: {', '.join(PASSES)}",
    )
    parser.add_argument("--limit", type=int, default=0, help="Process at most N jobs (0 = all).")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    apply = args.apply

    selected = [p.strip() for p in args.passes.split(",") if p.strip()]
    unknown = [p for p in selected if p not in PASS_FUNCS]
    if unknown:
        parser.error(f"Unknown pass(es): {', '.join(unknown)}. Valid: {', '.join(PASSES)}")

    jobs_root = Path(args.jobs_dir)
    if not jobs_root.is_dir():
        parser.error(f"Not a directory: {jobs_root}")

    if apply and not os.access(jobs_root, os.W_OK):
        sys.exit(
            f"{jobs_root} is not writable by uid {os.getuid()}.\n"
            "Re-run as the dikarya user:\n"
            f"  sudo -u dikarya {sys.executable} {' '.join(sys.argv)}"
        )

    min_age_seconds = args.min_age_hours * 3600.0
    tally = Tally()

    job_dirs = sorted(d for d in jobs_root.iterdir() if d.is_dir() and JOB_ID_RE.match(d.name))
    if args.limit:
        job_dirs = job_dirs[: args.limit]

    started = time.time()
    for index, job_dir in enumerate(job_dirs, 1):
        if recently_touched(job_dir, min_age_seconds):
            tally.jobs_skipped_recent += 1
            continue
        tally.jobs_visited += 1
        for name in selected:
            try:
                PASS_FUNCS[name](job_dir, apply, tally)
            except OSError as exc:
                tally.errors += 1
                print(f"  ! {job_dir.name} [{name}]: {exc}", file=sys.stderr)
        if not args.quiet and index % 500 == 0:
            done = sum(v[1] for v in tally.by_pass.values())
            print(f"  ... {index}/{len(job_dirs)} jobs, {human(done)} so far", file=sys.stderr)

    total = sum(v[1] for v in tally.by_pass.values())
    mode = "RECLAIMED" if apply else "WOULD RECLAIM (dry run)"
    print()
    print(f"{mode}: {human(total)}")
    print(f"{'pass':<12} {'files':>8} {'saved':>12}")
    print("-" * 34)
    for name in selected:
        files, saved = tally.by_pass[name]
        print(f"{name:<12} {files:>8} {human(saved):>12}")
    print("-" * 34)
    print(
        f"jobs processed: {tally.jobs_visited}   "
        f"skipped (active within {args.min_age_hours}h): {tally.jobs_skipped_recent}   "
        f"errors: {tally.errors}"
    )
    print(f"elapsed: {time.time() - started:.1f}s")
    if not apply:
        print("\nDry run -- nothing was modified. Re-run with --apply to reclaim.")
    # This runs weekly from cron (ops/cron/dikarya-reclaim-job-space). Exiting 0
    # after counting failures means a run that reclaimed nothing looks identical
    # to a clean one, and the space quietly stops coming back.
    return 1 if tally.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
