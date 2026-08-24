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
        """Run the command, refusing to let a crash masquerade as a failure.

        CliRunner catches whatever the command raises and reports exit_code 1,
        so a bare `assertNotEqual(exit_code, 0)` passed both when the command
        deliberately failed closed and when it blew up on the way there. Re-raise
        anything that is not the command's own SystemExit, and let each test
        assert the specific documented code -- 1 and 2 are not interchangeable
        here: 1 means live work would be destroyed, 2 means nothing is confirmed
        running but a row could not be verified.
        """
        with patch(
            "app.services.job_reconcile_service.count_jobs_in_flight",
            return_value=result,
        ):
            invoked = self.runner.invoke(args=["jobs-in-flight"])
        if invoked.exception is not None and not isinstance(invoked.exception, SystemExit):
            raise invoked.exception
        return invoked

    def test_no_live_or_unknown_jobs_is_safe(self):
        result = self._invoke({"in_flight": [], "unknown": []})

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Safe to restart the worker", result.output)

    def test_unknown_job_fails_closed(self):
        result = self._invoke({"in_flight": [], "unknown": ["job-unknown"]})

        # 2: nothing confirmed running, but a row could not be checked.
        self.assertEqual(result.exit_code, 2)
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

        # 1: a restart would destroy work that is genuinely in flight.
        self.assertEqual(result.exit_code, 1)
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

        # 1, not 2: a confirmed live job outranks the unverifiable one.
        self.assertEqual(result.exit_code, 1)
        self.assertIn("job-unknown", result.output)
        self.assertIn("job-live", result.output)
        self.assertNotIn("Safe to restart the worker", result.output)


if __name__ == "__main__":
    unittest.main()
