"""Daily spending guard coverage for Claude tree reviews."""

import json
import inspect
from unittest.mock import patch

import pytest
from flask import Flask

from app.api import routes
from app.config import Config
from app.services import tree_analysis_service as service


JOB_ID = "12345678-1234-4234-8234-123456789abc"


class _FakeRedis:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.expiries = {}

    def exists(self, key):
        return int(key in self.values)

    def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = int(value)
        self.expiries[key] = ex
        return True

    def incr(self, key):
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def decr(self, key):
        self.values[key] -= 1
        return self.values[key]


def test_daily_review_limit_seeds_today_from_usage_log(tmp_path):
    now = 1_787_274_000.0
    day_start = int(now // 86400) * 86400
    usage_log = tmp_path / "claude_reviews.jsonl"
    usage_log.write_text(
        "\n".join(
            [
                json.dumps({"ts": day_start - 1}),
                json.dumps({"ts": day_start + 10}),
                "not json",
                json.dumps({"ts": day_start + 20}),
            ]
        )
    )
    redis = _FakeRedis()

    with (
        patch.object(Config, "CLAUDE_REVIEW_MAX_DAILY", 25),
        patch.object(service, "USAGE_LOG_PATH", usage_log),
        patch.object(service.time, "time", return_value=now),
        patch("app.workers.queue.get_redis_connection", return_value=redis),
    ):
        service._reserve_daily_review()

    key = "dikarya:claude_review:daily:2026-08-21"
    assert redis.values[key] == 3
    assert redis.expiries[key] > 300


def test_daily_review_limit_rejects_and_does_not_inflate_counter():
    now = 1_787_274_000.0
    key = "dikarya:claude_review:daily:2026-08-21"
    redis = _FakeRedis({key: 25})

    with (
        patch.object(Config, "CLAUDE_REVIEW_MAX_DAILY", 25),
        patch.object(service.time, "time", return_value=now),
        patch("app.workers.queue.get_redis_connection", return_value=redis),
        pytest.raises(service.TreeAnalysisDailyLimit) as caught,
    ):
        service._reserve_daily_review()

    assert redis.values[key] == 25
    assert caught.value.retry_after_seconds > 0


def test_daily_review_limit_fails_closed_when_redis_is_unavailable():
    with (
        patch.object(Config, "CLAUDE_REVIEW_MAX_DAILY", 25),
        patch(
            "app.workers.queue.get_redis_connection",
            side_effect=ConnectionError("redis down"),
        ),
        pytest.raises(service.TreeAnalysisUnavailable, match="could not be checked"),
    ):
        service._reserve_daily_review()


def test_cached_review_does_not_consume_daily_allowance(tmp_path):
    cached = {"cached": True, "review": {"overall_rating": "usable"}}

    with (
        patch.object(service, "is_configured", return_value=True),
        patch.object(service, "build_context", return_value={"tree": {}}),
        patch.object(service, "fingerprint", return_value="fingerprint"),
        patch.object(service, "load_cached_review", return_value=cached),
        patch.object(service, "_reserve_daily_review") as reserve,
        patch.object(service, "_acquire_slot") as acquire_slot,
    ):
        result = service.review_job(tmp_path)

    assert result is cached
    reserve.assert_not_called()
    acquire_slot.assert_not_called()


def test_review_endpoint_returns_429_and_retry_after_at_daily_limit(tmp_path):
    (tmp_path / JOB_ID).mkdir()
    app = Flask(__name__)
    original_view = inspect.unwrap(routes.claude_review)

    with (
        app.test_request_context(method="POST", json={}),
        patch.object(Config, "JOB_DIR", tmp_path),
        patch.object(routes, "check_job_access", return_value=(None, None, 200)),
        patch.object(service, "is_configured", return_value=True),
        patch.object(
            service,
            "review_job",
            side_effect=service.TreeAnalysisDailyLimit(25, 1234),
        ),
    ):
        response, status = original_view(JOB_ID)

    assert status == 429
    assert response.headers["Retry-After"] == "1234"
    assert "daily review limit (25)" in response.get_json()["error"]
