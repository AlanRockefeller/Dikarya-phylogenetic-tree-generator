import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from rq import Retry

from app.workers.queue import get_job_status


class WorkerQueueStatusTests(unittest.TestCase):
    def test_retry_result_is_omitted_from_status_response(self):
        job = Mock()
        job.get_status.return_value = "scheduled"
        job.result = Retry(max=1, interval=60)
        job.enqueued_at = datetime.now(timezone.utc)
        job.started_at = None
        job.ended_at = None
        job.exc_info = "an error from a prior attempt"

        queue = Mock()
        queue.fetch_job.return_value = job

        with patch("app.workers.queue.get_queue", return_value=queue):
            status = get_job_status("job-id")

        self.assertEqual(status["status"], "queued")
        self.assertNotIn("error", status)
        self.assertNotIn("result", status)

    def test_failed_status_includes_error(self):
        job = Mock()
        job.get_status.return_value = "failed"
        job.result = None
        job.enqueued_at = None
        job.started_at = None
        job.ended_at = datetime.now(timezone.utc)
        job.exc_info = "current failure"

        queue = Mock()
        queue.fetch_job.return_value = job

        with patch("app.workers.queue.get_queue", return_value=queue):
            status = get_job_status("job-id")

        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "current failure")


if __name__ == "__main__":
    unittest.main()
