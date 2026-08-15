#!/usr/bin/env python3
"""Compact, coverage-aware summary of Dikarya's live and rotated logs."""

import argparse
import collections
import gzip
import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "var" / "logs"
ACCESS_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] '
    r'"(?P<method>[A-Z]+) (?P<path>\S+) (?P<proto>[^"]+)" '
    r'(?P<status>\d{3}) (?P<size>\S+) "(?P<ref>[^"]*)" "(?P<ua>[^"]*)"'
    r'(?: (?P<micros>\d+))?(?: req=(?P<req>\S+))?'
)
UUID_RE = re.compile(r'(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
NUMERIC_SEG_RE = re.compile(r'/\d+(?=/|$|\.)')
TS_RE = re.compile(r'^\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})')
LEVEL_RE = re.compile(r'\[(ERROR|CRITICAL|WARNING)\]')
CONTEXT_RE = re.compile(r'\[(?=[^\]]*\b(?:req|job|rq|user)=)(?P<body>[^\]]+)\]')
EXCEPTION_RE = re.compile(r'^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt))(?::\s*(.*))?$')
STATIC_SUFFIXES = (".css", ".js", ".map", ".ico", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".woff", ".woff2")
SCANNER_MARKERS = (
    "/.env", "/wp-", "/wordpress", "/phpmyadmin", "/xmlrpc", "/cgi-bin", "/.git",
    "/vendor/php", "/actuator", "/.aws", "/.ssh", "/.svn", "/.hg", "/.docker",
    "/.vscode", "/.idea", "/.well-known/security", "/config.json", "/credentials",
    "/id_rsa", "/backup.sql", "/dump.sql", "/database.sql", "/server-status",
    "/solr/", "/jenkins", "/hudson", "/manager/html", "/struts", "/login.action",
    "/telescope", "/debug/default", "/geoserver", "/owa/", "/autodiscover",
    "/boaform", "/hnap1", "/setup.cgi", "/shell", "/eval-stdin",
    # Appliance / webmail credential probes seen daily against this host.
    "/+cscoe+", "/remote/login", "/dana-na", "/global-protect", "/ecp/",
    "/autodiscover", "/onvif", "/device_service", "/mcp",
)
# Exact paths that only a scanner asks for. Kept as exact matches, not
# substrings, so a genuine product 404 such as
# /api/job/<id>/download/fasta/pruned is never swept into the noise bucket.
SCANNER_EXACT_PATHS = frozenset({
    "/login", "/logon", "/signin", "/ip", "/sse", "/graphql", "/api/graphql",
    "/config", "/env", "/settings", "/api/config", "/api/env", "/api/settings",
    "/api/v1/config", "/api/v1/env", "/api/v1/settings", "/server-info",
    "/console", "/status", "/info",
})
# UUID form used for RQ job ids in worker logs.
UUID_PATTERN = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'


def open_maybe_gz(path):
    return gzip.open(path, "rt", errors="replace") if path.suffix == ".gz" else open(path, errors="replace")


def log_files(stem, cutoff=None):
    """Return only the files that can hold records inside the requested window.

    Alan 8/15/26 - This used to return every rotation on disk and rely on the
    per-record timestamp check to throw the rest away. With two weeks of
    compressed access logs that meant decompressing and regex-matching several
    hundred thousand lines to produce a 24-hour report, and it made the coverage
    footer claim files that contributed nothing.

    A rotated file's mtime is the moment it was closed, so it cannot contain
    records newer than that: anything whose mtime is older than the cutoff is
    outside the window. The single newest such file is still read, because the
    cutoff usually falls inside it.
    """
    candidates = sorted(
        (path for path in LOG_DIR.glob(f"{stem}.log*") if path.is_file()),
        key=lambda path: (path.stat().st_mtime, path.name),
    )
    if cutoff is None:
        return candidates
    epoch = cutoff.timestamp()
    overlapping = [path for path in candidates if path.stat().st_mtime >= epoch]
    preceding = [path for path in candidates if path.stat().st_mtime < epoch]
    if preceding:
        overlapping.insert(0, preceding[-1])
    return overlapping


def normalize_path(path):
    path = path.split("?", 1)[0]
    path = UUID_RE.sub("<id>", path)
    path = NUMERIC_SEG_RE.sub("/<id>", path)
    return path


def parse_access_ts(raw):
    try:
        return datetime.strptime(raw.split()[0], "%d/%b/%Y:%H:%M:%S")
    except ValueError:
        return None


def parse_log_ts(line):
    match = TS_RE.search(line)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def percentile(values, fraction):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, max(0, math.ceil(len(values) * fraction) - 1))]


