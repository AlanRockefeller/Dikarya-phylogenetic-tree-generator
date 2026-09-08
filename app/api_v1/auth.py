"""Token authentication for /api/v1.

Tokens are bearer credentials of the form `dikarya_<43-char-base64url>`.
We store SHA-256 hashes only; the plaintext secret is shown to the user
exactly once at creation time.

Available scopes:
    jobs:read     - list/get jobs, read events, download files & logs
    jobs:write    - create, recompute, mutate, delete jobs
    tools:read    - BLAST, GenBank, MycoMap, iNaturalist lookups
    account:read  - /me, list own tokens
"""
import functools
import hashlib
import hmac
import logging
import secrets
from datetime import datetime

from flask import g, request

from app.extensions import db
from app.models import ApiToken
from app.api_v1.envelope import error_response
from app.services.log_context import log_degradation_rate_limited

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "dikarya_"
TOKEN_SECRET_BYTES = 32  # → ~43 base64url chars
TOKEN_PREFIX_VISIBLE = 16  # how many chars of the full token we keep visible

ALL_SCOPES = ("jobs:read", "jobs:write", "tools:read", "account:read")


def generate_token():
    """Mint a new token. Returns (plaintext, hash, visible_prefix).

    The plaintext is shown to the user once; only the hash is persisted.
    """
    secret = secrets.token_urlsafe(TOKEN_SECRET_BYTES)
    plaintext = f"{TOKEN_PREFIX}{secret}"
    token_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    return plaintext, token_hash, plaintext[:TOKEN_PREFIX_VISIBLE]


def hash_token(plaintext):
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _extract_token():
    """Pull the bearer token from the Authorization header.

    Per RFC 7235 the auth-scheme is case-insensitive ("Bearer", "bearer",
    "BEARER" all mean the same thing). The credential value that follows
    is *not* case-folded -- we leave it exactly as the client sent it so
    the hash compare downstream is byte-accurate.
    """
    header = request.headers.get("Authorization", "")
    if not header:
        return None
    # Split into at most two parts so a token containing whitespace would
    # be rejected explicitly rather than silently truncated.
    parts = header.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, credentials = parts
    if scheme.lower() != "bearer":
        return None
    token = credentials.strip()
    # Reject prefix-only / empty-secret strings before they cost us a DB
    # lookup. A well-formed token is the prefix plus at least a few chars
    # of base64url secret.
    if not token.startswith(TOKEN_PREFIX) or len(token) <= len(TOKEN_PREFIX):
        return None
    return token


def _lookup_token(plaintext):
    """Resolve a plaintext token to an ApiToken row via constant-time hash compare."""
    candidate_hash = hash_token(plaintext)
    # SQLAlchemy index lookup -- the index is on token_hash. The hash is
    # already a fixed-length hex digest, so the lookup is effectively O(1)
    # and timing-safe (no early-exit string compare on the secret itself).
    token = ApiToken.query.filter_by(token_hash=candidate_hash).first()
    if token is None:
        return None
    # Belt-and-suspenders constant-time compare in case anything later
    # introduces a partial-match index.
    if not hmac.compare_digest(token.token_hash, candidate_hash):
        return None
    return token


# ---------------------------------------------------------------------------
# Failed-authentication throttle (per client IP)
# ---------------------------------------------------------------------------
#
# Every @require_api_token endpoint carries its own token-keyed limit, but that
# decorator only runs once a token has been resolved -- a request with a bad or
# absent token is rejected here first and never reaches it. That left the
# unauthenticated path unmetered: an attacker could spend a Gunicorn slot, a
# SHA-256 hash and an indexed SELECT per request, for free, forever.
#
# This is deliberately NOT a brute-force defence. The secrets are 256 bits of
# `secrets.token_urlsafe`; nobody is guessing one. The point is that repeated
# *failed* authentication stops being free work, while a caller holding a valid
# token is untouched -- successful requests never hit this counter, so the
# existing per-token limits remain the only thing a legitimate client sees.
FAILED_AUTH_LIMITS = ("60 per minute", "600 per hour")
_FAILED_AUTH_NAMESPACE = "api_v1_auth_fail"
_failed_auth_items = None

# Token-shaped credentials require one indexed hash lookup before we can know
# whether they are valid. Bound that work with a deliberately much higher IP
# ceiling; the ordinary failed-auth bucket remains lower and, importantly, is
# never consulted for a successfully authenticated request.
PRE_AUTH_LOOKUP_LIMITS = ("600 per minute", "6000 per hour")
_PRE_AUTH_LOOKUP_NAMESPACE = "api_v1_auth_lookup"
_pre_auth_lookup_items = None


def _failed_auth_limit_items():
    global _failed_auth_items
    if _failed_auth_items is None:
        from limits import parse
        _failed_auth_items = tuple(parse(text) for text in FAILED_AUTH_LIMITS)
    return _failed_auth_items


def _pre_auth_lookup_limit_items():
    global _pre_auth_lookup_items
    if _pre_auth_lookup_items is None:
        from limits import parse
        _pre_auth_lookup_items = tuple(
            parse(text) for text in PRE_AUTH_LOOKUP_LIMITS
        )
    return _pre_auth_lookup_items


def _client_address():
    from flask_limiter.util import get_remote_address
    return get_remote_address() or "unknown"


