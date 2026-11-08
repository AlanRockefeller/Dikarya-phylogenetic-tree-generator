"""Shared EUtils pacing and Retry-After cooldowns, independent of web workers."""
import logging
import math
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import redis
from flask import current_app, has_app_context
from app.config import Config

logger = logging.getLogger(__name__)
KEY_PREFIX = "dikarya:ncbi:"
INTERVAL_MS = 350
_client = None
_lock = threading.Lock()
_local_next = 0.0
_last_warning = 0.0
_SLOT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local slot = math.max(now, tonumber(redis.call('GET', KEYS[1]) or 0), tonumber(redis.call('GET', KEYS[2]) or 0))
local wait = slot - now
if wait >= tonumber(ARGV[2]) then return -wait - 1 end
if wait > 0 then return wait end
redis.call('SET', KEYS[1], slot + tonumber(ARGV[1]), 'PX', math.max(1000, wait + tonumber(ARGV[1]) * 2))
return wait
"""
_COOLDOWN = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local until_at = math.max(now + tonumber(ARGV[1]), tonumber(redis.call('GET', KEYS[1]) or 0))
redis.call('SET', KEYS[1], until_at, 'PX', math.max(1, until_at - now))
return 1
"""


def is_eutils(url):
    return urlsplit(str(url)).hostname == "eutils.ncbi.nlm.nih.gov"


def connection():
    global _client
    if _client is None:
        url = current_app.config.get("REDIS_URL", Config.REDIS_URL) if has_app_context() else Config.REDIS_URL
        _client = redis.from_url(url, socket_connect_timeout=0.1, socket_timeout=0.1)
    return _client


# The longest shared cooldown a Retry-After can set (retry_after_seconds caps it).
MAX_COOLDOWN_SECONDS = 3600.0


def retry_after_seconds(value, default=1.0):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(str(value)).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            seconds = default
    return max(0.0, min(MAX_COOLDOWN_SECONDS, seconds)) if math.isfinite(seconds) else default


def _warn(exc):
    global _last_warning
    now = time.monotonic()
    if now - _last_warning > 300:
        _last_warning = now
        logger.warning("event=degraded.ncbi_pacing_unavailable DEGRADED shared EUtils pacing unavailable; using local pacing error=%s",
                       type(exc).__name__)


def wait_for_slot(url, *, deadline=None):
    """Reserve before every attempt; a rejected budget never consumes a slot."""
    global _local_next
    if not is_eutils(url):
        return
    deadline = deadline if deadline is not None else time.monotonic() + 60
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("NCBI request budget exhausted while waiting for a request slot")
        try:
            millis = int(connection().eval(_SLOT, 2, KEY_PREFIX + "next", KEY_PREFIX + "cooldown",
                                           INTERVAL_MS, max(1, int(remaining * 1000))))
            if millis < 0:
                raise TimeoutError("NCBI requests are busy; the request budget cannot absorb the shared cooldown")
            if millis == 0:
                return
            # Recheck after sleeping: another process may have received a 429
            # while we waited. No slot is committed until it can be used now.
            time.sleep(millis / 1000.0)
        except redis.RedisError as exc:
            _warn(exc)
            with _lock:
                now = time.monotonic()
                wait = max(0, _local_next - now)
                if wait >= remaining:
                    raise TimeoutError("NCBI request budget exhausted during local pacing") from exc
                _local_next = now + wait + INTERVAL_MS / 1000.0
            if wait:
                time.sleep(wait)
            return


def note_cooldown(url, retry_after):
    global _local_next
    if not is_eutils(url):
        return
    seconds = retry_after_seconds(retry_after)
    with _lock:
        _local_next = max(_local_next, time.monotonic() + seconds)
    try:
        connection().eval(_COOLDOWN, 1, KEY_PREFIX + "cooldown", max(1, int(seconds * 1000)))
    except redis.RedisError as exc:
        _warn(exc)
    logger.info("event=ncbi.cooldown seconds=%.3f", seconds)