def context_fields(text):
    match = CONTEXT_RE.search(text)
    if not match:
        return {}
    return dict(re.findall(r'(\w+)=([^\s\]]+)', match.group("body")))


def is_noise_4xx(path, ua):
    """Classify a 4xx target as crawler/scanner/static noise.

    ``path`` must already be query-free (see normalize_path): scanners append
    query strings to their probes, and `/index.php?s=/Index/think/...` used to
    fall through to the product bucket purely because it did not *end* in .php.
    """
    lower = path.split("?", 1)[0].lower()
    return (
        lower.endswith(STATIC_SUFFIXES)
        or lower.endswith((".php", ".asp", ".aspx", ".jsp", ".cgi", ".bak", ".sql", ".yml", ".yaml", ".ini"))
        or lower in ("/robots.txt", "/.well-known/traffic-advice")
        or lower.rstrip("/") in SCANNER_EXACT_PATHS
        or lower.startswith("/thumb/")
        or any(marker in lower for marker in SCANNER_MARKERS)
        or ("bot" in ua.lower() and not lower.startswith("/api/"))
    )


def coverage_record(files, oldest, newest, lines, unparsed, timed=0, contextual=0, observed=0):
    """Coverage numbers. ``lines`` is what was scanned; ``observed`` is what was in window."""
    return {
        "files": [p.name for p in files], "oldest": oldest, "newest": newest,
        "lines": lines, "unparsed": unparsed, "timed": timed,
        "contextual": contextual, "observed": observed,
    }


def analyze_access(cutoff):
    files = log_files("access", cutoff)
    statuses = collections.Counter()
    server_errors = collections.Counter()
    product_4xx = collections.Counter()
    noise_4xx = collections.Counter()
    rate_limited = collections.Counter()
    durations = collections.defaultdict(list)
    streams = collections.defaultdict(list)
    clients = collections.Counter()
    agents = collections.Counter()
    oldest = newest = None
    lines = unparsed = timed = contextual = total = 0
    for path in files:
        with open_maybe_gz(path) as handle:
            for line in handle:
                lines += 1
                match = ACCESS_RE.match(line)
                if not match:
                    unparsed += 1
                    continue
                when = parse_access_ts(match.group("ts"))
                if when is None:
                    unparsed += 1
                    continue
                if when < cutoff:
                    continue
                oldest = when if oldest is None or when < oldest else oldest
                newest = when if newest is None or when > newest else newest
                total += 1
                status = int(match.group("status"))
                normalized = normalize_path(match.group("path"))
                endpoint = f"{match.group('method')} {normalized}"
                statuses[f"{status // 100}xx"] += 1
                clients[match.group("ip")] += 1
                agents[match.group("ua")[:80]] += 1
                if status >= 500:
                    server_errors[(status, endpoint)] += 1
                elif status >= 400:
                    target = noise_4xx if is_noise_4xx(normalized, match.group("ua")) else product_4xx
                    target[(status, endpoint)] += 1
                if status == 429:
                    rate_limited[match.group("ip")] += 1
                micros = match.group("micros")
                if micros:
                    timed += 1
                    seconds = int(micros) / 1_000_000
                    (streams if normalized.endswith("/events") else durations)[endpoint].append(seconds)
                if match.group("req") not in (None, "-", ""):
                    contextual += 1
    return {
        "total": total, "statuses": statuses, "server_errors": server_errors,
        "product_4xx": product_4xx, "noise_4xx": noise_4xx,
        "rate_limited": rate_limited, "durations": durations, "streams": streams,
        "clients": clients, "agents": agents,
        "coverage": coverage_record(files, oldest, newest, lines, unparsed, timed, contextual, total),
    }


