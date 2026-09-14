"""Request outcomes without copying request bodies or response error text."""

import logging
import re
import time
from http import HTTPStatus

from flask import g, has_request_context, request

from app.services.security_events import BUCKET_SCANNER, BUCKET_TARGETED

_KNOWN_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
_JOB_PATH_RE = re.compile(r"^/(?:api/)?job/([^/]+)")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def note_request_failure(code):
    """Attach a developer-defined reason code, never a user's input or message."""
    if has_request_context() and isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
        g.request_failure_code = code



def _safe_path():
    """The requested path only -- never the query string, which can carry input."""
    return request.path if has_request_context() else "/"


def _safe_method():
    return request.method if request.method in _KNOWN_METHODS else "OTHER"


def _job_id_validity():
    """True/False when the matched route carries a job id, else None.

    A malformed id is the signal worth having: ids are short and guessable by
    design, so a *valid-shaped* id that simply 404s is an ordinary stale link.
    """
    view_args = (request.view_args or {}) if has_request_context() else {}
    job_id = view_args.get("job_id")
    if job_id is None:
        # An unmatched /job/<something> never populates view_args, so pull the
        # candidate out of the path instead.
        match = _JOB_PATH_RE.match(request.path or "")
        if not match:
            return None
        job_id = match.group(1)
    try:
        from app.services.security_utils import validate_job_id
        return bool(validate_job_id(job_id))
    except Exception:
        return None


def _app_path_segments(app):
    """First path segment of every rule the app serves, computed once.

    Derived from the live URL map rather than hardcoded, so a page added later
    is recognised as this app's surface without anyone remembering to update a
    list -- otherwise a genuine broken link to a new route would be filed away
    as scanner noise.
    """
    cached = getattr(app, "_dikarya_path_segments", None)
    if cached is not None:
        return cached
    segments = set()
    for rule in app.url_map.iter_rules():
        first = rule.rule.strip("/").split("/", 1)[0].lower()
        if first and not first.startswith("<"):
            segments.add(first)
    app._dikarya_path_segments = segments
    return segments


def _classify(app, status):
    from app.services.security_events import classify_request_failure

    return classify_request_failure(
        path=_safe_path(),
        method=_safe_method(),
        status=status,
        matched_route=request.url_rule.rule if request.url_rule is not None else None,
        user_agent=request.headers.get("User-Agent", "")[:200],
        job_id_valid=_job_id_validity(),
        # Inspected for injection markers only. The query string holds user
        # input (sequence text, search terms) and is never written to a log.
        query=request.query_string.decode("latin-1", "replace")[:500],
        app_segments=_app_path_segments(app),
    )


def _log_scanner(status, reason):
    """Internet-wide junk: its own file, INFO, never the root logger."""
    from app.services.log_context import scanner_logger

    scanner_logger().info(
        "event=security.scanner method=%s path=%s status=%s reason=%s client=%s",
        _safe_method(), _scrub(_safe_path()), status, reason,
        request.remote_addr or "-",
    )


def _log_targeted(app, status, reason):
    """A probe aimed at this application: WARNING, so it reaches errors.log."""
    app.logger.warning(
        "event=security.suspicious method=%s path=%s status=%s reason=%s "
        "client=%s agent=%s",
        _safe_method(), _scrub(_safe_path()), status, reason,
        request.remote_addr or "-",
        _scrub(request.headers.get("User-Agent", "-")[:120]),
    )


def _scrub(value):
    """Bound the length and strip anything that could break a log line.

    The path of a probe is attacker-controlled text going into a file an
    operator will read, so newlines (log injection) and control characters are
    folded out before it is written.
    """
    text = _CONTROL_RE.sub("", str(value))[:300]
    return text.replace(" ", "%20") or "-"


def install_request_diagnostics(app):
    @app.before_request
    def start_request_clock():
        g.request_started_at = time.monotonic()

    @app.after_request
    def log_failed_request(response):
        status = response.status_code
        if status < 400:
            return response

        # Alan 9/12/26 - An unmatched 4xx used to return here unlogged, which
        # kept vulnerability sweeps out of errors.log but also meant a probe
        # aimed at Dikarya's own surface -- traversal against a job artifact
        # path, a malformed job id, mapping /api/ -- left no record anywhere
        # except Gunicorn's access.log. Classify instead of discarding: junk
        # goes to its own file, anything aimed at this app is reported.
        #
        # This only ADDS a record. A matched route keeps its ordinary
        # http.request_failed line below, because that line carries the
        # developer reason code (scope_required, csrf_token_missing) and
        # returning early here would silently throw it away.
        bucket = reason = None
        if status < 500 and request.endpoint != "static":
            bucket, reason = _classify(app, status)
            if bucket == BUCKET_TARGETED:
                _log_targeted(app, status, reason)

        if status < 500 and (
            request.url_rule is None
            or request.endpoint == "static"
        ):
            # Nothing matched, so there is no route to report. Scanner junk is
            # filed in its own log; a targeted probe was already reported above.
            if bucket == BUCKET_SCANNER:
                _log_scanner(status, reason)
            return response

        code = getattr(g, "request_failure_code", None)
        if not code:
            try:
                code = HTTPStatus(status).name.lower()
            except ValueError:
                code = "http_error"
        started = getattr(g, "request_started_at", None)
        duration_ms = (time.monotonic() - started) * 1000 if started is not None else 0
        # A rule has placeholders, not submitted path/query values. Do not
        # inspect response bodies: errors can echo FASTA, labels or credentials,
        # and reading a streaming response here could consume a download/SSE.
        app.logger.log(
            logging.ERROR if status >= 500 else logging.WARNING,
            "event=http.request_failed method=%s route=%s status=%s reason=%s "
            "duration_ms=%.1f request_bytes=%s",
            request.method if request.method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"} else "OTHER",
            request.url_rule.rule if request.url_rule is not None else "<unmatched>",
            status, code, duration_ms, request.content_length,
        )
        return response
