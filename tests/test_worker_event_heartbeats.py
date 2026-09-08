"""The live status machinery added on 2026-08-31.

Long steps now publish a heartbeat from a background thread, overview messages
carry an icon and a dedupe key, and step transitions are timestamped inside
``update_step_meta``. All three are best-effort by design, which is exactly why
they need pinning: a heartbeat thread that outlives its step keeps publishing
into a finished job's channel, an unrecognised icon renders a completion as
still-running, and a timestamp written twice makes the status page report the
wrong elapsed time for the step.
"""

import threading
import time
import unittest
from unittest.mock import patch

from app.workers import events


class _FakeRedis:
    """Records what the worker published, without a broker."""

    def __init__(self, fail=False):
        self.published = []
        self.fail = fail
        self._lock = threading.Lock()

    def publish(self, channel, message):
        if self.fail:
            raise RuntimeError("redis is down")
        with self._lock:
            self.published.append((channel, message))


class _FakeJob:
    """The two attributes of an RQ job that update_step_meta touches."""

    def __init__(self, meta=None):
        self.meta = meta if meta is not None else {}
        self.saves = 0

    def save_meta(self):
        self.saves += 1


class HeartbeatLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.redis = _FakeRedis()
        patcher = patch.object(events, "_get_redis", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _heartbeat_threads(self):
        return [t for t in threading.enumerate() if t.name.startswith("heartbeat-")]

    def test_a_heartbeat_thread_does_not_outlive_its_step(self):
        """A thread still beating into a finished job's channel is the bug."""
        before = self._heartbeat_threads()
        with events.step_heartbeat("job-1", events.STEP_TREE, "Tree Building",
                                   interval=0.01):
            time.sleep(0.05)
            self.assertEqual(len(self._heartbeat_threads()), len(before) + 1)

        # join(timeout=1) inside the contextmanager has already run.
        self.assertEqual(self._heartbeat_threads(), before)

    def test_the_thread_stops_when_the_step_fails(self):
        """The failure path is the one that matters: it does not return normally."""
        before = self._heartbeat_threads()
        with self.assertRaises(ValueError):
            with events.step_heartbeat("job-2", events.STEP_ALIGN, "Alignment",
                                       interval=0.01):
                time.sleep(0.03)
                raise ValueError("mafft died")

        self.assertEqual(self._heartbeat_threads(), before)

    def test_no_heartbeat_is_published_after_the_block_exits(self):
        with events.step_heartbeat("job-3", events.STEP_TREE, "Tree Building",
                                   interval=0.01):
            time.sleep(0.05)
        published_at_exit = len(self.redis.published)
        self.assertGreater(published_at_exit, 0, "no heartbeat was published at all")

        time.sleep(0.05)
        self.assertEqual(len(self.redis.published), published_at_exit)

    def test_a_heartbeat_carries_the_step_and_its_elapsed_time(self):
        with events.step_heartbeat("job-4", events.STEP_TREE, "Tree Building",
                                   interval=0.01):
            time.sleep(0.03)

        import json
        beats = [json.loads(message) for _channel, message in self.redis.published]
        self.assertTrue(beats)
        for beat in beats:
            self.assertEqual(beat["type"], events.EVENT_HEARTBEAT)
            self.assertEqual(beat["step"], events.STEP_TREE)
            self.assertEqual(beat["label"], "Tree Building")
            self.assertGreaterEqual(beat["elapsed_seconds"], 0)
            self.assertEqual(beat["job_id"], "job-4")

    def test_a_failing_publish_neither_raises_nor_leaks_the_thread(self):
        """Every publish is best-effort; a dead Redis must not fail the step."""
        with patch.object(events, "_get_redis", return_value=_FakeRedis(fail=True)):
            before = self._heartbeat_threads()
            with events.step_heartbeat("job-5", events.STEP_ALIGN, "Alignment",
                                       interval=0.01):
                time.sleep(0.05)
            self.assertEqual(self._heartbeat_threads(), before)

    def test_the_thread_is_a_daemon_so_a_hung_publish_cannot_block_shutdown(self):
        seen = []
        real_thread = threading.Thread

        def capture(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            seen.append(thread)
            return thread

        with patch.object(events.threading, "Thread", capture):
            with events.step_heartbeat("job-6", events.STEP_TREE, "Tree Building",
                                       interval=5):
                pass

        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].daemon)


