"""Request outcomes without copying request bodies or response error text."""

import logging
import re
import time
from http import HTTPStatus

from flask import g, has_request_context, request


def note_request_failure(code):
    """Attach a developer-defined reason code, never a user's input or message."""
    if has_request_context() and isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
        g.request_failure_code = code


def install_request_diagnostics(app):
    @app.before_request
    def start_request_clock():
        g.request_started_at = time.monotonic()

    @app.after_request
    def log_failed_request(response):
        status = response.status_code
        if status < 400:
            return response
        # Collector rejections (including CSRF before its handler runs) need
        # diagnostics too; record only the reason code, never the payload.
        if status < 500 and (
            request.url_rule is None
            or request.endpoint == "static"
        ):
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
