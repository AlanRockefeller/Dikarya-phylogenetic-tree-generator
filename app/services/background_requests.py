"""Run a slow, read-only API handler off the request thread.

Alan 10/7/26 - POST /api/blast holds its request for 35-95 s and the iNat tree
preview sometimes for a minute. A phone that leaves the browser while it waits
(switching apps to copy a sequence is enough) has the connection dropped by
iOS: nginx logs a 499, the page reports "Load failed", and the server finishes
the work and throws the answer away. It also pins one of the 32 Gunicorn
request slots for the whole wait.

A caller that sends ``X-Dikarya-Background: 1`` now gets a 202 with a task id
at once; the handler runs on a thread in this process, its finished response
is stored in Redis, and the page polls ``GET /api/background/<id>``. A dropped
poll costs nothing -- the next one picks up where it left off.

Whenever this cannot run in the background (Redis unreachable, this process
already at its thread ceiling) ``start()`` returns None and the caller runs the
handler inline exactly as before, so a Redis outage degrades to the old
behaviour rather than breaking BLAST.

A web restart kills a running thread. Its task then disappears when the
"running" record ages out, and the page reports that rather than polling
forever; a BLAST rerun is cheap because blast_service caches finished searches.
"""

import json
import logging
import re
import secrets
import threading

from flask import copy_current_request_context, current_app, g, jsonify

logger = logging.getLogger(__name__)

BACKGROUND_HEADER = "X-Dikarya-Background"
_KEY = "dikarya:bgreq:{}"
# A finished result stays fetchable this long, so a phone that comes back to
# the tab much later still gets its answer.
RESULT_TTL_SECONDS = 3600
# How long a "running" marker survives without a result. Longer than the
# slowest handler (BLAST_REQUEST_MAX_WAIT_SECONDS is 240) so a live task never
# looks lost, short enough that one killed by a restart is reported promptly.
RUNNING_TTL_SECONDS = 330
# Per Gunicorn worker process. Each thread mostly sleeps on NCBI/iNat, but the
# ceiling keeps a burst from spawning unbounded threads; past it, requests run
# inline as they always did.
MAX_CONCURRENT_PER_PROCESS = 8
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,40}$")
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_PER_PROCESS)


def wants_background(req) -> bool:
    return req.headers.get(BACKGROUND_HEADER) == "1"


def _redis():
    from app.workers.queue import get_redis_connection
    return get_redis_connection()


def start(kind, handler, *args):
    """Run ``handler(*args)`` on a thread; return its task id, or None.

    ``handler`` must be the undecorated body of a view (no rate-limit
    decorator, which would count the request twice) and return anything a view
    may return. None means "run it inline instead".
    """
    if not _slots.acquire(blocking=False):
        logger.info("event=background_request.inline reason=busy kind=%s", kind)
        return None
    task_id = secrets.token_urlsafe(18)
    key = _KEY.format(task_id)
    try:
        conn = _redis()
        conn.set(key, json.dumps({"state": "running", "kind": kind}),
                 ex=RUNNING_TTL_SECONDS)
    except Exception as exc:
        _slots.release()
        logger.warning("event=background_request.inline reason=redis_unavailable "
                       "kind=%s error=%s", kind, type(exc).__name__)
        return None

    request_id = g.get("request_id")

    @copy_current_request_context
    def run():
        # The copied request context gets a fresh app context, so g is empty;
        # the request id is what ties this thread's log lines to the request.
        g.request_id = request_id
        try:
            try:
                rv = handler(*args)
            except Exception:
                logger.exception("event=http.unhandled_error where=background_%s", kind)
                rv = (jsonify({"status": "error", "error": "Internal server error",
                               "request_id": request_id}), 500)
            response = current_app.make_response(rv)
            record = {
                "state": "done",
                "kind": kind,
                "status": response.status_code,
                "body": response.get_data(as_text=True),
            }
            conn.set(key, json.dumps(record), ex=RESULT_TTL_SECONDS)
        except Exception:
            logger.exception("event=background_request.store_failed kind=%s", kind)
        finally:
            _slots.release()

    threading.Thread(target=run, name=f"bgreq-{kind}", daemon=True).start()
    return task_id


def fetch(task_id):
    """The stored record for ``task_id``, or None when unknown or expired."""
    if not _TASK_ID_RE.match(task_id or ""):
        return None
    raw = _redis().get(_KEY.format(task_id))
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return record if isinstance(record, dict) else None