class OverviewIconTests(unittest.TestCase):
    def setUp(self):
        self.redis = _FakeRedis()
        patcher = patch.object(events, "_get_redis", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _last(self):
        import json
        return json.loads(self.redis.published[-1][1])

    def test_every_known_icon_survives_unchanged(self):
        for icon in events.OVERVIEW_ICONS:
            with self.subTest(icon=icon):
                events.publish_overview("job", "message", icon=icon)
                self.assertEqual(self._last()["icon"], icon)

    def test_an_unrecognised_icon_falls_back_to_running(self):
        """The feed draws whatever it is given; a bad value must not mean "done"."""
        for icon in ("", None, "success", "DONE", 3, ["done"]):
            with self.subTest(icon=icon):
                events.publish_overview("job", "message", icon=icon)
                self.assertEqual(self._last()["icon"], events.STATE_RUNNING)

    def test_the_default_icon_is_running(self):
        events.publish_overview("job", "message")
        self.assertEqual(self._last()["icon"], events.STATE_RUNNING)

    def test_a_key_is_sent_only_when_one_is_given(self):
        """The page dedupes on the key, so an empty one must not become a key."""
        events.publish_overview("job", "message")
        self.assertNotIn("key", self._last())

        events.publish_overview("job", "message", key="skip:blast")
        self.assertEqual(self._last()["key"], "skip:blast")


class StepTimestampTests(unittest.TestCase):
    """job.meta timestamps are what the status page reports elapsed time from."""

    def test_running_stamps_started_at_and_a_terminal_state_stamps_ended_at(self):
        job = _FakeJob()
        events.update_step_meta(job, events.STEP_ALIGN, {"state": events.STATE_RUNNING})
        entry = job.meta["steps"][events.STEP_ALIGN]
        started = entry["started_at"]
        self.assertIsInstance(started, float)
        self.assertNotIn("ended_at", entry)

        events.update_step_meta(job, events.STEP_ALIGN,
                                {"state": events.STATE_DONE, "detail": "ok"})
        self.assertEqual(entry["started_at"], started, "started_at was rewritten")
        self.assertGreaterEqual(entry["ended_at"], started)

    def test_a_repeated_running_patch_does_not_restart_the_clock(self):
        """run_phylo_job marks INPUT running twice; the first time is the start."""
        job = _FakeJob()
        events.update_step_meta(job, events.STEP_INPUT, {"state": events.STATE_RUNNING})
        started = job.meta["steps"][events.STEP_INPUT]["started_at"]
        time.sleep(0.01)
        events.update_step_meta(job, events.STEP_INPUT, {
            "state": events.STATE_RUNNING, "label": "MycoMap Input Preparation",
        })
        self.assertEqual(job.meta["steps"][events.STEP_INPUT]["started_at"], started)

    def test_every_terminal_state_stamps_ended_at(self):
        for state in (events.STATE_DONE, events.STATE_FAILED, events.STATE_SKIPPED):
            with self.subTest(state=state):
                job = _FakeJob()
                events.update_step_meta(job, events.STEP_TRIM, {"state": state})
                self.assertIn("ended_at", job.meta["steps"][events.STEP_TRIM])

    def test_a_patch_with_no_state_touches_neither_timestamp(self):
        job = _FakeJob()
        events.update_step_meta(job, events.STEP_TREE, {"detail": "bootstrapping"})
        entry = job.meta["steps"][events.STEP_TREE]
        self.assertNotIn("started_at", entry)
        self.assertNotIn("ended_at", entry)

    def test_a_none_job_is_a_no_op_rather_than_an_AttributeError(self):
        events.update_step_meta(None, events.STEP_TREE, {"state": events.STATE_DONE})
        events.update_job_meta(None, {"current_step": "tree"})

    def test_the_meta_is_saved_once_per_transition(self):
        job = _FakeJob()
        events.update_step_meta(job, events.STEP_TREE, {"state": events.STATE_RUNNING})
        self.assertEqual(job.saves, 1)


class TerminalStateCleanupTests(unittest.TestCase):
    """A terminal job must not leave per-job rate-limiter state behind."""

    def setUp(self):
        self.redis = _FakeRedis()
        patcher = patch.object(events, "_get_redis", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _has_state(self, job_id):
        return job_id in events._rate_limiter._job_keys

    def test_completion_forgets_the_jobs_rate_limiter_buckets(self):
        events.publish_log("done-job", events.STEP_TREE, "stdout", "a line")
        self.assertTrue(self._has_state("done-job"))

        events.publish_job_completed("done-job", "/job/done-job/view")
        self.assertFalse(self._has_state("done-job"))

    def test_failure_forgets_them_too(self):
        events.publish_log("failed-job", events.STEP_TREE, "stdout", "a line")
        self.assertTrue(self._has_state("failed-job"))

        events.publish_job_failed("failed-job", events.STEP_TREE, "Tree Building",
                                  "raxml exited 1")
        self.assertFalse(self._has_state("failed-job"))

    def test_state_is_forgotten_even_when_the_terminal_publish_fails(self):
        events.publish_log("broken-job", events.STEP_TREE, "stdout", "a line")
        self.assertTrue(self._has_state("broken-job"))

        with patch.object(events, "publish_event", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                events.publish_job_completed("broken-job", "/job/broken-job/view")

        self.assertFalse(self._has_state("broken-job"))


if __name__ == "__main__":
    unittest.main()
