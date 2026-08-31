#!/usr/bin/env python3
"""Compact, coverage-aware summary of Dikarya's live and rotated logs."""

import argparse
import collections
import gzip
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "var" / "logs"
DEFAULT_CHECKPOINT = REPO_ROOT / ".log-review-checkpoint.json"
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
# The WARNING+ mirror's timestamp has milliseconds and is followed by a logger
# name. Gunicorn's copy instead has a numeric UTC offset and no logger field.
# Distinguish those formatter layouts before removing metadata: an application
# message may itself legitimately begin with a bracketed tag such as [ALIGN].
MIRROR_HEAD_RE = re.compile(
    r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}\]\s+'
    r'\[\d+\]\s+\[(?:ERROR|CRITICAL|WARNING)\]\s+'
    r'\[[^\]]+\]\s*(?P<message>.*)$'
)
EXCEPTION_RE = re.compile(r'^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt))(?::\s*(.*))?$')
STATIC_SUFFIXES = (".css", ".js", ".map", ".ico", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".woff", ".woff2")
SCANNER_MARKERS = (
    "/.env", "/wp-", "/wordpress", "/phpmyadmin", "/xmlrpc", "/cgi-bin", "/.git",
    "/vendor/php", "/actuator", "/.aws", "/.ssh", "/.svn", "/.hg", "/.docker",
    "/.vscode", "/.idea", "/.well-known/security", "/config.json", "/credentials",
    "/id_rsa", "/backup.sql", "/dump.sql", "/database.sql", "/server-status",
    "/solr/", "/jenkins", "/hudson", "/manager/html", "/struts", "/login.action",
    "/telescope", "/debug/default", "/geoserver", "/owa/", "/autodiscover",
    "/boaform", "/hnap1", "/setup.cgi", "/shell", "/eval-stdin", "/wp/",
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
    # Auth/console routes this app has never had. One scanner probed each of
    # these 88 times in a day, in the same sweep as the /login and /signin
    # probes above, but they were landing in the product bucket and crowding
    # out the real 4xx entries. Dikarya's own auth lives at /auth/login.
    "/signup", "/register", "/dashboard", "/admin", "/account",
    "/auth/callback", "/api/auth/signin", "/login.html", "/sftp-config.json",
    # Generic fetch/proxy/config endpoints from a burst scanner that rotated
    # dozens of fake crawler user agents. Dikarya has never exposed these exact
    # routes; real downloads and previews live under scoped resource paths.
    "/fetch", "/proxy", "/api/proxy", "/api/v1/fetch", "/api/download",
    "/api/image", "/api/preview", "/api/v2/settings", "/api/v2/config",
})
# Scanner probes hide the extension behind a version digit -- /randkeyword.PhP7,
# /zup.php73, /baxa1.phP8 all arrived in one sweep and were filed as
# product-relevant 404s because a plain endswith(".php") does not match them.
# Case is already folded by the caller; the trailing digits are the whole point.
SCRIPT_EXT_RE = re.compile(r'\.(?:php|asp|aspx|jsp|cgi|pl|cfm)[0-9]*$')
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
    """Parse Gunicorn's %(t)s and normalize it to naive UTC.

    Everything else in this digest is naive UTC: the window boundaries come from
    parse_window_timestamp(), which converts to UTC and drops the tzinfo, and the
    application log timestamps are written in UTC. Gunicorn's timestamp carries
    an explicit offset, and it used to be discarded -- correct only for as long
    as the host stays on UTC. On any other host, or across a DST transition, that
    silently slides the whole 24-hour window by the offset. Apply the offset and
    convert; do not simply strip it.
    """
    parts = raw.strip().strip("[]").split()
    if not parts:
        return None
    try:
        when = datetime.strptime(parts[0], "%d/%b/%Y:%H:%M:%S")
    except ValueError:
        return None
    if len(parts) < 2:
        # No offset in the record. It is already whatever the host writes, which
        # is the same assumption parse_log_ts makes about the app logs.
        return when
    try:
        aware = datetime.strptime(f"{parts[0]} {parts[1]}", "%d/%b/%Y:%H:%M:%S %z")
    except ValueError:
        return when
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def parse_log_ts(line):
    match = TS_RE.search(line)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def parse_window_timestamp(raw):
    """Parse an ISO-8601 operator boundary and normalize it to naive UTC."""
    value = str(raw or "").strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid timestamp {raw!r}; use ISO-8601, e.g. 2026-08-22T18:03:00Z"
        ) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def format_window_timestamp(value):
    """Render the naive UTC timestamps used by the log parsers."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat(timespec="seconds") + "Z"


def read_review_checkpoint(path):
    """Return the completed review boundary stored in ``path``."""
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ValueError(
            f"No log-review checkpoint exists at {path}. Seed it with "
            f"--mark-reviewed <ISO timestamp> after completing an initial review."
        ) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"Could not read log-review checkpoint {path}: {exc}") from exc
    if not isinstance(payload, dict) or not payload.get("reviewed_through"):
        raise ValueError(
            f"Log-review checkpoint {path} has no reviewed_through timestamp."
        )
    return parse_window_timestamp(payload["reviewed_through"])


def write_review_checkpoint(path, reviewed_through):
    """Atomically record a successfully reviewed UTC boundary."""
    reviewed_through = reviewed_through.replace(microsecond=0)
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    if reviewed_through > now:
        raise ValueError(
            "Refusing to mark future logs as reviewed "
            f"({format_window_timestamp(reviewed_through)} > "
            f"{format_window_timestamp(now)})."
        )
    if path.exists():
        # Reads stay strict everywhere else -- a review must never silently
        # start from a boundary nobody can vouch for. Here the operator is
        # explicitly reseeding the file, and refusing to overwrite a corrupt
        # checkpoint made --mark-reviewed, the documented recovery, unable to
        # recover it. An unreadable file therefore means "no valid previous
        # boundary", which cannot be moved backwards.
        try:
            previous = read_review_checkpoint(path)
        except ValueError as exc:
            print(
                f"warning: replacing an unreadable log-review checkpoint at "
                f"{path} ({exc})",
                file=sys.stderr,
            )
            previous = None
        if previous is not None and reviewed_through < previous:
            raise ValueError(
                "Refusing to move the log-review checkpoint backwards "
                f"({format_window_timestamp(previous)} -> "
                f"{format_window_timestamp(reviewed_through)})."
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "reviewed_through": format_window_timestamp(reviewed_through),
        "updated_at": format_window_timestamp(datetime.now(timezone.utc)),
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


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


def is_noise_4xx(path, ua, status=0, method=""):
    """Classify a 4xx target as crawler/scanner/static noise.

    ``path`` must already be query-free (see normalize_path): scanners append
    query strings to their probes, and `/index.php?s=/Index/think/...` used to
    fall through to the product bucket purely because it did not *end* in .php.
    """
    lower = path.split("?", 1)[0].lower()
    # A 405 means the route exists but not for that verb, which the UI never
    # does -- it only calls endpoints it knows. In practice these are scanners
    # POSTing to document routes: two IPs sent 312 of the 317 "POST /" 405s in
    # one day, and they were filed as product-relevant errors. A 405 under
    # /api/ is still worth seeing, since that would be a genuine route/frontend
    # mismatch rather than a probe.
    if status == 405 and not lower.startswith("/api/"):
        return True
    return (
        lower.endswith(STATIC_SUFFIXES)
        or lower.endswith((".bak", ".sql", ".yml", ".yaml", ".ini"))
        or SCRIPT_EXT_RE.search(lower) is not None
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


def analyze_access(cutoff, until=None):
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
                if when < cutoff or (until is not None and when >= until):
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
                    target = noise_4xx if is_noise_4xx(
                        normalized, match.group("ua"), status, match.group("method")
                    ) else product_4xx
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


def formatted_log_message(first_line):
    """Return message data after stripping only known formatter metadata."""
    mirror = MIRROR_HEAD_RE.match(first_line)
    if mirror:
        message = mirror.group("message")
    else:
        message = LEVEL_RE.split(first_line, 1)[-1]
    return CONTEXT_RE.sub("", message).strip()


def meaningful_error_key(record):
    lines = [line.strip() for line in record.splitlines() if line.strip()]
    for line in reversed(lines[1:]):
        match = EXCEPTION_RE.match(line)
        if match:
            detail = re.sub(r'\b\d+\b', '<n>', UUID_RE.sub('<id>', match.group(2) or ''))
            return f"{match.group(1).split('.')[-1]}: {detail}"[:180].rstrip(": ")
    first = lines[0] if lines else record
    message = formatted_log_message(first)
    message = UUID_RE.sub("<id>", message)
    message = re.sub(r'\b\d+\b', '<n>', message)
    return message.strip()[:180]


def record_identity(record):
    """A mirror-invariant identity for one log record.

    error.log and errors.log carry the same WARNING+ records, but not the same
    bytes. error.log is written by the handler Gunicorn installed, wrapped by
    ContextFormatter -- "[2026-08-24 08:01:02 +0000] [pid] [ERROR] msg [req=..]".
    errors.log is written by install_error_mirror()'s own handler, whose format
    adds the logger name and milliseconds -- "[2026-08-24 08:01:02,123] [pid]
    [ERROR] [app.services.queue] msg [req=..]". Keying on the raw text therefore
    counted one incident twice.

    Everything below is derived only from the parts both formats carry: the
    message after the level, with the logger-name bracket and the trailing
    context suffix removed, plus the traceback lines (identical in both, because
    ContextFormatter appends its suffix to the first line only) and the context
    fields themselves. Note that identifiers and numbers are deliberately NOT
    normalized away here -- unlike meaningful_error_key(), which folds them so
    related failures group together, this has to keep two genuinely different
    same-second messages apart.
    """
    lines = record.splitlines()
    first = lines[0] if lines else record
    message = formatted_log_message(first)
    body = tuple(line.rstrip() for line in lines[1:])
    context = tuple(sorted(context_fields(first).items()))
    return (message.strip(), body, context)


def analyze_errors(cutoff, until=None):
    # Grouped by stem rather than flattened into one list, because the
    # occurrence counter below has to reset between the two mirrored streams
    # while still counting across each stream's own rotations.
    #
    # errors.log is a level-filtered mirror of error.log (app/__init__.py
    # attaches the WARNING+ handler to the root logger), so a WARNING or worse
    # record is written to both, in the same order. It is NOT written at the
    # same byte offset -- error.log carries the INFO traffic in between -- so
    # position in the file cannot identify a record across the mirror. What does
    # survive the mirror is ordinal position *among matching records*: the third
    # "Redis unavailable" in this second is the third in either file.
    streams = [log_files("error", cutoff), log_files("errors", cutoff)]
    files = [path for stream in streams for path in stream]
    exceptions = collections.Counter()
    degradations = collections.Counter()
    affected = collections.defaultdict(set)
    affected_jobs = collections.defaultdict(set)
    seen = set()
    oldest = newest = None
    lines = unparsed = contextual = records = 0
    for stream in streams:
        # How many times this exact record has already been seen in this stream.
        # Reset per stream so the mirror's copies line up with the originals,
        # and shared across the stream's rotations so a record either side of a
        # rotation boundary is not mistaken for the same occurrence.
        occurrences = collections.Counter()
        for path in stream:
            for record in iter_log_records(path):
                records += 1
                lines += record.count("\n") or 1
                when = parse_log_ts(record)
                if when is None:
                    unparsed += 1
                    continue
                if (
                    when < cutoff
                    or (until is not None and when >= until)
                    or not LEVEL_RE.search(record.splitlines()[0])
                ):
                    continue
                fields = context_fields(record.splitlines()[0])
                oldest = when if oldest is None or when < oldest else oldest
                newest = when if newest is None or when > newest else newest
                key = meaningful_error_key(record)
                request = fields.get("req")
                if request not in (None, "-"):
                    # A request id already identifies the occurrence: repeats
                    # inside one request are one incident, and the mirror
                    # carries the same id.
                    incident = (request, key)
                else:
                    # No request id, so the record has to identify itself.
                    # Timestamp plus normalized key is not enough (one-second
                    # resolution, and the key rewrites every integer to <n>), and
                    # the raw text is worse than useless: it is byte-identical
                    # for three separate failures in the same second, and NOT
                    # identical for the two copies of one failure, because the
                    # two streams are formatted by different handlers. Identify
                    # the record by its normalized semantic content -- which is
                    # the same on both sides of the mirror -- and separate
                    # repeats by their occurrence ordinal within the stream, so
                    # the mirror's Nth copy lands on the original's Nth.
                    signature = (when, key, record_identity(record))
                    incident = signature + (occurrences[signature],)
                    occurrences[signature] += 1
                if incident in seen:
                    continue
                seen.add(incident)
                if fields:
                    contextual += 1
                user = fields.get("user")
                job = fields.get("job")
                if "DEGRADED" in record:
                    event = re.search(r'event=degraded\.([\w.-]+)', record)
                    slug = event.group(1) if event else record.split("DEGRADED", 1)[-1].strip().split(":", 1)[0]
                    degradations[slug[:100]] += 1
                    if user:
                        affected[slug[:100]].add(user)
                    if job:
                        affected_jobs[slug[:100]].add(job)
                else:
                    exceptions[key] += 1
                    if user:
                        affected[key].add(user)
                    if job:
                        affected_jobs[key].add(job)
    return {
        "exceptions": exceptions, "degradations": degradations, "affected": affected,
        "affected_jobs": affected_jobs,
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


# Lines that belong to the record above them rather than starting a new one.
_CONTINUATION_PREFIXES = (
    " ", "\t", "Traceback (most recent call last)", "During handling of",
    "The above exception", "  File \"",
)


def _is_record_continuation(line):
    """True when a timestamp-less line continues the preceding log record."""
    if not line.strip():
        return True
    if line.startswith((" ", "\t")):
        return True
    if line.startswith(_CONTINUATION_PREFIXES):
        return True
    # "SomeError: detail" -- the final line of a traceback.
    return bool(EXCEPTION_RE.match(line.strip()))


def analyze_worker(cutoff, grace=timedelta(minutes=60), until=None):
    """Summarize worker job lifecycle from worker.log alone (no Redis, no DB)."""
    files = log_files("worker", cutoff)
    counts = collections.Counter()
    started = {}
    terminal = set()
    last_start = {}
    retry_markers = collections.Counter()
    oldest = newest = None
    lines = unparsed = contextual = window_lines = 0
    for path in files:
        with open_maybe_gz(path) as handle:
            for line in handle:
                lines += 1
                when = parse_log_ts(line)
                if when is None:
                    # Traceback bodies, RQ banners and the worker's own startup
                    # echo are continuations of the record above them, not
                    # unreadable lines. Counting them as "unparsed" reported
                    # 124 failures on a file with none, which made the coverage
                    # footer look broken every time a job raised.
                    if not _is_record_continuation(line):
                        unparsed += 1
                    continue
                if when < cutoff or (until is not None and when >= until):
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
                    if state == "started":
                        job_id = fields["job"]
                        if job_id in terminal:
                            # Application job UUIDs are deliberately reused as
                            # RQ IDs for later recomputes. A start after a
                            # terminal event is a new lifecycle.
                            terminal.remove(job_id)
                            started.pop(job_id, None)
                            last_start.pop(job_id, None)
                            retry_markers.pop(job_id, None)
                        previous = last_start.get(job_id)
                        if job_id not in started:
                            counts["started"] += 1
                            started[job_id] = when
                        elif retry_markers[job_id]:
                            # The RQ retry record already counted this attempt.
                            retry_markers[job_id] -= 1
                        elif not (
                            previous
                            and previous[1] == "rq"
                            and abs((when - previous[0]).total_seconds()) <= 30
                        ):
                            # RQ does not consistently emit its retry wording,
                            # but each resumed task emits another stable start.
                            counts["retried"] += 1
                        last_start[job_id] = (when, "stable")
                    else:
                        job_id = fields["job"]
                        if job_id not in terminal:
                            counts[state] += 1
                            terminal.add(job_id)
                    continue

                # 2. RQ terminal lines. Checked before starts because "Job OK
                #    (uuid)" also matches the start line's shape.
                matched = False
                for state, patterns in RQ_TERMINAL_RES.items():
                    job_id = _first_match(patterns, line)
                    if job_id:
                        if job_id not in terminal:
                            counts[state] += 1
                            terminal.add(job_id)
                        matched = True
                        break
                if matched:
                    continue

                job_id = _first_match(RQ_RETRY_RES, line)
                if job_id:
                    counts["retried"] += 1
                    retry_markers[job_id] += 1
                    continue

                start_match = RQ_START_RE.search(line)
                if start_match and start_match.group("desc").strip() != "Job OK":
                    job_id = start_match.group("id")
                    if job_id in terminal:
                        terminal.remove(job_id)
                        started.pop(job_id, None)
                        last_start.pop(job_id, None)
                        retry_markers.pop(job_id, None)
                    previous = last_start.get(job_id)
                    if job_id not in started:
                        counts["started"] += 1
                        started[job_id] = when
                    elif retry_markers[job_id]:
                        retry_markers[job_id] -= 1
                    elif not (
                        previous
                        and previous[1] == "stable"
                        and abs((when - previous[0]).total_seconds()) <= 30
                    ):
                        counts["retried"] += 1
                    last_start[job_id] = (when, "rq")

    # "Unterminated" only means something once a job has had time to finish.
    # Anything younger than the grace period is simply still running.
    # `until` is always set by main(), so this falls back only when a caller
    # uses analyze_worker() directly. Age against the present in naive UTC, not
    # against `newest`: a worker that died stops advancing the log, which would
    # pin the reference near its last line and report its stranded job "active"
    # forever -- the exact case this section exists to surface.
    reference = until or datetime.now(timezone.utc).replace(tzinfo=None)
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
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--hours", type=float)
    window.add_argument("--since", help="ISO-8601 UTC start boundary")
    window.add_argument(
        "--since-checkpoint", action="store_true",
        help="start at the last successfully reviewed checkpoint",
    )
    parser.add_argument(
        "--until", help="ISO-8601 UTC end boundary (default: current UTC second)",
    )
    parser.add_argument(
        "--checkpoint-file", type=Path, default=DEFAULT_CHECKPOINT,
        help=f"review checkpoint path (default: {DEFAULT_CHECKPOINT})",
    )
    parser.add_argument(
        "--mark-reviewed", metavar="TIMESTAMP",
        help="record a completed review boundary and exit; use a digest's checkpoint_candidate",
    )
    parser.add_argument("--since-rotated", action="store_true", help="deprecated; rotations are always included")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--slow-threshold", type=float, default=5.0)
    parser.add_argument(
        "--unterminated-grace-minutes", type=float, default=60,
        help="how long a started job may run before it is listed as having no terminal event",
    )
    args = parser.parse_args()
    if args.mark_reviewed:
        try:
            reviewed_through = parse_window_timestamp(args.mark_reviewed)
            write_review_checkpoint(args.checkpoint_file, reviewed_through)
        except ValueError as exc:
            parser.error(str(exc))
        print(
            f"Log-review checkpoint advanced through "
            f"{format_window_timestamp(reviewed_through)} at {args.checkpoint_file}"
        )
        return 0
    if not LOG_DIR.is_dir():
        print(f"No log directory at {LOG_DIR}", file=sys.stderr)
        return 1
    try:
        until = (
            parse_window_timestamp(args.until)
            if args.until else
            datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        )
        if args.since_checkpoint:
            cutoff = read_review_checkpoint(args.checkpoint_file)
            window_label = "checkpointed review window"
        elif args.since:
            cutoff = parse_window_timestamp(args.since)
            window_label = "explicit review window"
        else:
            hours = 24 if args.hours is None else args.hours
            if hours <= 0:
                parser.error("--hours must be greater than zero")
            cutoff = until - timedelta(hours=hours)
            window_label = f"{hours:g}h window"
    except ValueError as exc:
        parser.error(str(exc))
    if cutoff >= until:
        parser.error("The review start must be earlier than --until")

    access = analyze_access(cutoff, until=until)
    errors = analyze_errors(cutoff, until=until)
    worker_counts, unterminated, worker_coverage = analyze_worker(
        cutoff, grace=timedelta(minutes=args.unterminated_grace_minutes), until=until
    )
    print(
        f"Dikarya log digest -- {window_label} "
        f"[{format_window_timestamp(cutoff)}, {format_window_timestamp(until)})"
    )
    print(f"checkpoint_candidate={format_window_timestamp(until)}")

    section("Traffic")
    print(f"  {access['total']} requests   " + "  ".join(f"{k}={v}" for k, v in sorted(access['statuses'].items())))
    section("Server errors (5xx) — always shown")
    rows([f"{count:>5}  {status}  {endpoint}" for (status, endpoint), count in access["server_errors"].most_common(args.top)])
    section("Product-relevant client errors (4xx)")
    rows([f"{count:>5}  {status}  {endpoint}" for (status, endpoint), count in access["product_4xx"].most_common(args.top)])
    section("Crawler/scanner/static noise (4xx)")
    rows([f"{count:>5}  {status}  {endpoint}" for (status, endpoint), count in access["noise_4xx"].most_common(args.top)])
    section("Exceptions and errors")
    rows([
        f"{count:>5}  {key}"
        + (
            f" [jobs: {len(errors['affected_jobs'][key])}]"
            if errors["affected_jobs"].get(key) else ""
        )
        + (
            f" [users: {', '.join(sorted(errors['affected'][key])[:3])}]"
            if errors['affected'].get(key) else ""
        )
        for key, count in errors["exceptions"].most_common(args.top)
    ])
    section("Degraded work")
    rows([f"{count:>5}  {key}" for key, count in errors["degradations"].most_common(args.top)])
    section("Worker lifecycle")
    print("  " + "  ".join(
        f"{key}={worker_counts.get(key, 0)}"
        for key in ("started", "completed", "failed", "retried", "active", "degraded")
    ))
    reference = until
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