def iter_log_records(path):
    """Yield timestamp-led records with their multiline traceback attached."""
    current = None
    with open_maybe_gz(path) as handle:
        for line in handle:
            if TS_RE.match(line):
                if current:
                    yield "".join(current)
                current = [line]
            elif current is not None:
                current.append(line)
        if current:
            yield "".join(current)


def meaningful_error_key(record):
    lines = [line.strip() for line in record.splitlines() if line.strip()]
    for line in reversed(lines[1:]):
        match = EXCEPTION_RE.match(line)
        if match:
            detail = re.sub(r'\b\d+\b', '<n>', UUID_RE.sub('<id>', match.group(2) or ''))
            return f"{match.group(1).split('.')[-1]}: {detail}"[:180].rstrip(": ")
    first = lines[0] if lines else record
    message = LEVEL_RE.split(first, 1)[-1]
    message = CONTEXT_RE.sub("", message)
    message = re.sub(r'^\s*\[[^\]]+\]\s*', '', message)
    message = UUID_RE.sub("<id>", message)
    message = re.sub(r'\b\d+\b', '<n>', message)
    return message.strip()[:180]


def analyze_errors(cutoff):
    files = log_files("error", cutoff) + log_files("errors", cutoff)
    exceptions = collections.Counter()
    degradations = collections.Counter()
    affected = collections.defaultdict(set)
    seen = set()
    oldest = newest = None
    lines = unparsed = contextual = records = 0
    for path in files:
        for record in iter_log_records(path):
            records += 1
            lines += record.count("\n") or 1
            when = parse_log_ts(record)
            if when is None:
                unparsed += 1
                continue
            if when < cutoff or not LEVEL_RE.search(record.splitlines()[0]):
                continue
            fields = context_fields(record.splitlines()[0])
            oldest = when if oldest is None or when < oldest else oldest
            newest = when if newest is None or when > newest else newest
            key = meaningful_error_key(record)
            # Mirrored files and repeated records inside one request are one incident.
            incident = (fields.get("req"), key) if fields.get("req") not in (None, "-") else (when, key)
            if incident in seen:
                continue
            seen.add(incident)
            if fields:
                contextual += 1
            user = fields.get("user")
            if "DEGRADED" in record:
                event = re.search(r'event=degraded\.([\w.-]+)', record)
                slug = event.group(1) if event else record.split("DEGRADED", 1)[-1].strip().split(":", 1)[0]
                degradations[slug[:100]] += 1
                if user:
                    affected[slug[:100]].add(user)
            else:
                exceptions[key] += 1
                if user:
                    affected[key].add(user)
    return {
        "exceptions": exceptions, "degradations": degradations, "affected": affected,
        "coverage": coverage_record(files, oldest, newest, lines, unparsed, 0, contextual, len(seen)),
    }


# Alan 8/15/26 - Worker lifecycle patterns, in the shapes RQ 2.x actually emits.
#
# The previous version recognised only Dikarya's own event=job.* records plus a
# guess at RQ's start line, and nothing at all for RQ's completions. Every
# ordinary job therefore looked like a "start without terminal event", which made
# the whole section noise and hid the handful of genuinely stranded jobs it exists
# to surface.
STABLE_EVENT_RE = re.compile(r'event=job\.(started|completed|failed)\b')
# "phylo_high: <description> (<uuid>)" -- RQ's start line.
# The trailing "[release=... job=...]" group is ContextFormatter's suffix, which
# is appended to RQ's own records once they go through Dikarya's root handler.
RQ_START_RE = re.compile(
    rf'(?P<queue>[\w.-]+):\s+(?P<desc>.+?)\s+\((?P<id>{UUID_PATTERN})\)\s*(?:\[[^\]]*\])?\s*$'
)
RQ_TERMINAL_RES = {
    "completed": (
        re.compile(rf':\s+Job OK\s+\((?P<id>{UUID_PATTERN})\)'),
        re.compile(rf'Successfully completed (?:job )?(?P<id>{UUID_PATTERN})'),
    ),
    "failed": (
        re.compile(rf'moving job (?P<id>{UUID_PATTERN}) to FailedJobRegistry'),
        re.compile(rf'job (?P<id>{UUID_PATTERN}) stopped by user'),
        re.compile(rf'Work horse killed for job (?P<id>{UUID_PATTERN})'),
        re.compile(rf'job (?P<id>{UUID_PATTERN}) has exceeded maximum retry attempts'),
    ),
}
# A retry or a repeat is not an outcome: the job runs again and reports later.
RQ_RETRY_RES = (
    re.compile(rf'handling retry of job (?P<id>{UUID_PATTERN})'),
    re.compile(rf'job (?P<id>{UUID_PATTERN}) scheduled (?:to repeat|for retry)'),
    re.compile(rf'scheduled for retry\D{{0,40}}(?P<id>{UUID_PATTERN})'),
)


