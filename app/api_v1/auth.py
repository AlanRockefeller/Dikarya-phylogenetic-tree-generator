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
import secrets
from datetime import datetime

from flask import g, request

from app.extensions import db
from app.models import ApiToken
from app.api_v1.envelope import error_response

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
                return error_response(
                    code="missing_token",
                    message="Provide an API token via the Authorization header: "
                            "Authorization: Bearer dikarya_...",
                    status=401,
                )
            token = _lookup_token(plaintext)
            if token is None or not token.is_active:
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
                except Exception:
                    # Best-effort: a failure here must never block auth.
                    pass

            return fn(*args, **kwargs)
        return wrapper
    return decorator


def api_token_key_func():
    """flask-limiter key function: bucket per token (not per IP)."""
    token = getattr(g, "api_token", None)
    if token is not None:
        return f"api_token:{token.id}"
    # Fall back to IP for any pre-auth request (shouldn't normally hit limiter).
    from flask_limiter.util import get_remote_address
    return get_remote_address()
