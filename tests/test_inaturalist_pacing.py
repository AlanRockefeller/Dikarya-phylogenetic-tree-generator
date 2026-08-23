"""iNaturalist request pacing.

iNat asks for at most one request per second. Dikarya runs four Gunicorn
workers plus an RQ worker, so a process-local counter would allow one request
per second *per process*. The reservation therefore lives in Redis, which every
process already shares, and every request -- including retries -- goes through
it. No test here actually sleeps: the pacer is exercised through its returned
wait, and time.sleep is patched out.
"""

import unittest
from unittest.mock import patch

from app.services import inaturalist_tree_service as svc


class FakeRedis:
    """Enough of Redis to run the reservation script deterministically."""

    def __init__(self):
        self.store = {}
        self.calls = 0

    def eval(self, script, numkeys, key, now_ms, interval_ms, ttl_ms):
        self.calls += 1
        now_ms, interval_ms = int(now_ms), int(interval_ms)
        slot = int(self.store.get(key, 0))
        if slot < now_ms:
            slot = now_ms
        self.store[key] = slot + interval_ms
        return slot


class _Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def time(self):
        return self.now


class RedisReservationTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.clock = _Clock()
        svc._local_next_slot = 0.0

    def _reserve(self, interval=1.0):
        with (
            patch.object(svc, "_pacing_redis", return_value=self.redis),
            patch.object(svc.time, "time", self.clock.time),
        ):
            return svc._reserve_inat_slot(interval)

    def test_first_request_is_not_delayed(self):
        self.assertEqual(self._reserve(), 0.0)

    def test_immediate_second_request_waits_a_full_interval(self):
        self._reserve()
        self.assertAlmostEqual(self._reserve(), 1.0, places=3)

    def test_consecutive_requests_are_spaced_one_interval_apart(self):
        waits = [self._reserve() for _ in range(5)]
        self.assertEqual(waits, [0.0, 1.0, 2.0, 3.0, 4.0])

    def test_a_caller_that_waited_long_enough_is_not_delayed_again(self):
        self._reserve()
        self.clock.now += 5.0
        self.assertEqual(self._reserve(), 0.0)

    def test_separate_processes_share_one_cursor(self):
        # Two "processes" only share the Redis state, not any module globals.
        first = self._reserve()
        other = FakeRedis()
        other.store = self.redis.store  # same Redis, different client object
        with (
            patch.object(svc, "_pacing_redis", return_value=other),
            patch.object(svc.time, "time", self.clock.time),
        ):
            second = svc._reserve_inat_slot(1.0)
        self.assertEqual(first, 0.0)
        self.assertAlmostEqual(second, 1.0, places=3)

    def test_wait_is_capped_so_one_request_cannot_hang_indefinitely(self):
        self.redis.store[svc._PACING_KEY] = int(self.clock.now * 1000) + 600_000
        self.assertEqual(self._reserve(), svc.MAX_PACING_WAIT_SECONDS)


class RedisFailureFallbackTests(unittest.TestCase):
    """A brief Redis problem must slow imports down, not make them impossible."""

    def setUp(self):
        svc._local_next_slot = 0.0

    def test_reservation_falls_back_to_process_local_pacing(self):
        monotonic = [500.0]
        with (
            patch.object(svc, "_pacing_redis", side_effect=OSError("no redis")),
            patch.object(svc.time, "monotonic", lambda: monotonic[0]),
        ):
            first = svc._reserve_inat_slot(1.0)
            second = svc._reserve_inat_slot(1.0)
        self.assertEqual(first, 0.0)
        self.assertAlmostEqual(second, 1.0, places=3)

    def test_fallback_is_still_paced_and_never_raises(self):
        with patch.object(svc, "_pacing_redis", side_effect=RuntimeError("boom")):
            self.assertIsInstance(svc._reserve_inat_slot(1.0), float)


class RequestPathTests(unittest.TestCase):
    """Every outbound call, retries included, goes through the pacer."""

    def _run(self, urlopen_side_effect):
        paced = []
        with (
            patch.object(svc, "_pace_inat_request",
                         side_effect=lambda: paced.append(1)),
            patch.object(svc.time, "sleep", lambda _s: None),
            patch.object(svc.urllib.request, "urlopen",
                         side_effect=urlopen_side_effect),
        ):
            try:
                svc._http_request("https://api.inaturalist.org/v1/observations/1")
            except svc.InatTreeError:
                pass
        return paced

    def test_a_single_observation_call_is_paced(self):
        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return b'{"results": []}'

        self.assertEqual(len(self._run(lambda *a, **k: _Resp())), 1)

    def test_each_retry_is_paced_too(self):
        import urllib.error

        def _always_429(*a, **k):
            raise urllib.error.HTTPError(
                "u", 429, "Too Many Requests", {}, None,
            )

        paced = self._run(_always_429)
        self.assertEqual(len(paced), svc.MAX_HTTP_ATTEMPTS + 1)


class NoRedundantPageSleepTests(unittest.TestCase):
    def test_page_loop_no_longer_sleeps_separately(self):
        source = open(svc.__file__).read()
        body = source.split("def _collect_scope_observations_with_field", 1)[1]
        body = body.split("\ndef ", 1)[0]
        self.assertNotIn("time.sleep(RATE_LIMIT_DELAY)", body)


if __name__ == "__main__":
    unittest.main()