def _first_match(patterns, line):
    for pattern in patterns:
        match = pattern.search(line)
        if match:
            return match.group("id")
    return None


def analyze_worker(cutoff, grace=timedelta(minutes=60)):
    """Summarize worker job lifecycle from worker.log alone (no Redis, no DB)."""
    files = log_files("worker", cutoff)
    counts = collections.Counter()
    started = {}
    terminal = set()
    oldest = newest = None
    lines = unparsed = contextual = window_lines = 0
    for path in files:
        with open_maybe_gz(path) as handle:
            for line in handle:
                lines += 1
                when = parse_log_ts(line)
                if when is None:
                    unparsed += 1
                    continue
                if when < cutoff:
                    continue
                window_lines += 1
                oldest = when if oldest is None or when < oldest else oldest
                newest = when if newest is None or when > newest else newest
                fields = context_fields(line)
                if fields:
                    contextual += 1
                if "DEGRADED" in line:
                    counts["degraded"] += 1

                # 1. Dikarya's own stable events win: they carry the application
                #    job id, which is what an operator can act on.
                event_match = STABLE_EVENT_RE.search(line)
                if event_match and fields.get("job"):
                    state = event_match.group(1)
                    counts[state] += 1
                    if state == "started":
                        started.setdefault(fields["job"], when)
                    else:
                        terminal.add(fields["job"])
                    continue

                # 2. RQ terminal lines. Checked before starts because "Job OK
                #    (uuid)" also matches the start line's shape.
                matched = False
                for state, patterns in RQ_TERMINAL_RES.items():
                    job_id = _first_match(patterns, line)
                    if job_id:
                        counts[state] += 1
                        terminal.add(job_id)
                        matched = True
                        break
                if matched:
                    continue

                job_id = _first_match(RQ_RETRY_RES, line)
                if job_id:
                    counts["retried"] += 1
                    continue

                start_match = RQ_START_RE.search(line)
                if start_match and start_match.group("desc").strip() != "Job OK":
                    counts["started"] += 1
                    started.setdefault(start_match.group("id"), when)

    # "Unterminated" only means something once a job has had time to finish.
    # Anything younger than the grace period is simply still running.
    reference = newest or datetime.now()
    unmatched = [(job_id, when) for job_id, when in started.items() if job_id not in terminal]
    active = [item for item in unmatched if reference - item[1] < grace]
    stale = sorted((item for item in unmatched if reference - item[1] >= grace), key=lambda item: item[1])
    counts["active"] = len(active)
    return counts, stale, coverage_record(files, oldest, newest, lines, unparsed, 0, contextual, window_lines)


