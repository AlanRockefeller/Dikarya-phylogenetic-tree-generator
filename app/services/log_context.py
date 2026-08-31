"""Small, process-safe logging context helpers for requests and RQ jobs."""

import contextvars
import functools
import hashlib
import logging
import os
import re
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DEGRADED_MARKER = "DEGRADED"
_UNSET = "-"
_BACKGROUND = contextvars.ContextVar("dikarya_log_context", default={})
_FACTORY_INSTALLED = False
_DEGRADED_LAST = {}
_DEGRADED_LOCK = threading.Lock()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RELEASE_CACHE = None
_RELEASE_LOCK = threading.Lock()

# Handler markers. Both the web and the worker call create_app(), sometimes more
# than once in a process (CLI commands, tests, the reconcile scripts), so every
# handler this module installs is tagged and looked up before anything is opened.
CONSOLE_MARKER = "_dikarya_console"
ERROR_MIRROR_MARKER = "_dikarya_error_mirror"


def _read_git_head(git_dir: Path) -> str:
    """Read .git/HEAD (following one ref indirection). Split out so it is countable."""
    head = (git_dir / "HEAD").read_text().strip()
    if head.startswith("ref: "):
        head = (git_dir / head[5:]).read_text().strip()
    return head


def _resolve_release(base_dir) -> str:
    configured = (
        os.environ.get("DIKARYA_RELEASE")
        or os.environ.get("RELEASE_VERSION")
        or os.environ.get("GIT_COMMIT")
    )
    if configured:
        return str(configured)[:80]
    try:
        head = _read_git_head(Path(base_dir) / ".git")
        if head:
            return f"git:{head[:12]}"
    except OSError:
        pass
    return "unknown"


def configured_release(base_dir=None) -> str:
    """Return a bounded, non-secret deployment identifier.

    Alan 8/15/26 - This is stamped onto every log record by the record factory,
    so the uncached version re-opened .git/HEAD (plus the ref file) once per
    record -- two stat+read syscalls per log line in the hot path of every
    request and every pipeline step. The release cannot change inside a running
    process, so resolve it once. ``reset_release_cache()`` exists for tests.

    This is the single canonical resolver; ``app.config`` defers to it.
    """
    global _RELEASE_CACHE
    if base_dir is not None:
        return _resolve_release(base_dir)
    cached = _RELEASE_CACHE
    if cached is not None:
        return cached
    with _RELEASE_LOCK:
        if _RELEASE_CACHE is None:
            _RELEASE_CACHE = _resolve_release(_REPO_ROOT)
        return _RELEASE_CACHE


def reset_release_cache() -> None:
    """Forget the cached release (tests only)."""
    global _RELEASE_CACHE
    with _RELEASE_LOCK:
        _RELEASE_CACHE = None


def background_user_identity(db_job) -> str:
    """Return the log identity for a background job's owner.

    Deliberately never the email address. Background job logs are written to
    files that are read, digested and pasted around far more freely than the
    request log, and the address adds nothing a grep on the stable internal id
    cannot do. An ownerless (anonymous) job reports "anon".
    """
    user_id = getattr(db_job, "user_id", None) if db_job is not None else None
    return f"id:{user_id}" if user_id else "anon"


def bind_background_context(**values):
    """Merge safe values into the current task context and return a reset token."""
    current = dict(_BACKGROUND.get())
    current.update({key: str(value)[:300] for key, value in values.items() if value not in (None, "")})
    return _BACKGROUND.set(current)


def reset_background_context(token) -> None:
    _BACKGROUND.reset(token)


def set_pipeline_context(*, step=None, tool=None) -> None:
    """Update the active task's step/tool without disturbing job correlation."""
    current = dict(_BACKGROUND.get())
    if step is not None:
        current["step"] = str(step)[:80]
    if tool is not None:
        current["tool"] = str(tool)[:80]
    elif tool is None:
        current.pop("tool", None)
    _BACKGROUND.set(current)


@contextmanager
def background_context(**values):
    token = bind_background_context(**values)
    try:
        yield
    finally:
        reset_background_context(token)


