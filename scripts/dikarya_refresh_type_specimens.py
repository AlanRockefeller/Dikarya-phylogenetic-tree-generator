#!/usr/bin/env python3
"""
Refresh the type-specimen reference data the tree viewer marks tips from.

Two independent passes (see app/services/type_specimen_service.py):

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

Output lives in Config.TYPE_SPECIMEN_DIR (cache/type_specimens, group dikarya,
2775), which the web process reads and the worker appends to.

Every run except --dry-run also appends its log to ~/.dikarya/type-specimens/
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import Config  # noqa: E402
from app.services import type_specimen_service as tss  # noqa: E402
from app.services.api_diagnostics import diagnostic_urlopen  # noqa: E402
from app.services.artifact_storage import default_file_mode  # noqa: E402
from app.services.fasta_utils import is_genbank_accession  # noqa: E402

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
    r"TYPE material|type strain)\b",
    re.IGNORECASE,
)


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
    """Accessions the viewer currently marks as types, from either source."""
    genbank = {acc for acc, entry in tss.genbank_index().items() if entry.get("status")}
    return genbank | set(tss.mycomap_index())


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


def _specificity(record, row_id):
    """Rank two rows for the same accession: a named category beats the generic
    "type", then a row with a description and a voucher. The row id breaks a
    tie so the answer never depends on the API's row order."""
    return (record["status"] != "type", bool(record["type_material"]),
            bool(record["voucher"]), -row_id if isinstance(row_id, int) else 0)


def build_snapshot(rows):
    records = {}
    ranks = {}
    skipped = {"not_an_accession": 0, "not_type_material": 0}
    for row in rows:
        acc = tss.accession_root(row.get("accessionNumber"))
        # Drops the core list's 7-digit lab sequence ids and the two
        # spreadsheet header rows that were imported as data.
        if not acc or not is_genbank_accession(acc):
            skipped["not_an_accession"] += 1
            continue
        type_material = " ".join(str(row.get("typeMaterial") or "").split())
        status = tss.classify_type_material(type_material)
        if status is None:
            if type_material:
                skipped["not_type_material"] += 1  # e.g. "reference material"
                continue
            status = "type"  # listed as a type, but no category given
        record = {
            "status": status,
            "type_material": type_material,
            "organism": " ".join(str(row.get("organism") or "").split()),
            "voucher": " ".join(str(row.get("specimenVoucher") or row.get("isolate") or "").split()),
            "source": row.get("source") or "",
        }
        # The list can carry one accession more than once, and a later bare row
        # must not overwrite "holotype" with the generic "type".
        rank = _specificity(record, row.get("id"))
        if acc not in records or rank > ranks[acc]:
            records[acc] = record
            ranks[acc] = rank
    return records, skipped


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

def candidate_accessions(since_days):
    from app.services.sequence_dedup_service import record_accession

    cutoff = time.time() - since_days * 86400 if since_days else None
    candidates = set()
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
            if acc and (acc.startswith("NR_") or TYPE_WORD_RE.search(header)):
                candidates.add(acc)
    return candidates, jobs


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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--passes", default="mycomap,genbank",
                        help="comma-separated passes to run (default: mycomap,genbank)")
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    parser.add_argument("--force", action="store_true", help="accept a MycoMap snapshot that shrank by >10%%")
    parser.add_argument("--since-days", type=float, default=0,
                        help="genbank pass: only scan jobs whose input changed in this many days (0 = all)")
    parser.add_argument("--max-accessions", type=int, default=5000,
                        help="genbank pass: most accessions to fetch from NCBI in one run (0 = no limit)")
    args = parser.parse_args()

    passes = [name.strip() for name in args.passes.split(",") if name.strip()]
    unknown = set(passes) - {"mycomap", "genbank"}
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
            (run_mycomap if name == "mycomap" else run_genbank)(args)
        except Exception as exc:
            failed = True
            log(f"{name}: FAILED: {exc}")
            event("pass_failed", **{"pass": name}, error=exc)
    event("refresh_finished", outcome="failed" if failed else "ok",
          marked_before=marked_before, marked_after=len(marked_accessions()))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
