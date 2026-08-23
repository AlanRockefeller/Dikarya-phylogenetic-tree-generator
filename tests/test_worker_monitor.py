"""Worker startup coverage for delayed RQ jobs."""

import contextlib
import unittest
from unittest.mock import patch

from app.workers import worker_monitor


class TestWorkerScheduler(unittest.TestCase):
    def test_worker_starts_rq_scheduler(self):
        class FakeWorker:
            instance = None

            @staticmethod
            def _get_int_env(name, default):
                # Simulate the legacy production value and verify Dikarya caps
                # it so abandoned-job cleanup is not delayed ten minutes.
                return 600 if name == "RQ_MAINTENANCE_INTERVAL" else default

            def __init__(self, queues, **_kwargs):
                self.queues = queues
                self.work_kwargs = None
                self.cleaned_up = False
                self.maintenance_interval = None
                FakeWorker.instance = self

            def work(self, **kwargs):
                self.work_kwargs = kwargs

            def clean_up_heartbeat(self):
                self.cleaned_up = True

        class FakeApp:
            config = {}

            def app_context(self):
                return contextlib.nullcontext()

        with (
            patch.object(worker_monitor, "HeartbeatWorker", FakeWorker),
            patch("app.workers.queue.get_redis_connection", return_value=object()),
            patch("app.workers.queue.get_queue", side_effect=lambda name: name),
        ):
            worker_monitor.run_worker_with_heartbeat(FakeApp())

        self.assertEqual(FakeWorker.instance.queues, ["phylo_high", "phylo_bulk", "voucher_sync"])
        self.assertEqual(FakeWorker.instance.work_kwargs, {"with_scheduler": True})
        self.assertEqual(FakeWorker.instance.maintenance_interval, 120)
        self.assertTrue(FakeWorker.instance.cleaned_up)


if __name__ == "__main__":
    unittest.main()
