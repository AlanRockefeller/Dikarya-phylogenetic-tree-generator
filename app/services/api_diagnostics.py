"""Retain redacted failed upstream responses outside the main error log.

One compressed JSON file per failure avoids interleaving across Gunicorn/RQ
processes. No response-size truncation or automatic expiry: these are evidence.
Only a correlation ID and endpoint go into ordinary logs.
"""
import gzip
import io
import json
import logging
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request

from app.services.log_context import _current_context

logger = logging.getLogger(__name__)
ARCHIVE_DIR = Path(__file__).resolve().parents[2] / "var" / "logs" / "api-responses"
_SENSITIVE = re.compile(r"password|secret|token|authorization|cookie|api.?key|client.?secret|session|^code$|^key$", re.I)
_HEADERS = {"content-type", "content-length", "date", "retry-after", "x-request-id",
            "x-correlation-id", "age", "etag", "last-modified", "cache-control"}


def safe_url(url):
    parts = parse.urlsplit(str(url))
    return parse.urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))


def _secrets(req=None):
    values = [v for k, v in os.environ.items() if _SENSITIVE.search(k) and len(v) >= 4]
    if isinstance(req, request.Request):
        for key, value in req.header_items():
            if _SENSITIVE.search(key):
                values.extend([value, value.split(" ", 1)[-1]])
        for key, value in parse.parse_qsl(parse.urlsplit(req.full_url).query):
            if _SENSITIVE.search(key):
                values.append(value)
        if req.data:
            try:
                data = json.loads(req.data)
            except (ValueError, UnicodeError):
                data = dict(parse.parse_qsl(req.data.decode("utf-8", errors="replace")))
            if isinstance(data, dict):
                values.extend(str(v) for k, v in data.items() if _SENSITIVE.search(k))
    return sorted({v for v in values if len(v) >= 4}, key=len, reverse=True)


def _redact(value, secrets):
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if _SENSITIVE.search(str(k)) else _redact(v, secrets)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        # Also cover key=value and key: value in text/HTML error pages.
        value = re.sub(r'(?i)((?:access_token|refresh_token|api[_-]?key|password|client_secret|authorization)["\x27]?\s*[=:]\s*["\x27]?(?:(?:Bearer|Basic)\s+)?)[^\s"\x27<>&,}]+',
                       r'\1[REDACTED]', value)
        value = re.sub(r'(?is)(<(access_token|refresh_token|api[_-]?key|password|client_secret|authorization)\b[^>]*>).*?(</\2\s*>)',
                       r'\1[REDACTED]\3', value)
    return value


def record_api_failure(url, *, reason, body=None, status=None, headers=None, method="GET", req=None):
    """Best effort; a diagnostic failure must never replace the original error."""
    try:
        secrets = _secrets(req)
        encoding = "utf-8"
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8")
            except UnicodeDecodeError:
                encoding = "latin-1"
                body = body.decode("latin-1")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                pass
        now = datetime.now(timezone.utc)
        diagnostic_id = uuid.uuid4().hex
        directory = ARCHIVE_DIR / now.strftime("%Y-%m-%d")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{diagnostic_id}.json.gz"
        context = _current_context()
        # Stable user IDs are preferable, but existing request correlation may
        # supply an email. Keep it in the restricted diagnostic, not the URL.
        payload = _redact({
            "id": diagnostic_id, "captured_at": now.isoformat(),
            "url": safe_url(url), "method": method, "status": status,
            "query": parse.parse_qs(parse.urlsplit(str(url)).query, keep_blank_values=True),
            "reason": reason, "context": context,
            "headers": {k: v for k, v in (headers or {}).items() if k.lower() in _HEADERS},
            "body": body, "body_available": body is not None, "body_encoding": encoding,
        }, secrets)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
        with os.fdopen(fd, "wb") as stream:
            with gzip.GzipFile(fileobj=stream, mode="wb") as archive:
                archive.write(json.dumps(payload, ensure_ascii=True).encode("utf-8"))
        logger.warning("event=api.response_failed diagnostic=%s status=%s endpoint=%s reason=%s",
                       diagnostic_id, status, safe_url(url), _redact(reason, secrets))
        return diagnostic_id
    except Exception as exc:
        logger.warning("event=api.diagnostic_write_failed error_type=%s", type(exc).__name__)
        return None


class _Response:
    def __init__(self, response):
        self.response = response
        self.body = []

    def read(self, *args, **kwargs):
        data = self.response.read(*args, **kwargs)
        self.body.append(data)
        return data

    def __getattr__(self, name):
        return getattr(self.response, name)


@contextmanager
def diagnostic_urlopen(req, *args, opener=None, **kwargs):
    """Preserve retry behavior and HTTPError bodies for existing callers."""
    url = req.full_url if isinstance(req, request.Request) else str(req)
    method = req.get_method() if isinstance(req, request.Request) else "GET"
    try:
        response = (opener or request.urlopen)(req, *args, **kwargs)
    except error.HTTPError as exc:
        raw = None
        try:
            raw = exc.read()
        except Exception:
            pass
        record_api_failure(url, reason="http_error", body=raw, status=exc.code,
                           headers=exc.headers, method=method, req=req)
        if raw is not None:
            # HTTPError delegates read to its file; restore it after capture.
            exc.close()
            exc.fp = exc.file = io.BytesIO(raw)
            exc.read = exc.file.read
        raise
    except (error.URLError, TimeoutError, OSError) as exc:
        record_api_failure(url, reason=type(exc).__name__, method=method, req=req)
        raise
    with response as opened:
        tracked = _Response(opened)
        try:
            yield tracked
        except Exception as exc:
            record_api_failure(url, reason=type(exc).__name__, body=b"".join(tracked.body),
                               status=getattr(opened, "status", None),
                               headers=getattr(opened, "headers", None), method=method, req=req)
            raise


def record_requests_failure(response, reason="http_error"):
    try:
        if response is None:
            return
        # PreparedRequest is not a urllib Request; adapt only credential fields.
        req = request.Request(str(response.url), headers=dict(response.request.headers))
        data = getattr(response.request, "body", None)
        if isinstance(data, (str, bytes)):
            req.data = data.encode() if isinstance(data, str) else data
        record_api_failure(response.url, reason=reason, body=response.content,
                           status=response.status_code, headers=response.headers,
                           method=response.request.method, req=req)
    except Exception as exc:
        logger.warning("event=api.diagnostic_write_failed error_type=%s", type(exc).__name__)
