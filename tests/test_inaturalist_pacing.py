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
        self.expires_at = {}
        self.calls = 0

    def eval(self, script, numkeys, key, now_ms, interval_ms, max_wait_ms):
        self.calls += 1
        now_ms, interval_ms, max_wait_ms = (
            int(now_ms), int(interval_ms), int(max_wait_ms)
        )
        slot = int(self.store.get(key, 0))
        if slot < now_ms:
            slot = now_ms
        wait = slot - now_ms
        if max_wait_ms >= 0 and wait > max_wait_ms:
            return [-1, wait]
        next_slot = slot + interval_ms
        ttl = max(interval_ms * 2, next_slot - now_ms + interval_ms)
        self.store[key] = next_slot
        self.expires_at[key] = now_ms + ttl
        return [slot, ttl]


class _Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def time(self):
        return self.now


class _LocalSlotIsolation:
    """Restore the module-global pacing cursor after each test.

    `_local_next_slot` is process-wide state. Tests that set it and never put it
    back leaked a future cursor into every later test in the session -- and an
    assertion failure skipped any cleanup written at the end of a test body.
    """

    def setUp(self):
        super().setUp()
        self._saved_local_next_slot = svc._local_next_slot
        self.addCleanup(self._restore_local_next_slot)
        svc._local_next_slot = 0.0

    def _restore_local_next_slot(self):
        svc._local_next_slot = self._saved_local_next_slot


class RedisReservationTests(_LocalSlotIsolation, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.redis = FakeRedis()
        self.clock = _Clock()

    def _reserve(self, interval=1.0, max_wait=None):
        with (
            patch.object(svc, "_pacing_redis", return_value=self.redis),
            patch.object(svc.time, "time", self.clock.time),
        ):
            return svc._reserve_inat_slot(interval, max_wait=max_wait)

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

    def test_large_background_burst_never_collapses_at_interactive_cap(self):
        waits = [self._reserve() for _ in range(40)]
        self.assertEqual(waits, [float(i) for i in range(40)])
        starts = [self.clock.now + wait for wait in waits]
        self.assertTrue(all(
            later - earlier >= svc.RATE_LIMIT_DELAY
            for earlier, later in zip(starts, starts[1:])
        ))

    def test_deep_interactive_caller_is_deferred_not_sent_early(self):
        accepted = [self._reserve(max_wait=30) for _ in range(31)]
        self.assertEqual(accepted[-1], 30.0)
        cursor_before = self.redis.store[svc._PACING_KEY]
        with self.assertRaises(svc.InatTreeError) as caught:
            self._reserve(max_wait=30)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.redis.store[svc._PACING_KEY], cursor_before)

    def test_cursor_ttl_outlives_every_future_reservation(self):
        waits = [self._reserve() for _ in range(40)]
        latest_start_ms = int((self.clock.now + waits[-1]) * 1000)
        self.assertGreater(
            self.redis.expires_at[svc._PACING_KEY], latest_start_ms
        )


class RedisFailureFallbackTests(_LocalSlotIsolation, unittest.TestCase):
    """A brief Redis problem must slow imports down, not make them impossible."""

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

    def test_interactive_fallback_defers_instead_of_sending_early(self):
        svc._local_next_slot = 1000.0
        with (
            patch.object(svc, "_pacing_redis", side_effect=OSError("no redis")),
            patch.object(svc.time, "monotonic", return_value=900.0),
        ):
            with self.assertRaises(svc.InatTreeError):
                svc._reserve_inat_slot(1.0, max_wait=30.0)

    def test_a_rejected_interactive_request_does_not_consume_a_slot(self):
        """Match the Redis Lua path: check the wait, *then* commit the cursor.

        The local fallback advanced `_local_next_slot` before deciding whether
        the caller was allowed to wait that long, so every refusal pushed the
        cursor a further interval into the future -- for requests that were
        never sent. Ten refused clicks left the next legitimate caller a hundred
        seconds behind for no reason.
        """
        svc._local_next_slot = 1000.0
        with (
            patch.object(svc, "_pacing_redis", side_effect=OSError("no redis")),
            patch.object(svc.time, "monotonic", return_value=900.0),
        ):
            for _ in range(10):
                with self.assertRaises(svc.InatTreeError):
                    svc._reserve_inat_slot(10.0, max_wait=30.0)

        self.assertEqual(svc._local_next_slot, 1000.0)

    def test_an_accepted_interactive_request_still_commits_its_slot(self):
        svc._local_next_slot = 905.0
        with (
            patch.object(svc, "_pacing_redis", side_effect=OSError("no redis")),
            patch.object(svc.time, "monotonic", return_value=900.0),
        ):
            wait = svc._reserve_inat_slot(1.0, max_wait=30.0)

        self.assertAlmostEqual(wait, 5.0, places=3)
        self.assertEqual(svc._local_next_slot, 906.0)


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
