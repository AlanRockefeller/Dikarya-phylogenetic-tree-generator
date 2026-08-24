"""Cross-process census of open SSE streams.

Gunicorn runs `--workers 4 --threads 8` (GUNICORN_WORKERS / GUNICORN_THREADS in
app/config.py, which is what _capacity() below reads), so the whole site has
**32** request slots, and every open `/api/job/<id>/events` stream holds one for
its entire lifetime. When enough streams are open at once, ordinary requests get
no slot and nginx returns 504 after `proxy_read_timeout`.

The failure mode is subtler than global exhaustion, and the count alone will
not predict it. Gunicorn's gthread workers each pre-accept connections into
their own queue, so a worker whose threads are all parked on 30-minute streams
strands every connection it has accepted -- while the other three workers serve
traffic normally. That is why _pressure_threshold() keys off threads *per
worker* rather than the global total: raising the thread count raises the bar
for stranding one worker, it does not remove it.

On 2026-08-21 -- when this deployment still ran `--workers 4 --threads 2`, for
8 slots in total -- that produced 11 nginx 504s between 17:48 and 19:12, every
one of them exactly 300s (proxy_read_timeout), on endpoints as trivial as
/favicon.ico, at a global occupancy of 2-4 of those 8 slots. The decisive
evidence is that the 504'd `GET /tree` at 18:58:34 never appears in Gunicorn's
access log at all, while the same client's /favicon.ico one second later was
served 200: no worker ever accepted it. The thread count has since gone up; the
mechanism has not changed, only the number of streams it takes to reach it.

So treat this count as a pressure gauge, not a threshold. Enough streams on one
unlucky worker can time out requests while most of the global pool sits idle --
at 4x8 that takes 8 streams landing on the same worker, where at 4x2 it took 2.
Gunicorn's access log cannot show this on its own -- it only records requests that
*completed*, so a stranded connection leaves no trace there.

The registry is a Redis sorted set of stream ids scored by expiry, so a worker
that is SIGKILLed cannot leak a permanent count: its entries simply age out.
Every operation is best-effort -- observability must never take down the stream
it is observing.
"""

import logging
import time
import uuid

logger = logging.getLogger(__name__)

_REGISTRY_KEY = "sse:open_streams"

# Entries older than this are treated as dead. Comfortably longer than
# SSE_MAX_STREAM_SECONDS would ever leave a live stream unrefreshed, so a
# healthy stream is never miscounted as expired.
_ENTRY_TTL_SECONDS = 60 * 60 * 8


def _capacity():
    """Total concurrent request slots (Gunicorn workers x threads)."""
    from app.config import Config
    workers = int(getattr(Config, "GUNICORN_WORKERS", 4) or 4)
    threads = int(getattr(Config, "GUNICORN_THREADS", 8) or 8)
    return max(1, workers * threads)


def _pressure_threshold():
    """Stream count at which a single worker could already be saturated.

    Deliberately not "half the global pool": that is the assumption the
    2026-08-21 incident disproved, when 2 streams out of 8 slots timed out
    requests because both landed on the same worker. A worker stalls once its
    own thread pool is full, so the number that matters is threads-per-worker,
    independent of how many workers there are.
    """
    from app.config import Config
    return max(2, int(getattr(Config, "GUNICORN_THREADS", 8) or 8))


def open_stream(redis_conn, job_id):
    """Register a stream. Returns (token, open_count); count 0 if unavailable."""
    token = uuid.uuid4().hex
    now = time.time()
    try:
        redis_conn.zremrangebyscore(_REGISTRY_KEY, "-inf", now - _ENTRY_TTL_SECONDS)
        redis_conn.zadd(_REGISTRY_KEY, {token: now})
        count = int(redis_conn.zcard(_REGISTRY_KEY))
    except Exception:
        return token, 0

    capacity = _capacity()
    threshold = _pressure_threshold()
    # sse.opened is emitted unconditionally. It used to be the `else` of the
    # check below, so opens stopped being logged exactly during the pressure
    # episodes this registry exists to diagnose -- anything pairing sse.opened
    # with sse.closed undercounted precisely then. The pressure warning is an
    # additional record, not a replacement.
    logger.info("event=sse.opened SSE stream opened open_streams=%s", count)
    if count >= threshold:
        logger.warning(
            "event=sse.capacity_pressure open_streams=%s threshold=%s capacity=%s job=%s",
            count, threshold, capacity, job_id,
        )
    return token, count


def close_stream(redis_conn, token):
    """Deregister a stream. Returns the remaining count, or 0 if unavailable."""
    try:
        redis_conn.zrem(_REGISTRY_KEY, token)
        return int(redis_conn.zcard(_REGISTRY_KEY))
    except Exception:
        return 0
