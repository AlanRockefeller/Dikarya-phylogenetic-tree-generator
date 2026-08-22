"""Consistent JSON response envelope for /api/v1.

Success:
    {"data": <resource>, "meta": {...optional...}}

Error:
    {"error": {"code": "...", "message": "...", "request_id": "...", "details": {...}}}
"""
import uuid
from flask import g, jsonify, request


def _request_id():
    rid = getattr(g, "request_id", None)
    if not rid:
        rid = uuid.uuid4().hex[:12]
        g.request_id = rid
    return rid


def ok(data, *, status=200, meta=None):
    body = {"data": data}
    if meta is not None:
        body["meta"] = meta
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = _request_id()
    return response


def error_response(*, code, message, status, details=None):
    """Build a structured error response. Always sets X-Request-Id."""
    err = {
        "code": code,
        "message": message,
        "request_id": _request_id(),
    }
    if details is not None:
        err["details"] = details
    response = jsonify({"error": err})
    response.status_code = status
    response.headers["X-Request-Id"] = err["request_id"]
    return response


def server_error(exc, *, where=""):
    """Generic 500 -- log the full traceback, return only request_id to caller."""
    from flask import current_app
    current_app.logger.exception(
        "event=http.unhandled_error where=%s exception=%s",
        where or "api_v1", type(exc).__name__,
    )
    return error_response(
        code="internal_error",
        message="Internal server error.",
        status=500,
    )


def paginate_query(query, *, default_per_page=50, max_per_page=100):
    """Apply ?page= / ?per_page= to a SQLAlchemy query and return (items, meta).

    Always clamps per_page to [1, max_per_page] and page to >= 1.
    """
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.args.get("per_page", default_per_page))
    except (TypeError, ValueError):
        per_page = default_per_page
    per_page = max(1, min(max_per_page, per_page))

    total = query.count()
    items = query.limit(per_page).offset((page - 1) * per_page).all()
    meta = {
        "page": page,
        "per_page": per_page,
        "total": total,
        "has_next": (page * per_page) < total,
    }
    return items, meta
