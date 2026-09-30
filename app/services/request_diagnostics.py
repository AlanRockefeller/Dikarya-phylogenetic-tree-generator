"""Request outcomes without copying request bodies or response error text."""

import logging
import re
import time
from http import HTTPStatus

from flask import g, has_request_context, request

from app.services.security_events import (
    BUCKET_SCANNER, BUCKET_TARGETED, TARGETED_REASONS,
)

_JOB_PATH_RE = re.compile(r"^/(?:api/)?job/([^/]+)")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Reasons that earn a WARNING of their own, every time, regardless of score.
# Each one is an attack with no innocent reading: traversal, a null byte, an
# injection marker, somebody naming this app's artifact layout or the binaries
# it executes, a refusal reported by the check that stopped it, or a honeytoken
# nobody can reach by accident.
#
# api_surface_probe and admin_probe are deliberately NOT here. They fired 61
# times in one ordinary day, essentially all of it `.env` hunting under an
# /api/ prefix, and a WARNING that arrives 61 times a day for nothing is a
# WARNING people learn to skip. They now feed the actor score instead, where
# volume is the point rather than the problem.
IMMEDIATE_WARN_REASONS = frozenset({
    "honeytoken",
    "path_traversal",
    "path_traversal_app_surface",
    "null_byte",
    "injection_probe",
    "artifact_path_probe",
    "tool_exploit_probe",
    "job_id_malformed",
    "path_escape_refused",
    "argument_injection_refused",
})

# Verbs the app serves somewhere, so Flask answers 405 rather than 404. Aimed
# at a route that does not take them, this is hand-driven: the UI only ever
# calls endpoints it knows. A verb Flask serves nowhere (PROPFIND, TRACE) is
# sweep signature and stays in security_events' scanner bucket.
_PROBING_VERBS = frozenset({"PUT", "DELETE", "PATCH", "OPTIONS"})


def note_request_failure(code):
    """Attach a developer-defined reason code, never a user's input or message."""
    if has_request_context() and isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
        g.request_failure_code = code


def note_attack_attempt(reason, token=""):
    """Report an attack that server-side code refused, not one read off a URL.

    The URL classifier can only see the request line. The attacks that matter
    most against this app are not visible there: a path that only turns out to
    escape ``var/jobs`` once it is resolved, a symlink planted in a job
    directory, a value that would have reached a MAFFT or RAxML command line as
    a flag. Those are caught deep in ``security_utils`` and the pipeline, which
    until now simply refused them and returned False -- correct, and silent.

    Whatever refuses one calls this, and the request's after_request hook turns
    it into the same WARNING and the same actor score as a traversal in a URL.
    A refusal means a real attempt reached the check that stopped it, so these
    carry enough weight to escalate on their own.

    ``token`` distinguishes repeated attempts from one repeated attempt; it is
    bounded and stripped before storage and is never written to a log.
    """
    if not has_request_context():
        return
    if reason not in TARGETED_REASONS:
        return
    attempts = getattr(g, "security_attack_attempts", None)
    if attempts is None:
        attempts = g.security_attack_attempts = []
    # Bounded: a loop over a thousand bad paths must not build a list of a
    # thousand entries inside one request.
    if len(attempts) < 8:
        attempts.append((reason, str(token or "")[:80]))



def note_tool_argument_refusal(field, value, logger=None):
    """Report a refused value that was shaped like a tool argument.

    The bioinformatics binaries are the part of Dikarya that actually executes
    something, and several routes accept free text that ends up in one of their
    argv lists (a model string above all). The validators already refuse
    anything unsafe -- this does not add a check, it makes the existing refusal
    audible, and only for values that look like an attempt rather than a typo.

    Works on both sides of the queue. Inside a request it becomes an actor
    signal; in the worker, where there is no actor to score, it is written
    straight out as a WARNING carrying the job and user from the log context.
    """
    from app.services.security_events import looks_weaponized

    if not looks_weaponized(value):
        return False
    if has_request_context():
        note_attack_attempt("argument_injection_refused", f"{field}")
        return True
    (logger or logging.getLogger(__name__)).warning(
        "event=security.suspicious method=WORKER path=- status=0 "
        "reason=argument_injection_refused client=- agent=- field=%s",
        _scrub(field),
    )
    return True


def _safe_path():
    """The requested path only -- never the query string, which can carry input."""
    return request.path if has_request_context() else "/"


def _safe_method():
    """The request method, bounded but not renamed.

    Alan 9/14/26 - This used to collapse everything outside the standard set to
    the literal "OTHER" before classification, which threw away the scanner
    signatures that matter most: PROPFIND (WebDAV discovery), TRACE (cross-site
    tracing) and CONNECT (open-proxy tests) all arrived indistinguishable from
    each other and from a garbage verb. normalize_method() bounds the untrusted
    value -- uppercase letters only, length-capped, so it cannot inject a log
    line -- while leaving PROPFIND saying PROPFIND.
    """
    from app.services.security_events import normalize_method

    return normalize_method(request.method if has_request_context() else "GET")


