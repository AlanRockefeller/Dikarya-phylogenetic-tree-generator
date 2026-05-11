"""Idempotency-Key support for POST endpoints.

Clients send `Idempotency-Key: <opaque-string>` on POST requests they want
to make safely retryable. The first response is cached in Redis for 24h
under (user_id, key); subsequent requests with the same key return the
cached response. If the same key is reused with a *different* request
body, we 409 to avoid silently accepting a divergent retry.
"""
import functools
import hashlib
import json

from flask import g, request

from app.api_v1.envelope import error_response

IDEM_TTL_SECONDS = 86400  # 24h
MAX_KEY_LEN = 200


def _redis():
    import redis as _r
    from app.config import Config
    return _r.from_url(Config.REDIS_URL)


def _key(user_id, idem_key):
    return f"idem:v1:{user_id}:{idem_key}"


def _hash_body(body_bytes):
    return hashlib.sha256(body_bytes or b"").hexdigest()


def idempotent(fn):
    """Decorator: cache responses keyed by Idempotency-Key + user.

    Only kicks in if the client sends the header; otherwise the route runs
    normally. Must be placed *after* require_api_token so `g.api_user` is set.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        idem_key = request.headers.get("Idempotency-Key", "").strip()
        if not idem_key:
            return fn(*args, **kwargs)
        if len(idem_key) > MAX_KEY_LEN:
            return error_response(
                code="bad_request",
                message=f"Idempotency-Key exceeds {MAX_KEY_LEN} chars.",
                status=400,
            )

        body_hash = _hash_body(request.get_data(cache=True))
        user = getattr(g, "api_user", None)
        if user is None:
            return fn(*args, **kwargs)
        cache_key = _key(user.id, idem_key)

        try:
            r = _redis()
            cached_raw = r.get(cache_key)
        except Exception:
            # If Redis is unreachable, skip the cache rather than 500.
            return fn(*args, **kwargs)

        if cached_raw:
            try:
                cached = json.loads(cached_raw)
            except Exception:
                cached = None
            if cached:
                if cached.get("body_hash") != body_hash:
                    return error_response(
                        code="conflict",
                        message="Idempotency-Key reused with a different request body.",
                        status=409,
                    )
                # Replay the saved response.
                from flask import Response
                resp = Response(
                    cached.get("body", "{}"),
                    status=cached.get("status", 200),
                    mimetype="application/json",
                )
                resp.headers["X-Idempotent-Replay"] = "true"
                return resp

        # Run the handler, then cache its (body, status).
        response = fn(*args, **kwargs)
        try:
            # Only cache successful 2xx responses; an error shouldn't lock
            # the key for 24h against a retry that might succeed.
            if 200 <= response.status_code < 300:
                payload = json.dumps({
                    "body_hash": body_hash,
                    "body": response.get_data(as_text=True),
                    "status": response.status_code,
                })
                r.setex(cache_key, IDEM_TTL_SECONDS, payload)
        except Exception:
            pass
        return response
    return wrapper
