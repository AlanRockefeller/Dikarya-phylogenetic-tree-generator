"""Concurrency and duplicate-billing guards for Claude tree reviews.

Two things are being protected here. The global slot ceiling keeps eight users
from occupying every Gunicorn request slot with a 90-second model call, and the
per-fingerprint lock keeps one user's double click from paying for the same
review twice. Both live in Redis, and both used to fail in ways that were only
visible as the feature refusing to work.
"""

import inspect
import time
from unittest.mock import patch

import pytest
from flask import Flask

from app.api import routes
from app.config import Config
from app.services import tree_analysis_service as service


JOB_ID = "12345678-1234-4234-8234-123456789abc"
SLOT_KEY = service.SLOT_REGISTRY_KEY


class _FakeRedis:
    """Enough Redis for the sorted-set registry and the SET NX lock.

    Keys carry their own expiry so a test can step time forward and watch a
    stale lock release itself, which is the recovery path after a worker is
    killed mid-review.
    """

    def __init__(self, now=None):
        # Real seconds by default: the registry is scored with time.time(), so a
        # fixed fake clock would put every seeded entry outside the live window.
        self.now = time.time() if now is None else now
        self.zsets = {}
        self.values = {}
        self.expiries = {}

    # --- clock -----------------------------------------------------------
    def advance(self, seconds):
        self.now += seconds
        for key, expires_at in list(self.expiries.items()):
            if expires_at is not None and expires_at <= self.now:
                self.expiries.pop(key, None)
                self.values.pop(key, None)
                self.zsets.pop(key, None)

    # --- strings ---------------------------------------------------------
    def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.expiries[key] = self.now + ex if ex else None
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    def eval(self, script, numkeys, key, arg):
        # The only script this module runs is delete-if-owned.
        assert "del" in script
        if self.values.get(key) == arg:
            return self.delete(key)
        return 0

    # --- sorted sets -----------------------------------------------------
    def zadd(self, key, mapping):
        entries = self.zsets.setdefault(key, {})
        entries.update(mapping)
        return len(mapping)

    def zremrangebyscore(self, key, minimum, maximum):
        entries = self.zsets.get(key, {})
        low = float("-inf") if minimum == "-inf" else float(minimum)
        high = float("inf") if maximum == "+inf" else float(maximum)
        doomed = [member for member, score in entries.items() if low <= score <= high]
        for member in doomed:
            entries.pop(member)
        return len(doomed)

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def zrem(self, key, member):
        return int(self.zsets.get(key, {}).pop(member, None) is not None)

    def expire(self, key, seconds):
        self.expiries[key] = self.now + seconds
        return 1

    def pipeline(self):
        return _FakePipeline(self)


class _FakePipeline:
    """Queues calls and replays them in order, like redis-py's pipeline."""

    def __init__(self, client):
        self.client = client
        self.queued = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.queued.append((name, args, kwargs))
            return self
        return queue

    def execute(self):
        results = []
        for name, args, kwargs in self.queued:
            results.append(getattr(self.client, name)(*args, **kwargs))
        self.queued = []
        return results


def _with_redis(redis):
    return patch("app.workers.queue.get_redis_connection", return_value=redis)


# ---------------------------------------------------------------------------
# Global concurrency slots
# ---------------------------------------------------------------------------

def test_stale_slots_are_cleared_before_the_ceiling_is_checked():
    # A worker killed mid-review leaves its entry behind. Under the old shared
    # counter that leak survived until a 900 s expiry the *rejected* requests
    # kept pushing forward; here it is dropped by its own age.
    redis = _FakeRedis()
    ttl = int(Config.CLAUDE_REVIEW_TIMEOUT_SECONDS) + service.SLOT_GRACE_SECONDS
    redis.zsets[SLOT_KEY] = {
        "abandoned": redis.now - ttl - 60,
        "live": redis.now - 5,
    }

    with patch.object(Config, "CLAUDE_REVIEW_MAX_CONCURRENT", 2), _with_redis(redis):
        slot = service._acquire_slot()

    assert slot is not None
    assert "abandoned" not in redis.zsets[SLOT_KEY]
    assert set(redis.zsets[SLOT_KEY]) == {"live", slot.token}