def _job_id_from_request():
    """The job id this request is about, matched route or not, else None."""
    view_args = (request.view_args or {}) if has_request_context() else {}
    job_id = view_args.get("job_id")
    if job_id is None:
        # An unmatched /job/<something> never populates view_args, so pull the
        # candidate out of the path instead.
        match = _JOB_PATH_RE.match(request.path or "")
        if not match:
            return None
        job_id = match.group(1)
    return job_id


def _job_id_validity():
    """True/False when the matched route carries a job id, else None.

    A malformed id is the signal worth having: ids are short and guessable by
    design, so a *valid-shaped* id that simply 404s is an ordinary stale link.
    """
    job_id = _job_id_from_request()
    if job_id is None:
        return None
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


def _actor_key():
    from app.services.security_actors import actor_key

    return actor_key(request.remote_addr if has_request_context() else None)


def _honeytoken(status):
    """The honeytoken this request tripped, or None.

    Only counted on a failing response: if one of these paths ever starts
    answering 2xx it has stopped being a decoy and become a route, and
    reporting its users as attackers would be the worst kind of false positive.
    """
    if status < 400:
        return None
    from app.services.security_honeytokens import honeytoken_hit

    return honeytoken_hit(_safe_path())


def _log_scanner(status, reason):
    """Internet-wide junk: its own file, INFO, never the root logger."""
    from app.services.log_context import scanner_logger

    scanner_logger().info(
        "event=security.scanner method=%s path=%s status=%s reason=%s client=%s",
        _safe_method(), _scrub(_safe_path()), status, reason,
        request.remote_addr or "-",
    )


def _security_state(app):
    """The three shared detectors, built once per app.

    Constructed lazily and stored on ``app.extensions`` rather than at import
    time, so importing this module (which the digest does, indirectly) never
    opens a socket, and so a Redis that is down at boot costs nothing.
    """
    state = app.extensions.get("security_actor_state")
    if state is not None:
        return state

    from app.services.security_actors import ActorScorer, BrowserWitness
    from app.services.security_path_crowd import PathCrowd

    def redis_factory():
        # The same short-timeout client the missing-route gate uses: 100ms, so
        # a wedged Redis delays a response by a tenth of a second at worst.
        from app import _scanner_404_redis

        return _scanner_404_redis()

    def on_error(exc):
        from app.services.log_context import log_degradation_rate_limited

        log_degradation_rate_limited(
            app.logger,
            "security_actor_store_unavailable",
            "Shared actor scoring store unavailable; using per-process fallback",
            exception=type(exc).__name__,
        )

    state = {
        "scorer": ActorScorer(redis_factory, on_error=on_error),
        "crowd": PathCrowd(redis_factory, on_error=on_error),
        "browser": BrowserWitness(redis_factory, on_error=on_error),
    }
    app.extensions["security_actor_state"] = state
    return state


def _collect_signals(app, status, bucket, reason, matched, browser_seen, crowd_verdict):
    """The scoring signals this one request contributes.

    Tokens are what makes a signal *distinct* -- a job id, a route pattern, a
    verb, a normalised path. None of them is a submission value, and none of
    them is ever logged: only counts reach the escalation line.
    """
    from app.services.security_actors import SIGNALS
    from app.services.security_path_crowd import (
        VERDICT_DICTIONARY, VERDICT_SINGLETON,
    )
    from app.services.security_events import _is_app_surface  # noqa: PLC2701

    signals = []
    path = _safe_path()
    method = _safe_method()

    # Anything the refusing code reported directly (see note_attack_attempt).
    for attack_reason, token in getattr(g, "security_attack_attempts", ()) or ():
        signals.append((attack_reason, token or path))

    if bucket == BUCKET_TARGETED and reason in SIGNALS:
        # A dictionary path scores nothing. `/api/.env` came from nine distinct
        # clients in one day: it is vocabulary, not a guess about us, and
        # letting it accumulate is how a sweep would escalate on volume alone.
        if not (reason in ("api_surface_probe", "admin_probe")
                and crowd_verdict == VERDICT_DICTIONARY):
            signals.append((reason, path))

    # An unmatched path shaped like this app's surface that no other client has
    # ever requested. The inverse of the rule above, and the reason the crowd
    # count is worth keeping: a scanner cannot produce a singleton, because its
    # dictionary is shared with every other scanner on the internet.
    if (
        matched is None
        and crowd_verdict == VERDICT_SINGLETON
        and _is_app_surface(path.lower(), _app_path_segments(app))
    ):
        signals.append(("unique_app_shaped_path", path))

    job_id = _job_id_from_request()
    if job_id and _job_id_validity() is True:
        # A valid-shaped id that 404s is an ordinary stale link -- ids are
        # short and guessable by design -- so this scores nothing until the
        # same actor has collected several DIFFERENT ones.
        if status == 404:
            signals.append(("job_enumeration", job_id))
        elif status in (401, 403):
            signals.append(("job_edit_denied", job_id))

    if matched is not None:
        if status == 400:
            code = getattr(g, "request_failure_code", None) or "bad_request"
            signals.append(("validation_fuzzing", f"{matched}:{code}"))
        if status == 405 and method in _PROBING_VERBS:
            signals.append(("odd_verb_real_route", f"{method}:{matched}"))

    # Both of the following are things a real user's browser does too, so they
    # only count for a client that has never fetched a static asset.
    if not browser_seen:
        if path.rstrip("/").lower() == "/health/jobs":
            signals.append(("health_scrape", "1"))
        if matched is not None and _is_app_surface(
            path.lower(), _app_path_segments(app)
        ):
            signals.append(("route_breadth", matched))

    return signals


