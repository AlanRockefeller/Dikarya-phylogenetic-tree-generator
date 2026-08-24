import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from rq import Retry
from rq.exceptions import NoSuchJobError

from app.workers.queue import get_job_status


def _job(status="queued", *, origin="phylo_high", result=None, exc_info=None):
    job = Mock()
    job.origin = origin
    job.get_status.return_value = status
    job.result = result
    job.enqueued_at = datetime.now(timezone.utc)
    job.started_at = None
    job.ended_at = None
    job.exc_info = exc_info
    return job


class WorkerQueueStatusTests(unittest.TestCase):
    def _status(self, job):
        with (
            patch("app.workers.queue.get_redis_connection", return_value=Mock()),
            patch("app.workers.queue.RqJob.fetch", return_value=job) as fetch,
        ):
            status = get_job_status("job-id")
        fetch.assert_called_once()
        return status

    def test_high_queue_job_is_found_by_global_id(self):
        self.assertEqual(
            self._status(_job(origin="phylo_high"))["status"], "queued"
        )

    def test_bulk_queue_job_is_found_by_global_id(self):
        self.assertEqual(
            self._status(_job(origin="phylo_bulk"))["status"], "queued"
        )

    def test_nonexistent_job_is_unknown(self):
        with (
            patch("app.workers.queue.get_redis_connection", return_value=Mock()),
            patch("app.workers.queue.RqJob.fetch", side_effect=NoSuchJobError),
        ):
            status = get_job_status("missing")
        self.assertEqual(status, {
            "id": "missing", "status": "unknown", "error": "Job not found"
        })

    def test_retry_result_is_omitted_and_scheduled_is_normalized(self):
        status = self._status(_job(
            "scheduled", result=Retry(max=1, interval=60),
            exc_info="an error from a prior attempt",
        ))
        self.assertEqual(status["status"], "queued")
        self.assertNotIn("error", status)
        self.assertNotIn("result", status)

    def test_deferred_is_normalized(self):
        self.assertEqual(self._status(_job("deferred"))["status"], "queued")

    def test_successful_result_is_preserved(self):
        result = {"job_id": "job-id", "status": "completed"}
        status = self._status(_job("finished", result=result))
        self.assertEqual(status["result"], result)

    def test_failed_status_never_exposes_traceback_or_failed_result(self):
        sensitive = (
            "Traceback: /var/www/dikarya/app/workers/tasks.py token=secret-token "
            + "ACGT" * 100
            + " user@example.com"
        )
        status = self._status(_job(
            "failed", result={"error": sensitive}, exc_info=sensitive
        ))
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "Job failed")
        self.assertNotIn("result", status)
        self.assertNotIn("/var/www", str(status))
        self.assertNotIn("secret-token", str(status))
        self.assertNotIn("ACGTACGT", str(status))
        self.assertNotIn("user@example.com", str(status))

    def test_infrastructure_error_is_generic(self):
        with (
            patch("app.workers.queue.get_redis_connection", return_value=Mock()),
            patch(
                "app.workers.queue.RqJob.fetch",
                side_effect=RuntimeError("redis://user:password@private-host"),
            ),
        ):
            status = get_job_status("job-id")
        self.assertEqual(status["error"], "Job status unavailable")
        self.assertNotIn("password", str(status))


if __name__ == "__main__":
    unittest.main()
