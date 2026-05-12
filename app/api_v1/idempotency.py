"""Idempotency-Key support for POST endpoints.

Clients send `Idempotency-Key: <opaque-string>` on POST requests they want
to make safely retryable. The first response is cached in Redis for 24h
under (user_id, method, path, key); subsequent requests with the same key
return the cached response. If the same key is reused with a *different*
request body, we 409 to avoid silently accepting a divergent retry.

To prevent two concurrent retries from both executing the handler (which
would create duplicate jobs), the first request atomically reserves the
cache key with a short-lived "PENDING" placeholder using SET NX. A second
request that sees the placeholder gets 409 `in_flight` and is expected to
retry shortly. After the handler completes, the placeholder is replaced
with the real cached response on 2xx, or deleted on error (so the retry
can succeed).
"""
import functools
import hashlib
import json
import re

from flask import g, request

from app.api_v1.envelope import error_response

IDEM_TTL_SECONDS = 86400         # 24h for cached successful responses
IDEM_PENDING_TTL_SECONDS = 60    # placeholder lifetime while the handler runs
MAX_KEY_LEN = 200
_KEY_CHARSET_RE = re.compile(r"^[A-Za-z0-9_\-]{1,200}$")
_PENDING_PREFIX = "PENDING:"


def _redis():
    import redis as _r
    from app.config import Config
    return _r.from_url(Config.REDIS_URL)


def _key(user_id, method, path, idem_key):
    # Scoping by method + path stops a key reused across different endpoints
    # from colliding (e.g. POST /jobs vs POST /jobs/{id}/recompute).
    return f"idem:v1:{user_id}:{method}:{path}:{idem_key}"


def _hash_body(method, path, body_bytes):
    h = hashlib.sha256()
    h.update(method.encode("utf-8"))
    h.update(b"\0")
    h.update(path.encode("utf-8"))
    h.update(b"\0")
    h.update(body_bytes or b"")
    return h.hexdigest()


def _replay(cached, *, is_replay=True):
    """Build a Flask Response from a cached payload dict."""
    from flask import Response
    resp = Response(
        cached.get("body", "{}"),
        status=cached.get("status", 200),
        mimetype="application/json",
    )
    if is_replay:
        resp.headers["X-Idempotent-Replay"] = "true"
    # Preserve the original request id so callers can correlate the replay
    # back to the original handler invocation.
    original_rid = cached.get("request_id")
    if original_rid:
        resp.headers["X-Request-Id"] = original_rid
    return resp


def idempotent(fn):
    """Decorator: cache responses keyed by Idempotency-Key + (user, method, path).

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
                message=(
                    f"`Idempotency-Key` is {len(idem_key)} characters; the "
                    f"maximum is {MAX_KEY_LEN}."
                ),
                status=400,
            )
        if not _KEY_CHARSET_RE.match(idem_key):
            return error_response(
                code="bad_request",
                message=(
                    "`Idempotency-Key` must contain only letters, digits, "
                    "underscore, and hyphen (1..200 chars). UUIDs and "
                    "base64url strings work well."
                ),
                status=400,
            )

        user = getattr(g, "api_user", None)
        if user is None:
            # Should never happen if @require_api_token is applied first;
            # fail closed by skipping the cache rather than caching against
            # an anonymous bucket.
            return fn(*args, **kwargs)

        method = request.method
        path = request.path
        body_bytes = request.get_data(cache=True)
        body_hash = _hash_body(method, path, body_bytes)
        cache_key = _key(user.id, method, path, idem_key)

        try:
            r = _redis()
        except Exception:
            # If Redis is unreachable we skip the cache rather than 500. This
            # *does* mean a true concurrent retry could double-execute during
            # a Redis outage, but failing the request entirely would be worse
            # for callers; document this tradeoff in the API guide.
            return fn(*args, **kwargs)

        # Atomically reserve the slot. NX returns truthy on success.
        placeholder = f"{_PENDING_PREFIX}{body_hash}"
        try:
            reserved = r.set(cache_key, placeholder, nx=True, ex=IDEM_PENDING_TTL_SECONDS)
        except Exception:
            reserved = None

        if not reserved:
            # Either a placeholder (another request in flight) or a real
            # cached response is sitting there. Inspect it.
            try:
                existing_raw = r.get(cache_key)
            except Exception:
                existing_raw = None
            if existing_raw is None:
                # Race: it just expired. Run the handler without caching;
                # next retry will reserve normally.
                return fn(*args, **kwargs)
            existing = existing_raw.decode("utf-8") if isinstance(existing_raw, bytes) else existing_raw

            if existing.startswith(_PENDING_PREFIX):
                pending_hash = existing[len(_PENDING_PREFIX):]
                if pending_hash != body_hash:
                    return error_response(
                        code="conflict",
                        message=(
                            "This `Idempotency-Key` is already in use for a "
                            "different request body on the same endpoint."
                        ),
                        status=409,
                    )
                return error_response(
                    code="in_flight",
                    message=(
                        "A request with this `Idempotency-Key` is still "
                        "being processed. Retry in a few seconds; the "
                        "cached response will be returned once it completes."
                    ),
                    status=409,
                    details={"retry_after_seconds": 5},
                )

            # It's a real cached response.
            try:
                cached = json.loads(existing)
            except Exception:
                cached = None
            if not cached:
                return fn(*args, **kwargs)
            if cached.get("body_hash") != body_hash:
                return error_response(
                    code="conflict",
                    message=(
                        "This `Idempotency-Key` was previously used with a "
                        "different request body on the same endpoint. "
                        "Choose a fresh key for the new request."
                    ),
                    status=409,
                )
            return _replay(cached, is_replay=True)

        # We hold the reservation. Run the handler.
        try:
            response = fn(*args, **kwargs)
        except Exception:
            # Release the placeholder so a retry can succeed.
            try:
                r.delete(cache_key)
            except Exception:
                pass
            raise

        try:
            if 200 <= response.status_code < 300:
                payload = json.dumps({
                    "body_hash": body_hash,
                    "body": response.get_data(as_text=True),
                    "status": response.status_code,
                    "request_id": response.headers.get("X-Request-Id"),
                })
                r.setex(cache_key, IDEM_TTL_SECONDS, payload)
            else:
                # Don't lock the key for 24h on a 4xx/5xx; let the caller
                # fix their request and retry.
                r.delete(cache_key)
        except Exception:
            pass
        return response
    return wrapper
