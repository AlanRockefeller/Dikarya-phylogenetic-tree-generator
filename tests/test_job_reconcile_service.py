import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rq.results import Result


def _naive_utc_now():
    """Now, as the naive UTC value Job.created_at stores.

    The migration declares created_at as sa.DateTime() with no timezone, so the
    fixtures must stay naive. datetime.utcnow() produced exactly this value but
    is deprecated in 3.12 and emitted a warning on every one of these tests.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


from app.config import Config
from app.services.job_reconcile_service import (
    RQJobInspection,
    _rq_was_killed,
    classify_reap_candidate,
    reconcile_job_statuses,
)


class _Field:
    def in_(self, _values):
        return self


class _Query:
    def __init__(self, jobs):
        self.jobs = jobs

    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def all(self):
        return self.jobs


def _model_for(jobs):
    return type(
        "FakeJobModel",
        (),
        {"status": _Field(), "created_at": _Field(), "query": _Query(jobs)},
    )


def _rq_job(exc_string, origin="phylo_high"):
    result = SimpleNamespace(type=Result.Type.FAILED, exc_string=exc_string)
    return SimpleNamespace(
        id="job-1",
        origin=origin,
        latest_result=lambda: result,
        requeue=Mock(),
    )


class RQInterruptionEvidenceTests(unittest.TestCase):
    def test_reaper_only_accepts_verified_missing_jobs(self):
        for status in ("created", "queued", "started", "deferred", "scheduled"):
            self.assertEqual(
                classify_reap_candidate(
                    RQJobInspection(verified=True, status=status)
                ),
                "live",
            )
        self.assertEqual(
            classify_reap_candidate(RQJobInspection(verified=False, error="down")),
            "unverified",
        )
        self.assertEqual(
            classify_reap_candidate(RQJobInspection(verified=True, missing=True)),
            "reap",
        )
        self.assertEqual(
            classify_reap_candidate(
                RQJobInspection(verified=True, status="finished")
            ),
            "ordinary_rq",
        )

    def test_only_positive_rq_workhorse_evidence_is_retryable(self):
        ordinary = _rq_job("Traceback (most recent call last):\nValueError: bad input")
        ambiguous = _rq_job("")
        stopped = _rq_job("Job stopped by user, work-horse terminated.")
        killed = _rq_job(
            "Work-horse terminated unexpectedly; waitpid returned 9 (signal 9); "
        )
        abandoned = _rq_job(
            "Moved to FailedJobRegistry, due to AbandonedJobError, at 2026-08-13"
        )

        self.assertFalse(_rq_was_killed(ordinary))
        self.assertFalse(_rq_was_killed(ambiguous))
        self.assertFalse(_rq_was_killed(stopped))
        self.assertTrue(_rq_was_killed(killed))
        self.assertTrue(_rq_was_killed(abandoned))

    def _reconcile(self, db_job, inspection, job_dir):
        # rollback() is exercised on every path that does not commit, so the
        # read transaction opened by the candidate query is always closed.
        fake_db = SimpleNamespace(
            session=SimpleNamespace(commit=Mock(), rollback=Mock())
        )
        fake_model = _model_for([db_job])
        with (
            patch("app.models.Job", fake_model),
            patch("app.extensions.db", fake_db),
            patch("app.workers.queue.get_redis_connection", return_value=Mock()),
            patch(
                "app.services.job_reconcile_service.inspect_rq_job",
                return_value=inspection,
            ),
            patch.object(Config, "JOB_DIR", Path(job_dir)),
        ):
            changed = reconcile_job_statuses()
        return changed, fake_db

    def test_normal_failed_canceled_and_ambiguous_jobs_are_not_requeued(self):
        cases = [
            ("failed", _rq_job("Traceback:\nValueError: deterministic")),
            ("failed", _rq_job("")),
            ("canceled", _rq_job("")),
            ("stopped", _rq_job("Job stopped by user, work-horse terminated.")),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for status, rq_job in cases:
                db_job = SimpleNamespace(
                    id="job-1", status="running", metrics={},
                    created_at=_naive_utc_now(), updated_at=None,
                )
                inspection = RQJobInspection(
                    verified=True, status=status, rq_job=rq_job
                )
                changed, _fake_db = self._reconcile(db_job, inspection, tmp)
                self.assertEqual(db_job.status, "failed")
                self.assertEqual(changed[0]["action"], "failed")
                rq_job.requeue.assert_not_called()

    def test_unexpected_bulk_interruption_requeues_once_on_same_origin(self):
        rq_job = _rq_job(
            "Work-horse terminated unexpectedly; waitpid returned 9 (signal 9); ",
            origin="phylo_bulk",
        )
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job-1"
            (job_dir / "alignment").mkdir(parents=True)
            (job_dir / "input_info.json").write_text('{"sequence": ">a\\nACGT"}')
            stale = job_dir / "alignment" / "alignment_raw.fasta"
            stale.write_text("")
            db_job = SimpleNamespace(
                id="job-1", status="running", metrics={},
                created_at=_naive_utc_now(), updated_at=None,
            )
            inspection = RQJobInspection(
                verified=True, status="failed", rq_job=rq_job
            )

            changed, _fake_db = self._reconcile(db_job, inspection, tmp)

            self.assertEqual(db_job.status, "queued")
            self.assertEqual(db_job.metrics["restart_requeue_count"], 1)
            self.assertEqual(db_job.metrics["requeued_queue"], "phylo_bulk")
            self.assertEqual(changed[0]["from_status"], "running")
            self.assertEqual(changed[0]["queue"], "phylo_bulk")
            self.assertFalse(stale.exists())
            self.assertTrue((job_dir / "input_info.json").exists())
            rq_job.requeue.assert_called_once_with()

    def test_second_interruption_is_terminal(self):
        rq_job = _rq_job(
            "Moved to FailedJobRegistry, due to AbandonedJobError, at 2026-08-13"
        )
        db_job = SimpleNamespace(
            id="job-1", status="queued", metrics={"restart_requeue_count": 1},
            created_at=_naive_utc_now(), updated_at=None,
        )
        inspection = RQJobInspection(
            verified=True, status="failed", rq_job=rq_job
        )
        with tempfile.TemporaryDirectory() as tmp:
            changed, _fake_db = self._reconcile(db_job, inspection, tmp)

        self.assertEqual(db_job.status, "failed")
        self.assertEqual(changed[0]["from_status"], "queued")
        rq_job.requeue.assert_not_called()

    def test_a_stale_artifact_that_will_not_delete_blocks_the_requeue(self):
        """Cleanup failure must never end in a requeue.

        Removing the possibly-truncated alignment left by a SIGKILLed tool is
        the entire safety mechanism. `discard_artifact` logs and swallows
        OSError rather than raising, so the only reliable signal is whether the
        file is still there afterwards; if it is, the job is failed with a
        reason instead of rerun into corrupt partial output.
        """
        rq_job = _rq_job(
            "Work-horse terminated unexpectedly; waitpid returned 9 (signal 9); ",
        )
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job-1"
            (job_dir / "alignment").mkdir(parents=True)
            (job_dir / "input_info.json").write_text('{"sequence": ">a\\nACGT"}')
            stale = job_dir / "alignment" / "alignment_raw.fasta"
            stale.write_text("truncated")
            db_job = SimpleNamespace(
                id="job-1", status="running", metrics={},
                created_at=_naive_utc_now(), updated_at=None,
            )
            inspection = RQJobInspection(
                verified=True, status="failed", rq_job=rq_job
            )
            # Exactly what discard_artifact does when unlink() fails: it warns
            # and returns, leaving the file in place.
            with patch(
                "app.services.job_reconcile_service.discard_artifact",
                return_value=0,
            ):
                changed, _fake_db = self._reconcile(db_job, inspection, tmp)

            rq_job.requeue.assert_not_called()
            self.assertEqual(db_job.status, "failed")
            self.assertEqual(changed[0]["action"], "failed")
            self.assertIn("alignment_raw.fasta", db_job.metrics["reconciled_reason"])
            self.assertIn("could not be removed",
                          db_job.metrics["reconciled_reason"])
            self.assertTrue(stale.exists())

    def test_a_gzipped_stale_artifact_left_behind_also_blocks_the_requeue(self):
        rq_job = _rq_job(
            "Work-horse terminated unexpectedly; waitpid returned 9 (signal 9); ",
        )
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job-1"
            (job_dir / "alignment").mkdir(parents=True)
            (job_dir / "input_info.json").write_text('{"sequence": ">a\\nACGT"}')
            leftover = job_dir / "alignment" / "alignment_raw.fasta.gz"
            leftover.write_bytes(b"\x1f\x8b")
            db_job = SimpleNamespace(
                id="job-1", status="running", metrics={},
                created_at=_naive_utc_now(), updated_at=None,
            )
            inspection = RQJobInspection(
                verified=True, status="failed", rq_job=rq_job
            )
            with patch(
                "app.services.job_reconcile_service.discard_artifact",
                return_value=0,
            ):
                changed, _fake_db = self._reconcile(db_job, inspection, tmp)

            rq_job.requeue.assert_not_called()
            self.assertEqual(changed[0]["action"], "failed")

    def test_unverified_rq_lookup_changes_nothing(self):
        db_job = SimpleNamespace(
            id="job-1", status="running", metrics={},
            created_at=_naive_utc_now(), updated_at=None,
        )
        inspection = RQJobInspection(verified=False, error="Redis unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            changed, fake_db = self._reconcile(db_job, inspection, tmp)

        self.assertEqual(changed, [])
        self.assertEqual(db_job.status, "running")
        fake_db.session.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