def _log_actor_escalation(app, escalation):
    """One line per actor per cooldown, naming the signals and nothing else.

    No path, no query, no user agent string, no sequence-derived text: an
    escalation reports *counts* of named signals, which is enough to know what
    happened and carries none of the submission content the monitoring rules
    keep out of shared views. The evidence for any single request is already in
    access.log, keyed by the same client.
    """
    from app.services.security_actors import (
        ESCALATION_THRESHOLD, WINDOW_SECONDS, format_evidence,
    )

    app.logger.warning(
        "event=security.actor_escalated actor=%s score=%s threshold=%s "
        "signals=%s clients=%s window_hours=%s",
        escalation.actor, escalation.score, ESCALATION_THRESHOLD,
        format_evidence(escalation.counts),
        escalation.counts.get("client_ips", 1),
        WINDOW_SECONDS // 3600,
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
        failed = status >= 400

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
        # Static assets are the browser evidence the actor scoring leans on:
        # a client that fetches a stylesheet is rendering a page, not reading
        # the URL map. Recorded for every status, then done -- a 404 on a
        # missing asset is nobody's attack.
        #
        # Alan 9/24/26 - This and the actor scoring below used to sit behind an
        # early return for status < 400, so an asset was only "seen" when it
        # FAILED to load and route_breadth / health_scrape -- which are 200s --
        # could never fire. Only the failure classification and logging are
        # limited to error responses now.
        state = _security_state(app)
        actor = _actor_key()
        if request.endpoint == "static":
            state["browser"].mark(actor)
            return response

        bucket = reason = None
        crowd_verdict = None
        if failed and status < 500:
            bucket, reason = _classify(app, status)

            # A honeytoken overrides whatever the path would otherwise look
            # like (/admin/db-export would read as an admin probe, /job/q0x9
            # as an ordinary missing job). It is checked here rather than in a
            # before_request so the response stays an ordinary 404 that tells
            # the prober nothing.
            token = _honeytoken(status)
            if token is not None:
                bucket, reason = BUCKET_TARGETED, "honeytoken"

            # Who else has asked for this path? Only for paths that matched no
            # route: a path the app really serves is not vocabulary and not a
            # guess, whoever else has requested it.
            if request.url_rule is None:
                crowd_verdict = state["crowd"].observe(
                    _safe_path(), request.remote_addr
                )

            if bucket == BUCKET_TARGETED and reason in IMMEDIATE_WARN_REASONS:
                _log_targeted(app, status, reason)
            elif bucket == BUCKET_TARGETED:
                # api_surface_probe / admin_probe: recorded where the volume
                # lives rather than as a WARNING each. scanner.log keeps the
                # evidence, the actor score decides whether it matters.
                _log_scanner(status, reason)
            elif bucket == BUCKET_SCANNER:
                # Alan 9/14/26 - Filed whether or not a route matched. This used
                # to sit inside the unmatched-only branch below, so a probe that
                # happened to land on a real rule -- a PROPFIND that Werkzeug
                # answers 405 on, a bot fetching a page that 404s -- was never
                # written to scanner.log at all, and the sweep looked smaller
                # than it was. It only ADDS a record: a matched route still gets
                # its ordinary http.request_failed line below, carrying the
                # developer reason code.
                _log_scanner(status, reason)

        # Score the actor whatever the status was: enumeration shows up as
        # 404s, denied edits as 403s, and route breadth mostly as 200s.
        try:
            signals = _collect_signals(
                app, status, bucket, reason,
                request.url_rule.rule if request.url_rule is not None else None,
                state["browser"].seen(actor),
                crowd_verdict,
            )
            if signals:
                from app.services.security_actors import ua_family

                # Recorded only alongside a real signal, so an ordinary
                # request writes nothing: which addresses inside the network
                # took part, and whether the client keeps changing what it
                # claims to be.
                signals.append(("client_ips", request.remote_addr or "-"))
                signals.append((
                    "ua_rotation",
                    ua_family(request.headers.get("User-Agent", "")),
                ))
                escalation = state["scorer"].record(actor, signals)
                if escalation is not None:
                    _log_actor_escalation(app, escalation)
        except Exception:
            # Detection must never be the reason a response fails. A broken
            # signal is a missed attacker; a raised exception here is a broken
            # site, and this runs on every request.
            app.logger.exception("event=security.actor_scoring_failed")

        if not failed:
            return response

        if status < 500 and (
            request.url_rule is None
            or request.endpoint == "static"
        ):
            # Nothing matched, so there is no route to report; both buckets were
            # already recorded above.
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
            _safe_method(),
            request.url_rule.rule if request.url_rule is not None else "<unmatched>",
            status, code, duration_ms, request.content_length,
        )
        return response
