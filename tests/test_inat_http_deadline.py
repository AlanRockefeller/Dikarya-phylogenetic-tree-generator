"""``_http_request`` under a caller's wall-clock deadline.

WHY THIS IS NOT TESTED THROUGH THE FINDER
-----------------------------------------
The finder's own deadline tests stub ``_fetch_observations`` and drive a fake
clock, which proves the search loop obeys its MODEL of time. It cannot prove a
real request obeys 150 seconds, because every part of a request that actually
spends time sits below that stub:

    _api_get -> _http_request -> pacing wait -> urlopen -> retry sleep -> again

``max_attempts`` counts RETRIES, so ``max_attempts=1`` is two HTTP attempts,
each preceded by a paced wait of up to MAX_PACING_WAIT_SECONDS and separated by
a Retry-After backoff of up to RETRY_BACKOFF_CAP. A timeout computed once by a
caller bounds exactly one of those. So the deadline is enforced here instead,
and this module drives that structure directly.
"""

import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from app.services import inaturalist_tree_service as svc
from app.services.inaturalist_tree_service import InatDeadlineExceeded, InatTreeError

URL = "https://api.inaturalist.org/v1/observations?id=1"


def _http_error(status=429, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(URL, status, "Too Many Requests", headers, None)


class HttpRequestDeadlineTests(unittest.TestCase):
    """A fake clock that only advances where the real one would."""

    def setUp(self):
        self.now = 1000.0
        self.slept = []
        self.paced = []
        self.attempts = []

        def monotonic():
            return self.now

        def sleep(seconds):
            self.slept.append(seconds)
            self.now += seconds

        def reserve(interval=svc.RATE_LIMIT_DELAY, max_wait=None):
            self.paced.append(max_wait)
            wait = self.pacing_wait
            return wait if max_wait is None else min(wait, max_wait)

        self.pacing_wait = 0.0
        for target, replacement in (
            ("monotonic", monotonic), ("sleep", sleep),
        ):
            patcher = patch.object(svc.time, target, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(svc, "_reserve_inat_slot", side_effect=reserve)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _urlopen(self, *, cost, outcome):
        """Every attempt costs ``cost`` seconds of clock, then does ``outcome``."""
        def opener(request, timeout=None):
            self.attempts.append({"timeout": timeout, "at": self.now})
            self.now += min(cost, timeout if timeout is not None else cost)
            raise outcome()
        return patch.object(svc, "diagnostic_urlopen", side_effect=opener)

    # -- the invariant ------------------------------------------------------

    def test_no_attempt_is_started_and_no_sleep_taken_past_the_deadline(self):
        deadline = self.now + 50.0
        with self._urlopen(cost=20.0, outcome=lambda: _http_error(retry_after=30)):
            with self.assertRaises(InatTreeError):
                svc._http_request(URL, max_attempts=3, timeout=20, deadline=deadline)

        self.assertLessEqual(
            self.now, deadline,
            "the request overran its deadline by {:.1f}s".format(self.now - deadline),
        )
        for attempt in self.attempts:
            self.assertLessEqual(
                attempt["at"] + attempt["timeout"], deadline + 1e-9,
                "an attempt was started that could finish after the deadline",
            )

    def test_each_attempt_timeout_is_clamped_to_what_is_left(self):
        # 20s nominal timeout, but only 8s left by the second attempt.
        deadline = self.now + 28.0
        with self._urlopen(cost=20.0, outcome=lambda: _http_error(retry_after=0)):
            with self.assertRaises(InatTreeError):
                svc._http_request(URL, max_attempts=3, timeout=20, deadline=deadline)

        self.assertGreaterEqual(len(self.attempts), 2)
        self.assertEqual(self.attempts[0]["timeout"], 20)
        self.assertAlmostEqual(self.attempts[1]["timeout"], 8.0, places=6)

    def test_a_backoff_longer_than_the_remaining_time_is_never_slept(self):
        deadline = self.now + 25.0
        with self._urlopen(cost=20.0, outcome=lambda: _http_error(retry_after=30)):
            with self.assertRaises(InatTreeError):
                svc._http_request(URL, max_attempts=3, timeout=20, deadline=deadline)
        self.assertEqual(self.slept, [],
                         "slept out a backoff with less time than the backoff")
        self.assertLessEqual(self.now, deadline)

    def test_pacing_cannot_outlast_the_deadline(self):
        # The pacer is told how long it may wait, so a busy queue cannot eat a
        # budget that was meant for the request itself.
        self.pacing_wait = 30.0
        deadline = self.now + 10.0
        with self._urlopen(cost=1.0, outcome=lambda: _http_error(retry_after=0)):
            with self.assertRaises(InatTreeError):
                svc._http_request(URL, max_attempts=0, timeout=20, deadline=deadline)
        self.assertTrue(self.paced)
        self.assertLessEqual(self.paced[0], 10.0)
        self.assertLessEqual(self.now, deadline)

    def test_a_deadline_already_gone_makes_no_request_at_all(self):
        deadline = self.now - 1.0
        with self._urlopen(cost=20.0, outcome=lambda: _http_error()):
            with self.assertRaises(InatDeadlineExceeded):
                svc._http_request(URL, max_attempts=3, timeout=20, deadline=deadline)
        self.assertEqual(self.attempts, [], "a request was made with no time left")

    def test_time_spent_pacing_is_charged_before_the_socket_read(self):
        """The budget is recomputed AFTER pacing, not reused from before it."""
        self.pacing_wait = 15.0
        deadline = self.now + 20.0
        with self._urlopen(cost=20.0, outcome=lambda: _http_error(retry_after=0)):
            with self.assertRaises(InatTreeError):
                svc._http_request(URL, max_attempts=0, timeout=20, deadline=deadline)
        self.assertEqual(len(self.attempts), 1)
        # 20s budget, 15s of it slept in the pacer: 5s left, not the full 20.
        self.assertAlmostEqual(self.attempts[0]["timeout"], 5.0, places=6)

    # -- the no-deadline path is untouched ----------------------------------

    def test_without_a_deadline_the_historical_behaviour_is_unchanged(self):
        with self._urlopen(cost=20.0, outcome=lambda: _http_error(retry_after=2)):
            with self.assertRaises(InatTreeError):
                svc._http_request(URL, max_attempts=2, timeout=20)
        # Three attempts (max_attempts counts retries) and both backoffs slept.
        self.assertEqual(len(self.attempts), 3)
        self.assertEqual(self.slept, [2.0, 2.0])
        self.assertTrue(all(item["timeout"] == 20 for item in self.attempts))
        self.assertEqual(self.paced, [None, None, None])

    def test_a_network_error_retry_is_bounded_the_same_way(self):
        deadline = self.now + 25.0

        def boom():
            return urllib.error.URLError("connection reset")

        with self._urlopen(cost=20.0, outcome=boom):
            with self.assertRaises(InatTreeError):
                svc._http_request(URL, max_attempts=3, timeout=20, deadline=deadline)
        self.assertLessEqual(self.now, deadline)


if __name__ == "__main__":
    unittest.main()