def test_a_rejected_request_leaves_the_registry_exactly_as_it_found_it():
    redis = _FakeRedis()
    held = {"first": redis.now - 5, "second": redis.now - 3}
    redis.zsets[SLOT_KEY] = dict(held)

    with (
        patch.object(Config, "CLAUDE_REVIEW_MAX_CONCURRENT", 2),
        _with_redis(redis),
        pytest.raises(service.TreeAnalysisUnavailable, match="other trees"),
    ):
        service._acquire_slot()

    # No leftover token, and the two live entries keep their original scores, so
    # a stream of rejections cannot postpone their expiry indefinitely.
    assert redis.zsets[SLOT_KEY] == held


def test_releasing_a_slot_removes_only_that_request():
    redis = _FakeRedis()
    redis.zsets[SLOT_KEY] = {"someone-else": redis.now - 1}

    with patch.object(Config, "CLAUDE_REVIEW_MAX_CONCURRENT", 4), _with_redis(redis):
        slot = service._acquire_slot()
        slot.release()

    assert list(redis.zsets[SLOT_KEY]) == ["someone-else"]


def test_slots_are_unlimited_rather_than_blocked_when_redis_is_down():
    with patch(
        "app.workers.queue.get_redis_connection",
        side_effect=ConnectionError("redis down"),
    ):
        assert service._acquire_slot() is None


# ---------------------------------------------------------------------------
# Per-fingerprint duplicate guard
# ---------------------------------------------------------------------------

def test_a_second_request_for_the_same_fingerprint_is_refused():
    redis = _FakeRedis()

    with _with_redis(redis):
        first = service._acquire_fingerprint_lock("abc123")
        with pytest.raises(service.TreeAnalysisInProgress) as caught:
            service._acquire_fingerprint_lock("abc123")

    assert first is not None
    assert caught.value.retry_after_seconds > 0
    # A different tree is unaffected.
    with _with_redis(redis):
        assert service._acquire_fingerprint_lock("different") is not None


def test_releasing_the_lock_lets_the_next_request_through():
    redis = _FakeRedis()

    with _with_redis(redis):
        lock = service._acquire_fingerprint_lock("abc123")
        lock.release()
        assert service._acquire_fingerprint_lock("abc123") is not None


def test_a_lock_only_releases_for_its_owner():
    redis = _FakeRedis()

    with _with_redis(redis):
        lock = service._acquire_fingerprint_lock("abc123")
    stale = service._FingerprintLock(key=lock.key, token="someone-elses", client=redis)
    stale.release()

    assert redis.get(lock.key) == lock.token


def test_an_abandoned_lock_expires_so_the_review_can_be_retried():
    redis = _FakeRedis()

    with _with_redis(redis):
        service._acquire_fingerprint_lock("abc123")  # never released: worker killed
        redis.advance(int(Config.CLAUDE_REVIEW_TIMEOUT_SECONDS) + service.SLOT_GRACE_SECONDS + 1)
        assert service._acquire_fingerprint_lock("abc123") is not None


def test_duplicate_guard_degrades_rather_than_blocking_when_redis_is_down():
    with patch(
        "app.workers.queue.get_redis_connection",
        side_effect=ConnectionError("redis down"),
    ):
        assert service._acquire_fingerprint_lock("abc123") is None


# ---------------------------------------------------------------------------
# review_job wiring
# ---------------------------------------------------------------------------

