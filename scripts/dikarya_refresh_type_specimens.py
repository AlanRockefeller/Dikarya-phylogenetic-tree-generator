#!/usr/bin/env python3
"""
Refresh the type-specimen reference data the tree viewer marks tips from.

Three independent passes (see app/services/type_specimen_service.py):

  mycomap   Download MycoMap's full type-specimen list
            (https://mycomap.org/api/type-specimens, public, no key needed) and
            replace mycomap_type_specimens.json. Refuses to replace a snapshot
            with one more than 10% smaller unless --force, so a half-failed
            API or an emptied table cannot silently wipe the markers.
  genbank   Backfill GenBank's own /type_material answers for accessions that
            already sit in job inputs: every RefSeq NR_ record plus any header
            that mentions a type word, minus what either source already knows.
            New fetches record themselves as they happen, so this only matters
            for jobs that predate the feature (or with --since-days, for the
            last week's jobs whose BLAST headers came from MycoMap).
  curation  Ask MycoMap.org's genbank-curation endpoint about every GenBank
            accession in job inputs and record its isType flag. Curators
            designate types there (typeBasis "manual") that never reach the
            type-specimen list; only tss.TRUSTED_CURATION_BASES are recorded
            ("source" is a keyword match). The flag can change at any time, so
            accessions are re-asked every run rather than skipped once known.
            Bounded by --max-curation-accessions (100 per request) and
            --curation-max-minutes. A run stopped by either one records where
            it got to in curation_cursor.json (per --since-days), and the
            next run continues from there, wrapping round.

Output lives in Config.TYPE_SPECIMEN_DIR (cache/type_specimens, group dikarya,
2775), which the web process reads and the worker appends to.

Every run except --dry-run also appends its log to cache/type_specimens/
refresh.log (type_specimen_service.REFRESH_LOG_PATH): per-pass statistics plus
one event=type_specimens.added / removed / reclassified line per accession.
scripts/dikarya_log_digest.py turns those into its "Type specimens" section.

    .venv/bin/python scripts/dikarya_refresh_type_specimens.py --dry-run
    .venv/bin/python scripts/dikarya_refresh_type_specimens.py
    .venv/bin/python scripts/dikarya_refresh_type_specimens.py --passes genbank --since-days 8

API notes, including why the paging sorts by accession and sends a User-Agent,
are in mycomap.org.type.specimen.api.txt at the repository root.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import Config  # noqa: E402
from app.services import type_specimen_service as tss  # noqa: E402
from app.services.api_diagnostics import diagnostic_urlopen  # noqa: E402
from app.services.artifact_storage import default_file_mode  # noqa: E402

MYCOMAP_URL = "https://mycomap.org/api/type-specimens"
# Cloudflare answers Python's default "Python-urllib/3.x" agent with a 403.
USER_AGENT = "dikarya-type-specimen-refresh (+https://dikarya.us)"
PAGE_SIZE = 200  # the API's maximum; larger values are silently capped
SHRINK_LIMIT = 0.90
GENBANK_BATCH = 100
# An accession NCBI returned nothing for is asked about again only after this
# long, and after every accession never asked about.
MISSING_RETRY_DAYS = 30
TYPE_WORD_RE = re.compile(
    r"\b(holotype|isotype|epitype|lectotype|neotype|paratype|syntype|ex-type|"
    r"TYPE material|type strain|type)\b",
    re.IGNORECASE,
)
NEGATED_TYPE_RE = re.compile(r"\b(?:non|not|no)[\s-]*(?:an?\s+)?\w*type\b", re.IGNORECASE)


# Opened by main() unless --dry-run: every log line is also appended to
# tss.REFRESH_LOG_PATH, which the log digest reads to report what was added.
_refresh_log = None


def log(message):
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {message}"
    print(line, flush=True)
    if _refresh_log is not None:
        _refresh_log.write(line + "\n")
        _refresh_log.flush()


def _event_value(value):
    text = " ".join(str(value if value is not None else "").split())
    # Bare when unambiguous, JSON-quoted otherwise; the digest reads both.
    return text if text and not re.search(r'[\s"=\\]', text) else json.dumps(text, ensure_ascii=False)


def event(name, **fields):
    """One machine-readable line; scripts/dikarya_log_digest.py parses these."""
    log(f"event=type_specimens.{name} "
        + " ".join(f"{key}={_event_value(value)}" for key, value in fields.items()))


def marked_accessions():
    """Accessions the viewer currently marks as types, from any source."""
    genbank = {acc for acc, entry in tss.genbank_index().items() if entry.get("status")}
    curation = {acc for acc, entry in tss.curation_index().items() if tss.curation_marks(entry)}
    return genbank | set(tss.mycomap_index()) | curation


# --------------------------------------------------------------------------
# mycomap pass
# --------------------------------------------------------------------------

def fetch_mycomap_page(page):
    # sortBy=accessionNumber: the default organism sort has ties whose order
    # changes between requests, and paging through it skipped 26 records and
    # duplicated 26 others while the total still matched.
    query = urlencode({"includeGenbank": "true", "sortBy": "accessionNumber",
                       "page": page, "limit": PAGE_SIZE})
    request = urllib.request.Request(
        f"{MYCOMAP_URL}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    for attempt in range(4):
        try:
            # Archives a failed or malformed reply (e.g. a Cloudflare 403) under
            # var/logs/api-responses, as every upstream call here must.
            with diagnostic_urlopen(request, timeout=60) as response:
                return json.load(response)
        except Exception as exc:
            if attempt == 3:
                raise RuntimeError(f"MycoMap page {page} failed: {exc}") from exc
            time.sleep(2 * (attempt + 1))


def download_mycomap():
    first = fetch_mycomap_page(1)
    pagination = first["pagination"]
    rows = list(first["specimens"])
    for page in range(2, int(pagination["totalPages"]) + 1):
        time.sleep(0.2)
        rows.extend(fetch_mycomap_page(page)["specimens"])
    unique = {row["id"]: row for row in rows}
    if len(unique) != int(pagination["total"]):
        raise RuntimeError(
            f"MycoMap returned {len(unique)} unique records but reported "
            f"{pagination['total']}; not replacing the snapshot"
        )
    return list(unique.values())


def build_snapshot(rows):
    return tss.build_mycomap_snapshot(rows)


def write_atomically(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        # mkstemp creates 0600; the web process must be able to read this.
        os.chmod(staged, path.stat().st_mode & 0o777 if path.exists() else default_file_mode())
        os.replace(staged, path)
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise


def run_mycomap(args):
    log("mycomap: downloading the type-specimen list")
    rows = download_mycomap()
    records, skipped = build_snapshot(rows)
    log(f"mycomap: {len(rows)} rows -> {len(records)} type accessions "
        f"(skipped {skipped['not_an_accession']} non-accessions, "
        f"{skipped['not_type_material']} non-type rows)")

    path = tss.DATA_DIR / tss.MYCOMAP_SNAPSHOT_NAME
    old_records = tss.mycomap_index()
    previous = len(old_records)
    if previous and len(records) < previous * SHRINK_LIMIT and not args.force:
        raise RuntimeError(
            f"new snapshot has {len(records)} accessions against {previous} before; "
            f"refusing to shrink it by more than {100 - SHRINK_LIMIT * 100:.0f}% (use --force)"
        )
    genbank_types = {acc for acc, entry in tss.genbank_index().items() if entry.get("status")}
    if args.dry_run:
        log(f"mycomap: dry run, would write {path} (previously {previous} accessions)")
        report_mycomap_changes(old_records, records, len(rows), genbank_types)
        return
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": MYCOMAP_URL,
        "rows": len(rows),
        "records": records,
    }
    write_atomically(path, json.dumps(payload, ensure_ascii=False, sort_keys=True))
    log(f"mycomap: wrote {path} ({len(records)} accessions, previously {previous})")
    report_mycomap_changes(old_records, records, len(rows), genbank_types)


def report_mycomap_changes(old_records, records, rows, genbank_types):
    added = sorted(set(records) - set(old_records))
    removed = sorted(set(old_records) - set(records))
    reclassified = sorted(
        acc for acc in set(records) & set(old_records)
        if records[acc].get("status") != old_records[acc].get("status")
    )
    initial = not old_records
    event("mycomap_summary", rows=rows, accessions=len(records), previous=len(old_records),
          added=len(added), removed=len(removed), reclassified=len(reclassified),
          initial="yes" if initial else "no")
    if initial:
        # The first snapshot "adds" the whole list; naming 30,000 accessions
        # would bury every later report, so only the totals are recorded.
        return
    for acc in added:
        record = records[acc]
        event("added", source="mycomap", accession=acc, status=record.get("status"),
              organism=record.get("organism"), voucher=record.get("voucher"),
              previously_marked="yes" if acc in genbank_types else "no")
    for acc in removed:
        record = old_records[acc]
        event("removed", source="mycomap", accession=acc, status=record.get("status"),
              organism=record.get("organism"),
              still_marked="yes" if acc in genbank_types else "no")
    for acc in reclassified:
        event("reclassified", source="mycomap", accession=acc,
              previous_status=old_records[acc].get("status"), status=records[acc].get("status"),
              organism=records[acc].get("organism"))


# --------------------------------------------------------------------------
# genbank pass
# --------------------------------------------------------------------------

def _scan_job_accessions(since_days, keep):
    """Accessions in job inputs (changed within since_days, 0 = all) that keep() accepts.

    ``keep(header, accession)`` decides per header. One scan for every pass, so
    the cutoff, the mtime check and the handling of unreadable jobs cannot drift
    apart between them.
    """
    from app.services.sequence_dedup_service import record_accession

    cutoff = time.time() - since_days * 86400 if since_days else None
    accessions = set()
    jobs = 0
    for input_dir in Config.JOB_DIR.glob("*/input"):
        try:
            if cutoff and (input_dir / "input_raw.fasta").stat().st_mtime < cutoff:
                continue
            headers = list(tss.job_headers(input_dir.parent))
        except OSError:
            continue
        jobs += 1
        for header in headers:
            acc = tss.accession_root(record_accession(header, {}))
            if acc and keep(header, acc):
                accessions.add(acc)
    return accessions, jobs


def candidate_accessions(since_days):
    return _scan_job_accessions(since_days, lambda header, acc: acc.startswith("NR_") or (
        TYPE_WORD_RE.search(header) and not NEGATED_TYPE_RE.search(header)))


def run_genbank(args):
    from app.services.blast_service import _fetch_genbank_xml_batch, _parse_genbank_xml

    candidates, jobs = candidate_accessions(args.since_days)
    known_genbank = tss.genbank_index()
    known_mycomap = tss.mycomap_index()
    retry_before = (datetime.now(timezone.utc) - timedelta(days=MISSING_RETRY_DAYS)).strftime("%Y-%m-%d")
    todo, stale_missing = [], []
    for acc in sorted(candidates):
        if acc in known_mycomap:
            continue
        entry = known_genbank.get(acc)
        if entry is None:
            todo.append(acc)
        elif entry.get("missing") and str(entry.get("recorded") or "") < retry_before:
            stale_missing.append(acc)
    # Never-asked accessions first, so dead ones cannot crowd them out of the
    # --max-accessions budget run after run.
    todo.extend(stale_missing)
    log(f"genbank: {jobs} jobs scanned, {len(candidates)} candidate accessions, "
        f"{len(todo) - len(stale_missing)} not yet known to either source, "
        f"{len(stale_missing)} missing from NCBI over {MISSING_RETRY_DAYS} days ago")
    deferred = 0
    if args.max_accessions and len(todo) > args.max_accessions:
        log(f"genbank: limiting this run to {args.max_accessions}; the rest are picked up next time")
        deferred = len(todo) - args.max_accessions
        todo = todo[:args.max_accessions]
    if args.dry_run or not todo:
        event("genbank_summary", jobs=jobs, candidates=len(candidates), checked=0,
              added=0, negatives=0, missing=0, unanswered=0, deferred=deferred)
        return

    found = written = missing = unanswered = 0
    new_types = {}
    for start in range(0, len(todo), GENBANK_BATCH):
        batch = todo[start:start + GENBANK_BATCH]
        unchecked = []
        documents = _fetch_genbank_xml_batch(batch, unchecked=unchecked)
        returned = set()
        for document in documents:
            parsed = _parse_genbank_xml(document)["by_acc"]
            returned.update(tss.accession_root(record.get("accession")) for record in parsed.values())
            for record in parsed.values():
                entry = tss.genbank_type_entry(record)
                if entry and entry["accession"]:
                    new_types[entry["accession"]] = entry
            found = len(new_types)
            # The parser already recorded the types; this adds the "asked, not a
            # type" answers so the next run does not ask NCBI again.
            written += tss.remember_genbank_records(parsed.values(), include_negatives=True)
        # Only an accession NCBI actually answered for can be recorded as gone.
        # A failed fetch, or a rejected batch whose one-by-one isolation timed
        # out or ran out of budget, leaves ids unasked; those are retried next
        # run rather than parked for MISSING_RETRY_DAYS as "no record".
        missing += tss.remember_missing_accessions(set(batch) - returned - set(unchecked))
        unanswered += len(set(unchecked))
        if unchecked:
            log(f"genbank: {len(unchecked)} of {len(batch)} accessions in the batch starting "
                f"{batch[0]} were not answered; retried next run")
        log(f"genbank: {min(start + GENBANK_BATCH, len(todo))}/{len(todo)} fetched, {found} types so far")
    log(f"genbank: done, {found} type accessions found, {written} negative answers recorded, "
        f"{missing} accessions NCBI returned no record for")
    # Every accession asked about here was unknown to both sources, so each type
    # found is newly marked.
    event("genbank_summary", jobs=jobs, candidates=len(candidates), checked=len(todo),
          added=len(new_types), negatives=written, missing=missing,
          unanswered=unanswered, deferred=deferred)
    for acc in sorted(new_types):
        entry = new_types[acc]
        event("added", source="genbank", accession=acc, status=entry.get("status"),
              organism=entry.get("organism"), voucher=entry.get("voucher"),
              previously_marked="no")


# --------------------------------------------------------------------------
# curation pass
# --------------------------------------------------------------------------

def job_accessions(since_days):
    """Every GenBank accession in job inputs (changed within since_days, 0 = all)."""
    return _scan_job_accessions(since_days, lambda header, acc: True)


# Where the last curation run that stopped short got to, per --since-days.
# Alan 11/8/26 - Negative answers are not cached, so without this every capped
# or time-limited run asked the same sorted prefix again and an all-jobs
# backfill (~212,000 accessions against the 25,000 cap) never got past it.
CURATION_CURSOR_NAME = "curation_cursor.json"


def _curation_cursor_scope(since_days):
    return f"since_days={since_days:g}"


def _read_curation_cursor(scope):
    try:
        data = json.loads((tss.DATA_DIR / CURATION_CURSOR_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = data.get(scope) if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def _write_curation_cursor(scope, accession):
    """Record (or, with accession None, clear) where the next run starts."""
    path = tss.DATA_DIR / CURATION_CURSOR_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    if accession:
        data[scope] = accession
    elif scope in data:
        del data[scope]
    else:
        return
    try:
        write_atomically(path, json.dumps(data, sort_keys=True) + "\n")
    except OSError as exc:
        log(f"curation: could not save where this run stopped ({exc}); "
            "the next run starts from the beginning")


def run_curation(args):
    from app.services.mycomap_org_service import CURATION_BATCH_SIZE, curated_genbank_rows

    candidates, jobs = job_accessions(args.since_days)
    todo = sorted(candidates)
    deferred = 0
    log(f"curation: {jobs} jobs scanned, {len(todo)} accessions to ask MycoMap.org about")
    # Continue after the accession the last short run stopped at, wrapping
    # round, so every candidate is asked in turn and then re-asked on the next
    # lap; a run that asks everything clears the cursor and starts at the top.
    scope = _curation_cursor_scope(args.since_days)
    cursor = _read_curation_cursor(scope)
    if cursor:
        start = bisect_right(todo, cursor)
        todo = todo[start:] + todo[:start]
        log(f"curation: continuing after {cursor}, where the last run stopped")
    capped = bool(args.max_curation_accessions and len(todo) > args.max_curation_accessions)
    if capped:
        log(f"curation: limiting this run to {args.max_curation_accessions}; "
            "the next run continues from where this one stops")
        deferred = len(todo) - args.max_curation_accessions
        todo = todo[:args.max_curation_accessions]
    if args.dry_run or not todo:
        event("curation_summary", jobs=jobs, candidates=len(candidates), checked=0,
              added=0, removed=0, unanswered=0, deferred=deferred)
        return

    before = {acc: entry for acc, entry in tss.curation_index().items() if tss.curation_marks(entry)}
    other_marked = ({acc for acc, entry in tss.genbank_index().items() if entry.get("status")}
                    | set(tss.mycomap_index()))
    # MycoMap.org rate-limits genbank-curation per client, and the live site's
    # name lookups share that allowance, so this pass is paced well below it
    # and waits out a 429's Retry-After rather than writing the batch off.
    budget_ends = time.monotonic() + args.curation_max_minutes * 60
    answered = unanswered = 0
    # How many of todo, from its start, were asked without a gap: the next run
    # starts after the last of them, so an unanswered batch is asked again.
    asked_through = 0
    starts = list(range(0, len(todo), CURATION_BATCH_SIZE))
    for number, start in enumerate(starts):
        batch = todo[start:start + CURATION_BATCH_SIZE]
        while True:
            try:
                # Records every answer into the curation cache as a side effect.
                answered += len(curated_genbank_rows(batch, strict=True))
                if asked_through == start:
                    asked_through = start + len(batch)
                break
            except Exception as exc:
                wait = (getattr(exc, "retry_after", None) or 0) + 5
                if wait > 5 and time.monotonic() + wait < budget_ends:
                    log(f"curation: MycoMap.org asked for a {wait - 5}s wait; waiting")
                    time.sleep(wait)
                    continue
                unanswered += len(batch)
                log(f"curation: batch starting {batch[0]} was not answered ({exc}); retried next run")
                break
        done = min(start + CURATION_BATCH_SIZE, len(todo))
        if done % 2000 < CURATION_BATCH_SIZE or done == len(todo):
            log(f"curation: {done}/{len(todo)} asked")
        if number + 1 < len(starts):
            if time.monotonic() + args.curation_interval >= budget_ends:
                left = len(todo) - done
                unanswered += left
                log(f"curation: --curation-max-minutes reached; {left} accessions left for next run")
                break
            time.sleep(args.curation_interval)
    if not capped and asked_through == len(todo):
        _write_curation_cursor(scope, None)
    elif asked_through:
        _write_curation_cursor(scope, todo[asked_through - 1])
    after = {acc: entry for acc, entry in tss.curation_index().items() if tss.curation_marks(entry)}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    log(f"curation: done, {answered} answered, {len(added)} designations added, "
        f"{len(removed)} cleared, {unanswered} unanswered")
    event("curation_summary", jobs=jobs, candidates=len(candidates), checked=len(todo),
          added=len(added), removed=len(removed), unanswered=unanswered, deferred=deferred)
    for acc in added:
        entry = after[acc]
        event("added", source="mycomap_curation", accession=acc, status=entry.get("status"),
              organism=entry.get("organism"), basis=entry.get("type_basis"),
              previously_marked="yes" if acc in other_marked else "no")
    for acc in removed:
        entry = before[acc]
        event("removed", source="mycomap_curation", accession=acc, status=entry.get("status"),
              organism=entry.get("organism"),
              still_marked="yes" if acc in other_marked else "no")


PASSES = {"mycomap": run_mycomap, "genbank": run_genbank, "curation": run_curation}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--passes", default="mycomap,genbank,curation",
                        help="comma-separated passes to run (default: mycomap,genbank,curation)")
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    parser.add_argument("--force", action="store_true", help="accept a MycoMap snapshot that shrank by >10%%")
    parser.add_argument("--since-days", type=float, default=0,
                        help="genbank and curation passes: only scan jobs whose input changed "
                             "in this many days (0 = all)")
    parser.add_argument("--max-accessions", type=int, default=5000,
                        help="genbank pass: most accessions to fetch from NCBI in one run (0 = no limit)")
    parser.add_argument("--max-curation-accessions", type=int, default=25000,
                        help="curation pass: most accessions to ask MycoMap.org about in one run "
                             "(0 = no limit; all jobs hold ~212,000)")
    parser.add_argument("--curation-interval", type=float, default=30,
                        help="curation pass: seconds between requests (default 30, leaving "
                             "MycoMap.org's per-client allowance for the live site's lookups)")
    parser.add_argument("--curation-max-minutes", type=float, default=150,
                        help="curation pass: stop after this long; the rest are asked next run")
    args = parser.parse_args()

    passes = [name.strip() for name in args.passes.split(",") if name.strip()]
    unknown = set(passes) - set(PASSES)
    if unknown:
        parser.error(f"unknown pass(es): {', '.join(sorted(unknown))}")

    global _refresh_log
    if not args.dry_run:
        try:
            tss.REFRESH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            _refresh_log = open(tss.REFRESH_LOG_PATH, "a", encoding="utf-8")
        except OSError as exc:
            # The refresh itself still matters more than its report.
            print(f"cannot append to {tss.REFRESH_LOG_PATH}: {exc}", file=sys.stderr)

    marked_before = len(marked_accessions())
    event("refresh_started", passes=",".join(passes), since_days=args.since_days,
          dry_run="yes" if args.dry_run else "no")
    failed = False
    for name in passes:
        try:
            PASSES[name](args)
        except Exception as exc:
            failed = True
            log(f"{name}: FAILED: {exc}")
            event("pass_failed", **{"pass": name}, error=exc)
    event("refresh_finished", outcome="failed" if failed else "ok",
          marked_before=marked_before, marked_after=len(marked_accessions()))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