def _failed_auth_over_budget():
    """True once this client has spent its failed-authentication budget.

    Applied only after authentication has failed. A valid credential from a
    shared address must not inherit another caller's failed-auth penalty. Any
    storage problem fails open: a broken limiter backend must not lock out API
    clients.
    """
    from app.extensions import limiter
    try:
        strategy = limiter.limiter
        address = _client_address()
        return any(
            not strategy.test(item, _FAILED_AUTH_NAMESPACE, address)
            for item in _failed_auth_limit_items()
        )
    except Exception:
        return False


def _record_failed_auth():
    """Charge one failed authentication against the caller's IP budget."""
    from app.extensions import limiter
    try:
        strategy = limiter.limiter
        address = _client_address()
        for item in _failed_auth_limit_items():
            strategy.hit(item, _FAILED_AUTH_NAMESPACE, address)
    except Exception:
        pass


def _pre_auth_lookup_allowed():
    """Charge the high-volume lookup bucket, failing open on backend errors.

    Every configured window is charged before the results are combined. ``all()``
    over a generator short-circuits, so when the per-minute bucket refused, the
    per-hour bucket never saw the request at all -- a caller held just under the
    minute limit could keep hammering indefinitely without the hourly ceiling
    ever filling up.
    """
    from app.extensions import limiter
    try:
        strategy = limiter.limiter
        address = _client_address()
        results = [
            strategy.hit(item, _PRE_AUTH_LOOKUP_NAMESPACE, address)
            for item in _pre_auth_lookup_limit_items()
        ]
        return all(results)
    except Exception as exc:
        from app.services.log_context import log_degradation_rate_limited
        log_degradation_rate_limited(
            logger, "pre_auth_lookup_limiter_unavailable",
            "API token lookups ran without pre-auth rate limiting because the "
            "limiter backend failed",
            exception=type(exc).__name__,
        )
        return True


def require_api_token(scope=None):
    """Decorator: require a valid, unrevoked API token with the given scope.

    Usage:
        @bp.route('/jobs', methods=['POST'])
        @require_api_token(scope='jobs:write')
        def create_job_v1(): ...

    On success, populates `g.api_user` and `g.api_token`.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            plaintext = _extract_token()
            if not plaintext:
                if _failed_auth_over_budget():
                    return _failed_auth_limited_response()
                _record_failed_auth()
                return error_response(
                    code="missing_token",
                    message="Provide an API token via the Authorization header: "
                            "Authorization: Bearer dikarya_...",
                    status=401,
                )
            if not _pre_auth_lookup_allowed():
                return error_response(
                    code="too_many_auth_attempts",
                    message=(
                        "Too many authentication attempts from this address. "
                        "Wait a minute and try again."
                    ),
                    status=429,
                )
            token = _lookup_token(plaintext)
            if token is None or not token.is_active:
                # A syntactically valid bearer credential must be resolved
                # before consulting the IP failure bucket; otherwise failures
                # from another client behind the same NAT block valid tokens.
                if _failed_auth_over_budget():
                    return _failed_auth_limited_response()
                _record_failed_auth()
                return error_response(
                    code="invalid_token",
                    message="The provided API token is invalid or has been revoked.",
                    status=401,
                )
            if scope and not token.has_scope(scope):
                return error_response(
                    code="insufficient_scope",
                    message=f"This token does not have the required scope: {scope}",
                    status=403,
                    details={"required_scope": scope, "granted_scopes": list(token.scopes or [])},
                )

            g.api_token = token
            g.api_user = token.user

            # Update last_used_at lazily -- once per minute is plenty. We
            # run this through a fresh engine-level connection rather than
            # the request-bound session so we don't flush any other pending
            # ORM changes as a side effect of an auth bookkeeping write.
            now = datetime.utcnow()
            if token.last_used_at is None or (now - token.last_used_at).total_seconds() > 60:
                try:
                    with db.engine.begin() as conn:
                        conn.execute(
                            ApiToken.__table__.update()
                            .where(ApiToken.id == token.id)
                            .values(last_used_at=now)
                        )
                    # Keep the in-memory object in sync so downstream code
                    # sees the new timestamp without a re-fetch.
                    token.last_used_at = now
                except Exception as exc:
                    # Best-effort: a failure here must never block auth. It is
                    # still worth knowing about -- a token whose last_used_at
                    # silently stops advancing makes `flask api-tokens` report
                    # active tokens as unused -- so say so, rate limited, since
                    # whatever broke the write will break it on every API call.
                    log_degradation_rate_limited(
                        logger, "api_token_last_used_not_recorded",
                        "API request served without recording the token's last_used_at",
                        exception=type(exc).__name__,
                    )

            return fn(*args, **kwargs)
        return wrapper
    return decorator


def _failed_auth_limited_response():
    return error_response(
        code="too_many_failed_auth",
        message=(
            "Too many failed authentication attempts from this address. "
            "Wait a minute and try again. Valid API tokens remain usable."
        ),
        status=429,
    )


def api_token_key_func():
    """flask-limiter key function: bucket per token (not per IP)."""
    token = getattr(g, "api_token", None)
    if token is not None:
        return f"api_token:{token.id}"
    # Fall back to IP for any pre-auth request (shouldn't normally hit limiter).
    from flask_limiter.util import get_remote_address
    return get_remote_address()