def _review_result():
    return {
        "review": {"overall_rating": "usable"},
        "model": "claude-opus-5",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def test_a_duplicate_review_spends_neither_a_slot_nor_a_daily_reservation(tmp_path):
    redis = _FakeRedis()

    with (
        _with_redis(redis),
        patch.object(service, "is_configured", return_value=True),
        patch.object(service, "build_context", return_value={"tree": {}}),
        patch.object(service, "fingerprint", return_value="same-numbers"),
        patch.object(service, "load_cached_review", return_value=None),
        patch.object(service, "_reserve_daily_review") as reserve,
        patch.object(service, "_acquire_slot") as acquire_slot,
        patch.object(service, "_call_claude") as call,
    ):
        # The first request is still out; its lock is held.
        service._acquire_fingerprint_lock("same-numbers")

        with pytest.raises(service.TreeAnalysisInProgress):
            service.review_job(tmp_path)

    reserve.assert_not_called()
    acquire_slot.assert_not_called()
    call.assert_not_called()


def test_a_completed_review_releases_its_lock_and_its_slot(tmp_path):
    redis = _FakeRedis()

    with (
        _with_redis(redis),
        patch.object(service, "is_configured", return_value=True),
        patch.object(service, "build_context", return_value={"tree": {}}),
        patch.object(service, "fingerprint", return_value="same-numbers"),
        patch.object(service, "load_cached_review", return_value=None),
        patch.object(service, "_reserve_daily_review"),
        patch.object(service, "_call_claude", return_value=_review_result()),
        patch.object(service, "_store_review"),
        patch.object(service, "_append_usage_log"),
        patch.object(Config, "CLAUDE_REVIEW_MAX_CONCURRENT", 2),
    ):
        payload = service.review_job(tmp_path)

        assert payload["schema_version"] == service.REVIEW_SCHEMA_VERSION
        assert redis.zcard(SLOT_KEY) == 0
        # The next identical request is free to run.
        assert service._acquire_fingerprint_lock("same-numbers") is not None


def test_a_failed_review_still_releases_its_lock(tmp_path):
    redis = _FakeRedis()

    with (
        _with_redis(redis),
        patch.object(service, "is_configured", return_value=True),
        patch.object(service, "build_context", return_value={"tree": {}}),
        patch.object(service, "fingerprint", return_value="same-numbers"),
        patch.object(service, "load_cached_review", return_value=None),
        patch.object(service, "_reserve_daily_review"),
        patch.object(
            service, "_call_claude",
            side_effect=service.TreeAnalysisUpstreamError("Claude returned a malformed review."),
        ),
    ):
        with pytest.raises(service.TreeAnalysisUpstreamError):
            service.review_job(tmp_path)

        assert service._acquire_fingerprint_lock("same-numbers") is not None


def test_a_cached_review_never_touches_the_lock(tmp_path):
    redis = _FakeRedis()
    cached = {"cached": True, "review": {"overall_rating": "usable"}}

    with (
        _with_redis(redis),
        patch.object(service, "is_configured", return_value=True),
        patch.object(service, "build_context", return_value={"tree": {}}),
        patch.object(service, "fingerprint", return_value="same-numbers"),
        patch.object(service, "load_cached_review", return_value=cached),
    ):
        assert service.review_job(tmp_path) is cached
        assert not redis.values


# ---------------------------------------------------------------------------
# Endpoint statuses
# ---------------------------------------------------------------------------

def _call_endpoint(tmp_path, error):
    (tmp_path / JOB_ID).mkdir(exist_ok=True)
    app = Flask(__name__)
    original_view = inspect.unwrap(routes.claude_review)

    with (
        app.test_request_context(method="POST", json={}),
        patch.object(Config, "JOB_DIR", tmp_path),
        patch.object(routes, "check_job_access", return_value=(None, None, 200)),
        patch.object(service, "is_configured", return_value=True),
        patch.object(service, "review_job", side_effect=error),
    ):
        return original_view(JOB_ID)


def test_endpoint_answers_409_with_retry_after_for_a_duplicate(tmp_path):
    response, status = _call_endpoint(tmp_path, service.TreeAnalysisInProgress(270))

    assert status == 409
    assert response.headers["Retry-After"] == "270"
    assert "already running" in response.get_json()["error"]


def test_endpoint_answers_502_when_claude_returns_something_unusable(tmp_path):
    response, status = _call_endpoint(
        tmp_path, service.TreeAnalysisUpstreamError("Claude returned a malformed review.")
    )

    # The browser's request was fine; the model's answer was not.
    assert status == 502


def test_endpoint_still_answers_400_when_the_job_has_nothing_to_review(tmp_path):
    response, status = _call_endpoint(
        tmp_path, service.TreeAnalysisError("This job has no tree file.")
    )

    assert status == 400


def test_endpoint_still_answers_503_when_the_feature_is_out_of_capacity(tmp_path):
    _, status = _call_endpoint(tmp_path, service.TreeAnalysisUnavailable("busy"))

    assert status == 503


def test_slot_ttl_covers_the_configured_timeout():
    # The lock and the slot must outlive the slowest call they protect,
    # otherwise a second request starts while the first is still billing.
    assert service._slot_ttl_seconds() > int(Config.CLAUDE_REVIEW_TIMEOUT_SECONDS)
    assert service._slot_ttl_seconds() < 3600


def test_time_is_not_frozen_by_the_fake_clock():
    # Guards the fixture itself: the registry scores real seconds.
    redis = _FakeRedis(now=time.time())
    with patch.object(Config, "CLAUDE_REVIEW_MAX_CONCURRENT", 1), _with_redis(redis):
        slot = service._acquire_slot()
    assert slot is not None
    assert redis.zsets[SLOT_KEY][slot.token] == pytest.approx(time.time(), abs=5)