def background_job_context(job_id_arg=None, *, pipeline_log=False):
    """Decorator that reliably sets/resets context around every RQ entry point."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                from rq import get_current_job
                rq_job = get_current_job()
            except Exception:
                rq_job = None
            app_job = None
            if isinstance(job_id_arg, int) and len(args) > job_id_arg:
                app_job = args[job_id_arg]
            elif isinstance(job_id_arg, str):
                app_job = kwargs.get(job_id_arg)
            app_job = app_job or (getattr(rq_job, "id", None)) or "local_debug"
            with background_context(
                job=app_job,
                rq=getattr(rq_job, "id", None) or _UNSET,
                release=configured_release(),
            ):
                handler = None
                if pipeline_log:
                    try:
                        from app.config import Config
                        log_path = Config.JOB_DIR / str(app_job) / "logs" / "pipeline.log"
                        log_path.parent.mkdir(parents=True, exist_ok=True)
                        handler = logging.FileHandler(log_path)
                        handler.addFilter(JobContextFilter(app_job))
                        handler.setFormatter(utc_formatter(
                            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
                        ))
                        logging.getLogger().addHandler(handler)
                    except OSError:
                        handler = None
                try:
                    return fn(*args, **kwargs)
                finally:
                    if handler is not None:
                        logging.getLogger().removeHandler(handler)
                        handler.close()
        return wrapped
    return decorate


def _current_context():
    context = dict(_BACKGROUND.get())
    context.setdefault("release", configured_release())
    try:
        from flask import g, has_request_context, request
        if has_request_context():
            context["req"] = getattr(g, "request_id", None) or _UNSET
            api_user = getattr(g, "api_user", None)
            if api_user is not None:
                context["user"] = (
                    getattr(api_user, "email", None)
                    or f"id:{getattr(api_user, 'id', 'unknown')}"
                )
            else:
                try:
                    from flask_login import current_user
                    context["user"] = (
                        getattr(current_user, "email", None)
                        or f"id:{current_user.id}"
                    ) if current_user and current_user.is_authenticated else "anon"
                except Exception:
                    context.setdefault("user", "anon")
            view_args = request.view_args or {}
            context["job"] = view_args.get("job_id") or getattr(g, "job_id", None) or context.get("job", _UNSET)
    except Exception:
        pass
    return context


def install_record_factory():
    global _FACTORY_INSTALLED
    if _FACTORY_INSTALLED:
        return
    previous_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous_factory(*args, **kwargs)
        for key, value in _current_context().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return record

    logging.setLogRecordFactory(factory)
    _FACTORY_INSTALLED = True


# Every timestamp Dikarya writes into a log file is UTC, explicitly.
#
# logging.Formatter defaults its converter to time.localtime, while
# scripts/dikarya_log_digest.py reads a timestamp with no offset as UTC -- the
# convention its own window arithmetic uses. On a server whose TZ is not UTC
# those two beliefs differ by the offset, which silently shifts every "is this
# job stale?" and "is this record in the review window?" decision. These logs
# are machine-consumed, so the fix is to make the emitted convention match the
# one the reader documents rather than to teach the reader about local time.
# User-facing date rendering elsewhere is deliberately untouched.
_UTC_LOG_CONVERTER = time.gmtime


def utc_formatter(fmt: str, datefmt=None) -> logging.Formatter:
    """A plain logging.Formatter that renders %(asctime)s in UTC."""
    formatter = logging.Formatter(fmt, datefmt)
    formatter.converter = _UTC_LOG_CONVERTER
    return formatter


class ContextFormatter(logging.Formatter):
    """Append correlation fields to the first line, before any traceback."""

    converter = _UTC_LOG_CONVERTER

    def format(self, record):
        base = super().format(record)
        values = {key: getattr(record, key, _UNSET) for key in (
            "req", "user", "job", "rq", "step", "tool", "release"
        )}
        useful = [(key, value) for key, value in values.items() if value not in (None, "", _UNSET)]
        if not useful:
            return base
        suffix = "[" + " ".join(f"{key}={value}" for key, value in useful) + "]"
        first, separator, rest = base.partition("\n")
        return f"{first} {suffix}{separator}{rest}"


class JobContextFilter(logging.Filter):
    """Allow only records for one application job into its pipeline log."""
    def __init__(self, job_id):
        super().__init__()
        self.job_id = str(job_id)

    def filter(self, record):
        return str(getattr(record, "job", "")) == self.job_id


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def stable_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def log_degradation(logger, what: str, detail: str, **context) -> None:
    extras = " ".join(f"{key}={str(value)[:200]!r}" for key, value in sorted(context.items()))
    logger.warning(
        "event=degraded.%s %s %s: %s%s",
        what, DEGRADED_MARKER, what, detail[:500], f" [{extras}]" if extras else "",
    )


def log_degradation_rate_limited(
    logger, what: str, detail: str, *, interval_seconds=300, **context
) -> None:
    """Emit a repeated fail-open signal at most once per process/interval."""
    now = time.monotonic()
    with _DEGRADED_LOCK:
        if now - _DEGRADED_LAST.get(what, 0) < interval_seconds:
            return
        _DEGRADED_LAST[what] = now
    log_degradation(logger, what, detail, **context)


# --------------------------------------------------------------------------
# Handler installation
#
# Alan 8/15/26 - The WARNING-only errors.log handler is attached to the *root*
# logger, and that had two consequences nobody wanted:
#
#   * RQ's setup_loghandlers() calls _has_effective_handler(), which walks up to
#     the root logger. Seeing a handler there, it decided RQ logging was already
#     configured and never installed its own stdout/stderr handlers -- so
#     worker.log stopped receiving "phylo_high: ... (uuid)" / "Job OK (uuid)".
#   * app/workers/tasks.py called logging.basicConfig(level=INFO) at import time,
#     which is a documented no-op once the root logger has any handler. The root
#     logger therefore stayed at its default WARNING, which disables INFO for
#     every app.* logger -- taking out event=job.started/completed, the pipeline
#     invariant checks, and every INFO record the per-job pipeline.log handler
#     was installed to capture.
#
# The fix is to stop depending on basicConfig entirely: set the root level
# explicitly, keep the errors mirror at WARNING (so errors.log is unchanged), and
# give non-Gunicorn processes a real stdout/stderr console path. Under Gunicorn
# no console handler is added, because Gunicorn already owns the process's
# stdout/stderr and app.logger already points at its handlers -- adding one would
# duplicate every line into error.log.
# --------------------------------------------------------------------------

def _has_marked_handler(logger_obj, marker) -> bool:
    return any(getattr(handler, marker, False) for handler in logger_obj.handlers)


def ensure_root_level(level=logging.INFO) -> None:
    """Make sure records at ``level`` are not discarded before reaching handlers."""
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)


def install_console_logging(level=logging.INFO) -> bool:
    """Give worker/metrics/CLI processes an explicit stdout+stderr log path.

    Returns True when handlers were added, False when they were already present.
    Nothing is opened before the duplicate check, so repeated create_app() calls
    cannot leak file descriptors.
    """
    root = logging.getLogger()
    if _has_marked_handler(root, CONSOLE_MARKER):
        return False
    out_handler = logging.StreamHandler(stream=sys.stdout)
    out_handler.setLevel(level)
    # ERROR+ goes to stderr only, mirroring what RQ's own handlers do, so a
    # reader of worker.log never sees the same failure twice.
    out_handler.addFilter(lambda record: record.levelno < logging.ERROR)
    err_handler = logging.StreamHandler(stream=sys.stderr)
    err_handler.setLevel(logging.ERROR)
    for handler in (out_handler, err_handler):
        handler.setFormatter(ContextFormatter(
            "[%(asctime)s] [%(process)d] [%(levelname)s] [%(name)s] %(message)s"
        ))
        setattr(handler, CONSOLE_MARKER, True)
        root.addHandler(handler)
    ensure_root_level(level)
    return True


def install_error_mirror(path, level=logging.WARNING) -> bool:
    """Attach the WARNING+ mirror (errors.log) to the root logger, once."""
    from logging.handlers import WatchedFileHandler

    root = logging.getLogger()
    # FileHandler stores baseFilename as an absolute path, so comparing a
    # relative target against it never matched and the "already installed"
    # check silently failed -- adding a second handler on the same file and
    # duplicating every WARNING in errors.log.
    target = os.path.abspath(str(path))
    already = _has_marked_handler(root, ERROR_MIRROR_MARKER) or any(
        getattr(handler, "baseFilename", None) == target for handler in root.handlers
    )
    if already:
        return False
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    # Rotation is external (ops/logrotate/dikarya). WatchedFileHandler notices
    # replacement and is safe across Gunicorn/RQ processes; RotatingFileHandler
    # can race and lose records during rollover.
    handler = WatchedFileHandler(target)
    handler.setLevel(level)
    handler.setFormatter(ContextFormatter(
        "[%(asctime)s] [%(process)d] [%(levelname)s] [%(name)s] %(message)s"
    ))
    setattr(handler, ERROR_MIRROR_MARKER, True)
    root.addHandler(handler)
    return True


def install_rq_logging(level=logging.INFO) -> None:
    """Make sure RQ's own loggers emit at INFO through the root console path.

    RQ only sets its logger level inside Worker.work(); several code paths (the
    scheduler thread, maintenance) log before that. Setting it here means an RQ
    line is never dropped just because our root handler pre-empted RQ's own
    handler installation.
    """
    for name in ("rq", "rq.worker", "rq.scheduler"):
        logging.getLogger(name).setLevel(level)


# --------------------------------------------------------------------------
# Telemetry sanitizing
# --------------------------------------------------------------------------

# Sequence payloads are the single largest privacy risk in this application:
# an unsanitized browser stack trace or job description can carry an entire
# specimen FASTA into a log file that is read by ops tooling and cron digests.
_SEQ_RE = re.compile(r"(?i)\b[ACGTURYSWKMBDHVN]{25,}\b")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"[\r\n\t]+")
_UUID_LIKE_RE = re.compile(r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Anything after ? or # in a URL or absolute path is caller-supplied and may hold
# OAuth codes, session state, or search text.
_URL_QUERY_RE = re.compile(r"(?P<base>(?:[A-Za-z][\w+.-]*://|/)[^\s?#]*)[?#]\S*")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]*)?")
_OPAQUE_TOKEN_RE = re.compile(r"(?<![\w./-])[A-Za-z0-9_-]{32,}(?![\w./-])")
_CREDENTIAL_RES = (
    # "Authorization: Bearer <token>" -- the optional scheme word has to be
    # consumed too, or only the word "Bearer" is redacted and the token stays.
    (re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:[A-Za-z][\w-]*\s+)?[^\s,;'\"]+"),
     "authorization=<redacted>"),
    # "Cookie: a=1; b=2". Bounded to the cookie pairs: an unbounded ".*" here
    # swallowed the whole rest of the stack trace and destroyed the diagnostics.
    (re.compile(r"(?i)\bcookies?\s*[:=]\s*[^\s,'\"]+(?:;\s*[^\s,'\"]+)*"), "cookie=<redacted>"),
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{4,}"), r"\1 <redacted>"),
    (re.compile(
        r"(?i)\b(code|state|token|access[_-]?token|refresh[_-]?token|id[_-]?token|"
        r"auth|authorization|api[_-]?key|apikey|client[_-]?secret|secret|"
        r"password|passwd|pwd|session|sessionid|sid|signature|sig)"
        r"\s*[=:]\s*[^\s&;,'\"<>]+"
    ), r"\1=<redacted>"),
)
TELEMETRY_MAX_LENGTH = 2000


def sanitize_telemetry_text(value, max_length: int = TELEMETRY_MAX_LENGTH) -> str:
    """Return a bounded, credential-free, sequence-free version of client text.

    Applied server-side to every untrusted telemetry field. The browser helper
    does the same cleanup, but browser code is attacker-controllable and a
    direct POST bypasses it entirely, so this copy is the one that counts.

    What survives on purpose: exception type names, stable action identifiers,
    path names, and the shape of a stack trace -- everything needed to find the
    bug. What does not: query strings, credentials, and nucleotide runs.
    """
    text = "" if value is None else str(value)
    text = _CONTROL_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _URL_QUERY_RE.sub(r"\g<base>?<redacted>", text)
    text = _JWT_RE.sub("<redacted-jwt>", text)
    for pattern, replacement in _CREDENTIAL_RES:
        text = pattern.sub(replacement, text)
    text = _SEQ_RE.sub(lambda match: f"<sequence:{len(match.group(0))}>", text)
    text = _OPAQUE_TOKEN_RE.sub(
        lambda match: match.group(0) if _UUID_LIKE_RE.match(match.group(0)) else "<redacted-token>",
        text,
    )
    return text.strip()[:max_length]
