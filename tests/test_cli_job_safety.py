import unittest
from unittest.mock import patch

from flask import Flask

from app.cli import jobs_in_flight_command


class JobsInFlightCommandTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.cli.add_command(jobs_in_flight_command)
        self.runner = self.app.test_cli_runner()

    def _invoke(self, result):
        with patch(
            "app.services.job_reconcile_service.count_jobs_in_flight",
            return_value=result,
        ):
            return self.runner.invoke(args=["jobs-in-flight"])

    def test_no_live_or_unknown_jobs_is_safe(self):
        result = self._invoke({"in_flight": [], "unknown": []})

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Safe to restart the worker", result.output)

    def test_unknown_job_fails_closed(self):
        result = self._invoke({"in_flight": [], "unknown": ["job-unknown"]})

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("could not be verified against RQ", result.output)
        self.assertIn("Do not restart the worker", result.output)
        self.assertNotIn("Safe to restart the worker", result.output)

    def test_confirmed_live_job_is_unsafe(self):
        result = self._invoke({
            "in_flight": [{
                "job_id": "job-live",
                "db_status": "running",
                "rq_status": "started",
                "created_at": "2026-08-13T00:00:00",
            }],
            "unknown": [],
        })

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("IN FLIGHT", result.output)
        self.assertNotIn("Safe to restart the worker", result.output)

    def test_unknown_and_live_jobs_report_both_and_fail(self):
        result = self._invoke({
            "in_flight": [{
                "job_id": "job-live",
                "db_status": "queued",
                "rq_status": "queued",
                "created_at": None,
            }],
            "unknown": ["job-unknown"],
        })

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("job-unknown", result.output)
        self.assertIn("job-live", result.output)
        self.assertNotIn("Safe to restart the worker", result.output)


if __name__ == "__main__":
    unittest.main()
