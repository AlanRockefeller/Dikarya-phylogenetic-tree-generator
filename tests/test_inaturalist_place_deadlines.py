"""The place-lookup deadline is a wall clock, not a loop-boundary suggestion.

`resolve_place_labels()` and `fetch_observation_places()` compared
`time.monotonic()` against the deadline only between batches. Everything a
batch then did was fixed-size: a 1-second courtesy sleep, a 20-second socket
timeout, and up to three exponential backoffs totalling 14 seconds. A caller
with 100ms of budget left could therefore start work that ran for another
twenty-plus seconds -- and this runs inside a Gunicorn request slot.

No test here sleeps or opens a socket: `time.monotonic` is a scripted clock,
`time.sleep` advances it, and the urlopen calls are recorded.
"""

import json
import unittest
from unittest.mock import patch

from app.services import inaturalist_places as places


class FakeClock:
    """A monotonic clock that only moves when the code under test spends time."""

    def __init__(self, start=1000.0):
        self.now = start
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DeadlineTestCase(unittest.TestCase):
    def setUp(self):
        places._PLACE_CACHE.clear()
        self.addCleanup(places._PLACE_CACHE.clear)
        self.clock = FakeClock()
        self.timeouts = []

        patcher = patch.object(places.time, "monotonic", self.clock.monotonic)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(places.time, "sleep", self.clock.sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _urlopen(self, payload=None, *, cost=0.0, error=None):
        """A urlopen double that records its timeout and spends `cost` seconds."""

        def _open(request, timeout=None):
            self.timeouts.append(timeout)
            self.clock.advance(cost)
            if error is not None:
                raise error
            return _Response(payload or {"results": []})

        return patch.object(places.urllib.request, "urlopen", _open)

    def _observations(self, count, place_ids_each=1):
        return [
            {"id": i, "place_ids": [10_000 + i * place_ids_each + n
                                    for n in range(place_ids_each)]}
            for i in range(1, count + 1)
        ]


class ResolvePlaceLabelDeadlineTests(DeadlineTestCase):
    def test_an_exhausted_deadline_starts_no_request_at_all(self):
        with self._urlopen():
            labels = places.resolve_place_labels(
                self._observations(1), deadline=self.clock.now)

        self.assertEqual(self.timeouts, [])
        self.assertEqual(labels, {})

    def test_the_request_timeout_is_capped_by_the_remaining_budget(self):
        """0.25s of budget must not authorize a 20-second socket read."""
        with self._urlopen():
            places.resolve_place_labels(
                self._observations(1), deadline=self.clock.now + 0.25)

        self.assertEqual(len(self.timeouts), 1)
        self.assertAlmostEqual(self.timeouts[0], 0.25)
        self.assertLess(self.timeouts[0], places.PLACE_REQUEST_TIMEOUT)

    def test_a_generous_budget_still_uses_the_ordinary_timeout(self):
        with self._urlopen():
            places.resolve_place_labels(
                self._observations(1), deadline=self.clock.now + 600)

        self.assertEqual(self.timeouts, [places.PLACE_REQUEST_TIMEOUT])

    def test_the_courtesy_delay_cannot_outlast_the_deadline(self):
        """The inter-batch sleep is spent from the same budget as the requests.

        Two batches, but only 0.4s left after the first: the delay is trimmed
        to what is left and no second request is opened behind it.
        """
        observations = self._observations(1, place_ids_each=places.PLACE_BATCH_SIZE + 1)
        with self._urlopen(cost=0.6):
            places.resolve_place_labels(
                observations, deadline=self.clock.now + 1.0)

        self.assertEqual(len(self.timeouts), 1, "a second batch was started")
        self.assertTrue(self.clock.slept)
        self.assertLessEqual(sum(self.clock.slept), 0.4001)
        self.assertLess(sum(self.clock.slept), places.PLACE_BATCH_DELAY)

    def test_the_whole_call_stays_inside_the_budget(self):
        """The end-to-end property, over enough places to need many batches."""
        observations = self._observations(
            1, place_ids_each=places.PLACE_BATCH_SIZE * 5)
        started = self.clock.now
        with self._urlopen(cost=0.3):
            places.resolve_place_labels(observations, deadline=started + 2.0)

        self.assertLessEqual(self.clock.now - started, 2.0 + 0.3,
                             "overran the budget by more than one in-flight request")

    def test_no_deadline_keeps_the_previous_behaviour(self):
        observations = self._observations(
            1, place_ids_each=places.PLACE_BATCH_SIZE + 1)
        with self._urlopen():
            places.resolve_place_labels(observations)

        self.assertEqual(self.timeouts,
                         [places.PLACE_REQUEST_TIMEOUT] * 2)
        self.assertEqual(self.clock.slept, [places.PLACE_BATCH_DELAY])

    def test_labels_still_come_back_when_there_is_time(self):
        payload = {"results": [
            {"id": 10_001, "name": "Mississippi",
             "admin_level": places.ADMIN_STATE, "display_name": "Mississippi, US"},
        ]}
        with self._urlopen(payload):
            labels = places.resolve_place_labels(
                [{"id": 1, "place_ids": [10_001]}], deadline=self.clock.now + 30)

        self.assertEqual(labels, {1: "Mississippi US"})


class FetchObservationPlaceDeadlineTests(DeadlineTestCase):
    def test_an_exhausted_deadline_opens_nothing(self):
        with self._urlopen():
            result = places.fetch_observation_places([1, 2], deadline=self.clock.now)

        self.assertEqual(self.timeouts, [])
        self.assertEqual(result, [])

    def test_the_request_timeout_is_capped_by_the_remaining_budget(self):
        with self._urlopen():
            places.fetch_observation_places([1], deadline=self.clock.now + 0.5)

        self.assertEqual(len(self.timeouts), 1)
        self.assertAlmostEqual(self.timeouts[0], 0.5)

    def test_retry_backoff_cannot_run_past_the_deadline(self):
        """Backoff reaches 2+4+8 seconds; a 1-second budget must not fund it."""
        started = self.clock.now
        with self._urlopen(error=OSError("connection reset")):
            places.fetch_observation_places([1], deadline=started + 1.0)

        self.assertLessEqual(sum(self.clock.slept), 1.0001)
        self.assertLessEqual(self.clock.now - started, 1.0001)
        # It gave up rather than burning all four attempts inside one second.
        self.assertLess(len(self.timeouts), places.OBSERVATION_FETCH_ATTEMPTS)

    def test_every_retry_timeout_shrinks_with_the_budget(self):
        with self._urlopen(error=OSError("connection reset")):
            places.fetch_observation_places([1], deadline=self.clock.now + 10)

        self.assertTrue(self.timeouts)
        for timeout in self.timeouts:
            self.assertLessEqual(timeout, places.PLACE_REQUEST_TIMEOUT)
        self.assertEqual(self.timeouts, sorted(self.timeouts, reverse=True))

    def test_the_inter_batch_delay_respects_the_deadline(self):
        ids = list(range(1, places.OBSERVATION_BATCH_SIZE + 2))
        with self._urlopen(cost=0.8):
            places.fetch_observation_places(ids, deadline=self.clock.now + 1.0)

        self.assertEqual(len(self.timeouts), 1, "a second batch was started")
        self.assertLessEqual(sum(self.clock.slept), 0.2001)

    def test_without_a_deadline_the_retries_are_unchanged(self):
        with self._urlopen(error=OSError("connection reset")):
            places.fetch_observation_places([1])

        self.assertEqual(len(self.timeouts), places.OBSERVATION_FETCH_ATTEMPTS)
        self.assertEqual(self.timeouts,
                         [places.PLACE_REQUEST_TIMEOUT]
                         * places.OBSERVATION_FETCH_ATTEMPTS)
        self.assertEqual(self.clock.slept, [2, 4, 8])

    def test_a_successful_fetch_still_returns_its_records(self):
        with self._urlopen({"results": [{"id": 1, "place_ids": [9]}]}):
            result = places.fetch_observation_places([1], deadline=self.clock.now + 30)

        self.assertEqual(result, [{"id": 1, "place_ids": [9]}])


class DeadlineHelperTests(DeadlineTestCase):
    def test_no_deadline_means_the_ordinary_ceiling_and_a_full_sleep(self):
        self.assertIsNone(places._remaining(None))
        self.assertEqual(places._request_timeout(None), places.PLACE_REQUEST_TIMEOUT)
        self.assertTrue(places._sleep_within(1.0, None))
        self.assertEqual(self.clock.slept, [1.0])

    def test_a_spent_budget_refuses_both(self):
        self.assertIsNone(places._request_timeout(self.clock.now))
        self.assertFalse(places._sleep_within(1.0, self.clock.now))
        self.assertEqual(self.clock.slept, [])

    def test_a_sleep_that_consumes_the_budget_reports_that_it_did(self):
        self.assertFalse(places._sleep_within(5.0, self.clock.now + 1.0))
        self.assertEqual(self.clock.slept, [1.0])


if __name__ == "__main__":
    unittest.main()
