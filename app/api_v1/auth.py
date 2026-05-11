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
    """Pull the bearer token from the Authorization header."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[len("Bearer "):].strip()
    if not token.startswith(TOKEN_PREFIX):
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

            # Update last_used_at lazily -- once per minute is plenty.
            now = datetime.utcnow()
            if token.last_used_at is None or (now - token.last_used_at).total_seconds() > 60:
                token.last_used_at = now
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()

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