def format_age(delta):
    minutes = max(0, int(delta.total_seconds() // 60))
    return f"{minutes // 60}h{minutes % 60:02d}m"


def section(title):
    print(f"\n{title}\n{'-' * len(title)}")


def rows(items, empty="  (none)"):
    if not items:
        print(empty)
    else:
        for item in items:
            print(f"  {item}")


def format_coverage(label, data):
    oldest = data["oldest"].strftime("%Y-%m-%d %H:%M:%S") if data["oldest"] else "-"
    newest = data["newest"].strftime("%Y-%m-%d %H:%M:%S") if data["newest"] else "-"
    # Percentages are over records inside the window, not over every line the
    # files happened to contain, so they do not drift with rotation size.
    timing = (100 * data["timed"] / max(1, data["observed"])) if data["timed"] else 0
    context = 100 * data["contextual"] / max(1, data["observed"])
    return (f"{label}: files={','.join(data['files']) or '-'} range={oldest}..{newest} "
            f"scanned={data['lines']} in-window={data['observed']} unparsed={data['unparsed']} "
            f"timing={timing:.1f}% context={context:.1f}%")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--since-rotated", action="store_true", help="deprecated; rotations are always included")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--slow-threshold", type=float, default=5.0)
    parser.add_argument(
        "--unterminated-grace-minutes", type=float, default=60,
        help="how long a started job may run before it is listed as having no terminal event",
    )
    args = parser.parse_args()
    if not LOG_DIR.is_dir():
        print(f"No log directory at {LOG_DIR}", file=sys.stderr)
        return 1
    cutoff = datetime.now() - timedelta(hours=args.hours)
    access = analyze_access(cutoff)
    errors = analyze_errors(cutoff)
    worker_counts, unterminated, worker_coverage = analyze_worker(
        cutoff, grace=timedelta(minutes=args.unterminated_grace_minutes)
    )
    print(f"Dikarya log digest -- {args.hours:g}h window (since {cutoff:%Y-%m-%d %H:%M})")

    section("Traffic")
    print(f"  {access['total']} requests   " + "  ".join(f"{k}={v}" for k, v in sorted(access['statuses'].items())))
    section("Server errors (5xx) — always shown")
    rows([f"{count:>5}  {status}  {endpoint}" for (status, endpoint), count in access["server_errors"].most_common(args.top)])
    section("Product-relevant client errors (4xx)")
    rows([f"{count:>5}  {status}  {endpoint}" for (status, endpoint), count in access["product_4xx"].most_common(args.top)])
    section("Crawler/scanner/static noise (4xx)")
    rows([f"{count:>5}  {status}  {endpoint}" for (status, endpoint), count in access["noise_4xx"].most_common(args.top)])
    section("Exceptions and errors")
    rows([f"{count:>5}  {key}" + (f" [users: {', '.join(sorted(errors['affected'][key])[:3])}]" if errors['affected'].get(key) else "") for key, count in errors["exceptions"].most_common(args.top)])
    section("Degraded work")
    rows([f"{count:>5}  {key}" for key, count in errors["degradations"].most_common(args.top)])
    section("Worker lifecycle")
    print("  " + "  ".join(
        f"{key}={worker_counts.get(key, 0)}"
        for key in ("started", "completed", "failed", "retried", "active", "degraded")
    ))
    reference = worker_coverage["newest"] or datetime.now()
    rows(
        [f"no terminal event observed: {job} (started {when:%Y-%m-%d %H:%M}, age {format_age(reference - when)})"
         for job, when in unterminated[:args.top]],
        empty=f"  every started job reached a terminal event or is still within the "
              f"{args.unterminated_grace_minutes:g}-minute grace period",
    )
    section(f"Slow requests (> {args.slow_threshold:g}s; streams excluded)")
    slow_rows = []
    for endpoint, values in access["durations"].items():
        slow_values = [value for value in values if value > args.slow_threshold]
        count = len(slow_values)
        if count:
            slow_rows.append((count, f"{count:>5}  {endpoint}  slow-p50={percentile(slow_values, .5):.2f}s slow-p95={percentile(slow_values, .95):.2f}s max={max(slow_values):.2f}s"))
    rows([text for _, text in sorted(slow_rows, reverse=True)[:args.top]], empty=f"  (nothing slower than {args.slow_threshold:g}s)")
    section("SSE stream lifetimes")
    rows([f"{len(values):>5}  {endpoint}  p50={percentile(values, .5):.1f}s p95={percentile(values, .95):.1f}s max={max(values):.1f}s" for endpoint, values in sorted(access["streams"].items())])
    section("Rate-limited clients (429)")
    rows([f"{count:>5}  {ip}" for ip, count in access["rate_limited"].most_common(args.top)])
    section("Heaviest clients / user agents")
    rows([f"{count:>5}  {ip}" for ip, count in access["clients"].most_common(5)])
    rows([f"{count:>5}  {ua}" for ua, count in access["agents"].most_common(5)])
    section("Data quality and coverage")
    print("  " + format_coverage("access", access["coverage"]))
    print("  " + format_coverage("errors", errors["coverage"]))
    print("  " + format_coverage("worker", worker_coverage))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
