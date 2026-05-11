"""Scaffold routes for /api/v1.

Phase 1 ships:
    GET  /api/v1/me      - basic account info, used by clients to verify token
    GET  /api/v1/tokens  - list the caller's own tokens (no secrets)

The job/tool endpoints land in Phase 2.
"""
from flask import g

from app.api_v1 import bp
from app.api_v1.auth import require_api_token
from app.api_v1.envelope import ok
from app.extensions import limiter
from app.api_v1.auth import api_token_key_func


@bp.route('/me', methods=['GET'])
@require_api_token(scope='account:read')
@limiter.limit("600 per minute", key_func=api_token_key_func)
def whoami():
    user = g.api_user
    return ok({
        "id": user.id,
        "email": user.email,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    })


@bp.route('/tokens', methods=['GET'])
@require_api_token(scope='account:read')
@limiter.limit("600 per minute", key_func=api_token_key_func)
def list_tokens():
    """List the caller's own tokens. Never returns secrets."""
    user = g.api_user
    tokens = sorted(user.api_tokens, key=lambda t: t.created_at, reverse=True)
    return ok([{
        "id": t.id,
        "name": t.name,
        "prefix": t.token_prefix,
        "scopes": list(t.scopes or []),
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
        "revoked_at": t.revoked_at.isoformat() if t.revoked_at else None,
    } for t in tokens])


@bp.route('/health', methods=['GET'])
def api_health():
    """Unauthenticated lightweight ping so clients can verify the API is up
    without spending a token request. Returns no sensitive info."""
    return ok({"status": "ok", "api_version": "v1"})
