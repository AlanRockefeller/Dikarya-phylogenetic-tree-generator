"""Public API v1 job lifecycle: create ordering, deletion safety, validation.

These cover four separate defects:

* the Job row was committed *after* the work was queued, so a free worker could
  start a job whose DB row did not exist yet;
* DELETE removed the job directory before committing the row deletion, and did
  nothing about a worker still writing into it;
* any non-empty `input_type` was accepted with 202 and only failed in the
  worker;
* a JSON array or scalar body survived `get_json(silent=True) or {}` and turned
  the handler's first `.get()` into a 500.
"""

import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask, g

from app.api_v1 import routes as v1
from app.config import Config

FASTA = ">Sample_A\nACGTACGTACGTACGTACGT\n>Sample_B\nACGTACGTACGTACGTACGA\n"


def _undecorated(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


class _RecordingDb:
    """A db double that records the order of session operations."""

    def __init__(self, events, commit_error=None):
        self.events = events
        self.commit_error = commit_error
        self.added = []
        self.deleted = []
        self.session = SimpleNamespace(
            add=self._add, delete=self._delete,
            commit=self._commit, rollback=self._rollback,
            expire_all=lambda: None, remove=lambda: None,
        )

    def _add(self, obj):
        self.added.append(obj)
        self.events.append("add")

    def _delete(self, obj):
        self.deleted.append(obj)
        self.events.append("delete")

    def _commit(self):
        self.events.append("commit")
        if self.commit_error is not None:
            raise self.commit_error

    def _rollback(self):
        self.events.append("rollback")


def _call_create(payload, *, enqueue=None, db=None, events=None):
    events = events if events is not None else []
    db = db or _RecordingDb(events)
    created = {}

    def _record_job(**kwargs):
        created.update(kwargs)
        return SimpleNamespace(**kwargs)

    def _default_enqueue(job_params, *a, **kw):
        events.append("enqueue")
        return kw.get("job_id")

    app = Flask(__name__)
    with (
        app.test_request_context(method="POST", json=payload),
        patch.object(v1, "enqueue_job", side_effect=enqueue or _default_enqueue),
        patch.object(v1, "Job", _record_job),
        patch.object(v1, "db", db),
        patch.object(v1, "serialize_job", side_effect=lambda job: {"id": job.id}),
    ):
        g.api_user = SimpleNamespace(id=7)
        g.api_token = SimpleNamespace(id=3)
        response = _undecorated(v1.create_job)()
    return response, events, created, db


# ---------------------------------------------------------------------------
# CodeRabbit #14 -- persist the Job row before enqueueing
# ---------------------------------------------------------------------------

class CreateOrderingTests(unittest.TestCase):
    def test_the_row_is_committed_before_the_job_becomes_runnable(self):
        response, events, created, _db = _call_create(
            {"input_type": "pasted_sequence", "sequence": FASTA})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(events, ["add", "commit", "enqueue"])
        self.assertLess(events.index("commit"), events.index("enqueue"))
        self.assertEqual(created["status"], "queued")
        self.assertEqual(created["user_id"], 7)

    def test_the_queued_id_is_the_committed_row_id(self):
        seen = {}

        def _enqueue(job_params, *a, **kw):
            seen["job_id"] = kw.get("job_id")
            return kw.get("job_id")

        _response, _events, created, _db = _call_create(
            {"input_type": "pasted_sequence", "sequence": FASTA},
            enqueue=_enqueue)

        self.assertEqual(seen["job_id"], created["id"])
        self.assertTrue(created["job_dir"].endswith(created["id"]))

    def test_a_failed_enqueue_keeps_the_row_and_marks_it_failed(self):
        events = []
        db = _RecordingDb(events)

        def _boom(job_params, *a, **kw):
            events.append("enqueue")
            raise RuntimeError("redis is down")

        response, events, _created, db = _call_create(
            {"input_type": "pasted_sequence", "sequence": FASTA},
            enqueue=_boom, db=db, events=events)

        self.assertEqual(response.status_code, 500)
        # The row is not deleted -- it is the only evidence the work was asked
        # for -- and it is left in an explicit failed state.
        self.assertEqual(db.deleted, [])
        record = db.added[0]
        self.assertEqual(record.status, "failed")
        self.assertIn("could not be added to the processing queue",
                      record.metrics["error"])
        self.assertEqual(record.metrics["enqueue_error"], "RuntimeError")

    def test_a_failed_commit_does_not_queue_anything(self):
        events = []
        db = _RecordingDb(events, commit_error=RuntimeError("db down"))

        response, events, _created, _db = _call_create(
            {"input_type": "pasted_sequence", "sequence": FASTA},
            db=db, events=events)

        self.assertEqual(response.status_code, 500)
        self.assertNotIn("enqueue", events)
        self.assertIn("rollback", events)

    def test_invalid_iqtree_ufboot_is_rejected_before_persistence(self):
        for bootstrap, message in (
            (999, "at least 1000"),
            ("many", "integer"),
            (1000.5, "integer"),
        ):
            with self.subTest(bootstrap=bootstrap):
                response, events, created, db = _call_create({
                    "input_type": "pasted_sequence",
                    "sequence": FASTA,
                    "tree_method": "iqtree",
                    "bootstrap": bootstrap,
                })

                self.assertEqual(response.status_code, 422)
                self.assertIn(message, response.get_json()["error"]["message"])
                self.assertEqual(events, [])
                self.assertEqual(created, {})
                self.assertEqual(db.added, [])


# ---------------------------------------------------------------------------
# CodeRabbit #17 -- reject unsupported input_type synchronously
# ---------------------------------------------------------------------------

class InputTypeValidationTests(unittest.TestCase):
    def test_documented_modes_still_work(self):
        for payload, expected in (
            ({"input_type": "pasted_sequence", "sequence": FASTA},
             "pasted_sequence"),
            ({"input_type": "accession_list", "accessions": ["MK564475"]},
             "accession_list"),
        ):
            with self.subTest(payload=payload):
                response, _e, created, _db = _call_create(payload)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(created["input_type"], expected)

    def test_accepted_aliases_still_normalize(self):
        cases = [
            ({"input_type": "sequence", "sequence": FASTA}, "pasted_sequence"),
            ({"input_type": "fasta", "sequence": FASTA}, "pasted_sequence"),
            ({"input_type": "accessions", "accessions": ["MK564475"]},
             "accession_list"),
            ({"input_type": "  PASTED_SEQUENCE  ", "sequence": FASTA},
             "pasted_sequence"),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                response, _e, created, _db = _call_create(payload)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(created["input_type"], expected)

    def test_omitted_input_type_is_still_inferred(self):
        response, _e, created, _db = _call_create({"sequence": FASTA})
        self.assertEqual(created["input_type"], "pasted_sequence")

        response, _e, created, _db = _call_create({"accessions": ["MK564475"]})
        self.assertEqual(created["input_type"], "accession_list")

    def test_unsupported_values_are_rejected_before_anything_is_created(self):
        for bad in ("nonsense", "genbank", "upload", "PASTED SEQUENCE", "sql"):
            with self.subTest(value=bad):
                response, events, created, db = _call_create(
                    {"input_type": bad, "sequence": FASTA})
                self.assertEqual(response.status_code, 422)
                body = response.get_json()["error"]
                self.assertEqual(body["code"], "validation_failed")
                self.assertEqual(body["details"]["field"], "input_type")
                self.assertEqual(events, [])
                self.assertEqual(created, {})
                self.assertEqual(db.added, [])

    def test_fasta_upload_is_rejected_with_or_without_sequence_text(self):
        for payload in ({"input_type": "fasta_upload", "sequence": FASTA},
                        {"input_type": "fasta_upload",
                         "accessions": ["MK564475"]}):
            with self.subTest(payload=payload):
                response, events, _created, _db = _call_create(payload)
                self.assertEqual(response.status_code, 422)
                self.assertIn("does not support server-side FASTA file",
                              response.get_json()["error"]["message"])
                self.assertEqual(events, [])

    def test_a_non_string_input_type_is_a_422_not_a_500(self):
        for bad in (5, ["pasted_sequence"], {"a": 1}, True):
            with self.subTest(value=bad):
                response, events, _created, _db = _call_create(
                    {"input_type": bad, "sequence": FASTA})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(events, [])


# ---------------------------------------------------------------------------
# CodeRabbit #18 -- mutation bodies must be JSON objects
# ---------------------------------------------------------------------------

class NonObjectBodyTests(unittest.TestCase):
    NON_OBJECTS = ([1, 2, 3], "a string", 42, True, [{"tips": ["x"]}])

    def _post(self, view, payload, **patches):
        app = Flask(__name__)
        job = SimpleNamespace(id="j", user_id=7, job_dir="/tmp/j",
                              status="completed", metrics={})
        with (
            app.test_request_context(method="POST", json=payload),
            patch.object(v1, "get_owned_job_or_404", return_value=job),
            patch.object(v1, "db", MagicMock()),
        ):
            g.api_user = SimpleNamespace(id=7)
            g.api_token = SimpleNamespace(id=3)
            return _undecorated(view)("11111111-1111-4111-8111-111111111111")

    def test_tree_mutations_reject_non_object_bodies(self):
        for view in (v1.prune_tree_v1, v1.rename_tip_v1, v1.reroot_v1,
                     v1.midpoint_root_v1):
            for payload in self.NON_OBJECTS:
                with self.subTest(view=view.__name__, payload=payload):
                    response = self._post(view, payload)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(
                        response.get_json()["error"]["code"], "bad_request")

    def test_a_valid_object_body_still_reaches_the_handler(self):
        app = Flask(__name__)
        job = SimpleNamespace(id="j", user_id=7, job_dir="/tmp/j",
                              status="completed", metrics={})
        with (
            app.test_request_context(method="POST", json={"tips": ["A"]}),
            patch.object(v1, "get_owned_job_or_404", return_value=job),
            patch("app.services.tree_edit_service.tree_state_lock",
                  MagicMock()),
            patch("app.services.tree_edit_service.load_tree_state",
                  return_value={"pruned": []}),
            patch("app.services.tree_edit_service.prune_taxa",
                  return_value={"pruned": ["A"]}),
            patch("app.services.tree_edit_service.save_tree_state",
                  MagicMock()),
        ):
            g.api_user = SimpleNamespace(id=7)
            g.api_token = SimpleNamespace(id=3)
            response = _undecorated(v1.prune_tree_v1)(
                "11111111-1111-4111-8111-111111111111")
        self.assertEqual(response.status_code, 200)

    def test_an_empty_body_is_still_treated_as_an_empty_object(self):
        app = Flask(__name__)
        job = SimpleNamespace(id="j", user_id=7, job_dir="/tmp/j",
                              status="completed", metrics={})
        with (
            app.test_request_context(method="POST"),
            patch.object(v1, "get_owned_job_or_404", return_value=job),
            patch("app.services.tree_edit_service.tree_state_lock",
                  MagicMock()),
            patch("app.services.tree_edit_service.load_tree_state",
                  return_value={}),
            patch("app.services.tree_edit_service.midpoint_root",
                  return_value={"rooting": "midpoint"}),
            patch("app.services.tree_edit_service.save_tree_state",
                  MagicMock()),
        ):
            g.api_user = SimpleNamespace(id=7)
            g.api_token = SimpleNamespace(id=3)
            response = _undecorated(v1.midpoint_root_v1)(
                "11111111-1111-4111-8111-111111111111")
        self.assertEqual(response.status_code, 200)

    def test_tools_endpoints_reject_non_object_bodies(self):
        app = Flask(__name__)
        for view in (v1.tools_blast, v1.tools_genbank,
                     v1.tools_inaturalist_tree):
            for payload in ([1, 2], "x", 9):
                with self.subTest(view=view.__name__, payload=payload):
                    with app.test_request_context(method="POST", json=payload):
                        g.api_user = SimpleNamespace(id=7)
                        g.api_token = SimpleNamespace(id=3)
                        response = _undecorated(view)()
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(
                        response.get_json()["error"]["code"], "bad_request")


# ---------------------------------------------------------------------------
# CodeRabbit #13 -- deletion must be failure-safe
# ---------------------------------------------------------------------------

class DeleteJobTests(unittest.TestCase):
    JOB_ID = "11111111-1111-4111-8111-111111111111"

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.jobs_root = Path(self._tmp.name)
        self.job_dir = self.jobs_root / self.JOB_ID
        (self.job_dir / "tree").mkdir(parents=True)
        (self.job_dir / "tree" / "tree_original.newick").write_text("(A,B);")
        self.addCleanup(self._tmp.cleanup)

    def _delete(self, *, status="completed", db=None, events=None,
                release=(True, "no RQ record"), extra_patches=()):
        events = events if events is not None else []
        db = db or _RecordingDb(events)
        job = SimpleNamespace(id=self.JOB_ID, user_id=7,
                              job_dir=str(self.job_dir), status=status,
                              metrics={})
        app = Flask(__name__)
        stack = [
            app.test_request_context(method="DELETE"),
            patch.object(v1, "get_owned_job_or_404", return_value=job),
            patch.object(v1, "db", db),
            patch.object(v1, "_release_rq_job", return_value=release),
            patch.object(Config, "JOB_DIR", self.jobs_root),
        ]
        stack.extend(extra_patches)
        with _nested(stack):
            g.api_user = SimpleNamespace(id=7)
            g.api_token = SimpleNamespace(id=3)
            response = _undecorated(v1.delete_job)(self.JOB_ID)
        return response, events, db

    def test_a_finished_job_is_deleted_and_its_files_removed(self):
        response, events, db = self._delete()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["data"]["deleted"])
        self.assertFalse(self.job_dir.exists())
        self.assertFalse((self.jobs_root / ".trash").exists()
                         and any((self.jobs_root / ".trash").iterdir()))
        self.assertIn("delete", events)

    def test_files_are_moved_aside_before_the_row_deletion_is_committed(self):
        observed = {}

        class _WatchingDb(_RecordingDb):
            def _delete(self_inner, obj):
                observed["dir_present_at_delete"] = self.job_dir.exists()
                observed["staged"] = sorted(
                    p.name for p in (self.jobs_root / ".trash").iterdir()
                )
                super()._delete(obj)

        events = []
        self._delete(db=_WatchingDb(events), events=events)

        # The original path is already free, and the bytes are still on disk.
        self.assertFalse(observed["dir_present_at_delete"])
        self.assertEqual(len(observed["staged"]), 1)
        self.assertTrue(observed["staged"][0].startswith(self.JOB_ID))

    def test_a_failed_commit_puts_the_files_back(self):
        events = []
        db = _RecordingDb(events, commit_error=RuntimeError("db down"))

        response, events, _db = self._delete(db=db, events=events)

        self.assertEqual(response.status_code, 500)
        # The row survives, so its artifacts must too.
        self.assertTrue(self.job_dir.is_dir())
        self.assertTrue((self.job_dir / "tree" / "tree_original.newick").is_file())
        self.assertIn("rollback", events)

    def test_a_live_job_commit_failure_restores_files_and_guard_state(self):
        class _FailLogicalDeleteOnce(_RecordingDb):
            def __init__(self, events):
                super().__init__(events)
                self.commits = 0

            def _commit(self):
                self.commits += 1
                self.events.append("commit")
                if self.commits == 2:
                    raise RuntimeError("delete commit failed")

        events = []
        database = _FailLogicalDeleteOnce(events)
        response, events, database = self._delete(
            status="queued", db=database, events=events,
            release=(True, "canceled"),
        )

        self.assertEqual(response.status_code, 500)
        self.assertTrue(self.job_dir.is_dir())
        self.assertEqual(database.deleted[0].status, "queued")
        self.assertEqual(database.commits, 3)

    def test_a_job_that_cannot_be_stopped_is_not_deleted_at_all(self):
        events = []
        response, events, db = self._delete(
            status="running", events=events,
            release=(False, "still running after a stop request"))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "conflict")
        self.assertTrue(self.job_dir.is_dir())
        self.assertEqual(events, ["commit", "commit"])
        self.assertEqual(db.deleted, [])
        self.assertEqual(db.added, [])

    def test_a_queued_job_is_released_from_rq_before_anything_is_touched(self):
        calls = []

        def _release(job_id):
            calls.append((job_id, self.job_dir.exists()))
            return True, "cancelled from queued"

        events = []
        job = SimpleNamespace(id=self.JOB_ID, user_id=7,
                              job_dir=str(self.job_dir), status="queued",
                              metrics={})
        app = Flask(__name__)
        with (
            app.test_request_context(method="DELETE"),
            patch.object(v1, "get_owned_job_or_404", return_value=job),
            patch.object(v1, "db", _RecordingDb(events)),
            patch.object(v1, "_release_rq_job", _release),
            patch.object(Config, "JOB_DIR", self.jobs_root),
        ):
            g.api_user = SimpleNamespace(id=7)
            g.api_token = SimpleNamespace(id=3)
            response = _undecorated(v1.delete_job)(self.JOB_ID)

        self.assertEqual(response.status_code, 200)
        # RQ was released while the directory was still in place.
        self.assertEqual(calls, [(self.JOB_ID, True)])

    def test_the_durable_guard_is_committed_before_rq_release(self):
        events = []

        def _release(_job_id):
            events.append("release")
            return True, "cancelled"

        response, events, _db = self._delete(
            status="queued", events=events,
            extra_patches=[patch.object(v1, "_release_rq_job", _release)],
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events[:2], ["commit", "release"])

    def test_a_completed_job_needs_no_redis_round_trip(self):
        def _explode(job_id):
            raise AssertionError("RQ must not be consulted for a finished job")

        events = []
        job = SimpleNamespace(id=self.JOB_ID, user_id=7,
                              job_dir=str(self.job_dir), status="completed",
                              metrics={})
        app = Flask(__name__)
        with (
            app.test_request_context(method="DELETE"),
            patch.object(v1, "get_owned_job_or_404", return_value=job),
            patch.object(v1, "db", _RecordingDb(events)),
            patch.object(v1, "_release_rq_job", _explode),
            patch.object(Config, "JOB_DIR", self.jobs_root),
        ):
            g.api_user = SimpleNamespace(id=7)
            g.api_token = SimpleNamespace(id=3)
            response = _undecorated(v1.delete_job)(self.JOB_ID)
        self.assertEqual(response.status_code, 200)

    def test_a_failed_cleanup_still_deletes_the_job_and_keeps_the_evidence(self):
        def _no_op_rmtree(path, *args, **kwargs):
            return None  # leaves the staged copy behind

        response, _events, _db = self._delete(
            extra_patches=[patch.object(shutil, "rmtree", _no_op_rmtree)])

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.job_dir.exists())
        staged = list((self.jobs_root / ".trash").iterdir())
        self.assertEqual(len(staged), 1)
        self.assertTrue((staged[0] / "tree" / "tree_original.newick").is_file())

    def test_a_job_directory_outside_JOB_DIR_is_never_touched(self):
        with TemporaryDirectory() as outside:
            stray = Path(outside) / "not-a-job"
            stray.mkdir()
            (stray / "keep.txt").write_text("keep")
            events = []
            job = SimpleNamespace(id=self.JOB_ID, user_id=7,
                                  job_dir=str(stray), status="completed",
                                  metrics={})
            app = Flask(__name__)
            with (
                app.test_request_context(method="DELETE"),
                patch.object(v1, "get_owned_job_or_404", return_value=job),
                patch.object(v1, "db", _RecordingDb(events)),
                patch.object(v1, "_release_rq_job",
                             return_value=(True, "no RQ record")),
                patch.object(Config, "JOB_DIR", self.jobs_root),
            ):
                g.api_user = SimpleNamespace(id=7)
                g.api_token = SimpleNamespace(id=3)
                response = _undecorated(v1.delete_job)(self.JOB_ID)

            self.assertEqual(response.status_code, 200)
            self.assertTrue((stray / "keep.txt").is_file())


class ReleaseRqJobTests(unittest.TestCase):
    """DB, RQ and the filesystem are separate systems.

    `_release_rq_job` reports success only on positive evidence that no
    work-horse can still be writing into the job directory; anything else
    reports failure so the caller declines to delete.
    """

    JOB_ID = "11111111-1111-4111-8111-111111111111"

    def _release(self, fetch):
        with (
            patch("rq.job.Job.fetch", fetch),
            patch("app.workers.queue.get_redis_connection",
                  return_value=MagicMock()),
        ):
            return v1._release_rq_job(self.JOB_ID)

    def test_a_job_rq_has_never_heard_of_is_safe_to_delete(self):
        from rq.exceptions import NoSuchJobError

        released, detail = self._release(
            MagicMock(side_effect=NoSuchJobError("gone")))
        self.assertTrue(released)
        self.assertIn("no RQ record", detail)

    def test_a_terminal_rq_job_is_safe_to_delete(self):
        for status in ("finished", "failed", "stopped", "canceled"):
            with self.subTest(status=status):
                rq_job = MagicMock()
                rq_job.get_status.return_value = status
                released, _detail = self._release(
                    MagicMock(return_value=rq_job))
                self.assertTrue(released)
                rq_job.cancel.assert_not_called()

    def test_a_queued_job_is_cancelled_out_of_the_queue(self):
        rq_job = MagicMock()
        rq_job.get_status.side_effect = ["queued", "canceled"]
        released, detail = self._release(MagicMock(return_value=rq_job))
        self.assertTrue(released)
        rq_job.cancel.assert_called_once_with()
        self.assertIn("queued", detail)

    def test_a_started_job_that_will_not_stop_is_reported_as_not_released(self):
        rq_job = MagicMock()
        rq_job.get_status.return_value = "started"
        with (
            patch.object(v1, "DELETE_STOP_WAIT_SECONDS", 0.05),
            patch.object(v1, "DELETE_STOP_POLL_SECONDS", 0.01),
            patch("rq.command.send_stop_job_command", MagicMock()),
        ):
            released, detail = self._release(MagicMock(return_value=rq_job))
        self.assertFalse(released)
        self.assertIn("still running", detail)

    def test_a_started_job_that_stops_is_released(self):
        rq_job = MagicMock()
        rq_job.get_status.side_effect = ["started", "stopped"]
        with (
            patch.object(v1, "DELETE_STOP_WAIT_SECONDS", 1.0),
            patch.object(v1, "DELETE_STOP_POLL_SECONDS", 0.01),
            patch("rq.command.send_stop_job_command", MagicMock()) as stop,
        ):
            released, detail = self._release(MagicMock(return_value=rq_job))
        self.assertTrue(released)
        self.assertEqual(detail, "stopped")
        stop.assert_called_once()

    def test_a_job_dequeued_during_cancel_is_stopped(self):
        rq_job = MagicMock()
        rq_job.get_status.side_effect = ["queued", "started", "stopped"]
        with (
            patch.object(v1, "DELETE_STOP_WAIT_SECONDS", 1.0),
            patch.object(v1, "DELETE_STOP_POLL_SECONDS", 0.01),
            patch("rq.command.send_stop_job_command", MagicMock()) as stop,
        ):
            released, detail = self._release(MagicMock(return_value=rq_job))
        self.assertTrue(released)
        self.assertEqual(detail, "stopped")
        rq_job.cancel.assert_called_once_with()
        stop.assert_called_once()

    def test_redis_trouble_is_never_reported_as_released(self):
        with patch("app.workers.queue.get_redis_connection",
                   side_effect=OSError("redis down")):
            released, detail = v1._release_rq_job(self.JOB_ID)
        self.assertFalse(released)
        self.assertIn("OSError", detail)


class WorkerDeletionGuardTests(unittest.TestCase):
    JOB_ID = "33333333-3333-4333-8333-333333333333"

    def test_missing_deleted_row_prevents_any_directory_recreation(self):
        from app.workers import tasks

        app = Flask(__name__)
        model = MagicMock()
        model.query.get.return_value = None
        with TemporaryDirectory() as tmp, \
                patch.object(tasks, "get_current_job",
                             return_value=SimpleNamespace(id=self.JOB_ID)), \
                patch("app.create_app", return_value=app), \
                patch("app.models.Job", model), \
                patch.object(Config, "JOB_DIR", Path(tmp)):
            result = tasks.run_phylo_job.__wrapped__({})
            self.assertEqual(result["status"], "cancelled")
            self.assertFalse((Path(tmp) / self.JOB_ID).exists())

    def test_deleting_row_prevents_the_worker_from_beginning(self):
        from app.workers import tasks

        app = Flask(__name__)
        model = MagicMock()
        model.query.get.return_value = SimpleNamespace(status="deleting")
        with TemporaryDirectory() as tmp, \
                patch.object(tasks, "get_current_job",
                             return_value=SimpleNamespace(id=self.JOB_ID)), \
                patch("app.create_app", return_value=app), \
                patch("app.models.Job", model), \
                patch.object(Config, "JOB_DIR", Path(tmp)):
            result = tasks.run_phylo_job.__wrapped__({})
            self.assertEqual(result["status"], "cancelled")
            self.assertFalse((Path(tmp) / self.JOB_ID).exists())


# ---------------------------------------------------------------------------
# CodeRabbit #15 -- SSE lifecycle
# ---------------------------------------------------------------------------

class SseLifecycleTests(unittest.TestCase):
    JOB_ID = "22222222-2222-4222-8222-222222222222"

    def test_limits_come_from_config_not_a_local_policy(self):
        with (
            patch.object(Config, "SSE_MAX_STREAM_SECONDS", 4321),
            patch.object(Config, "SSE_MAX_IDLE_SECONDS", 321),
            patch.object(Config, "SSE_TERMINAL_LINGER_SECONDS", 7),
        ):
            self.assertEqual(v1._sse_limits(), (4321, 321, 7))

    def test_there_is_no_hard_coded_thirty_minute_cap_left(self):
        source = open(v1.__file__).read()
        self.assertNotIn("SSE_MAX_DURATION_SECONDS", source)

    def _stream(self, snapshot_status, **config):
        """Drive the generator and return the events it produced."""
        limits = {"SSE_MAX_STREAM_SECONDS": 21600,
                  "SSE_MAX_IDLE_SECONDS": 600,
                  "SSE_TERMINAL_LINGER_SECONDS": 0}
        limits.update(config)
        snapshot = {"job": {"id": self.JOB_ID, "status": snapshot_status},
                    "log_tails": {}}
        fake_redis = MagicMock()
        fake_redis.pubsub.return_value.get_message.return_value = None
        fake_redis.incr.return_value = 1

        app = Flask(__name__)
        stack = [
            app.test_request_context(method="GET"),
            patch.object(v1, "get_owned_job_or_404",
                         return_value=SimpleNamespace(
                             id=self.JOB_ID, user_id=7, status=snapshot_status)),
            patch("app.api.routes._build_snapshot", return_value=snapshot),
            patch("redis.from_url", return_value=fake_redis),
            patch("app.services.sse_registry.open_stream",
                  return_value=("tok", 1)),
            patch("app.services.sse_registry.close_stream", return_value=0),
            patch.object(v1, "db", MagicMock()),
            patch.object(v1, "Job", _job_model(snapshot_status)),
        ]
        stack.extend(patch.object(Config, k, val) for k, val in limits.items())
        with _nested(stack):
            g.api_user = SimpleNamespace(id=7)
            g.api_token = SimpleNamespace(id=3)
            response = _undecorated(v1.job_events)(self.JOB_ID)
            chunks = list(response.response)
        return "".join(chunks)

    def test_an_already_finished_job_emits_its_snapshot_and_closes(self):
        # Before the fix this loop had no reachable exit: no terminal event was
        # coming and the DB poll is skipped for terminal jobs, so the stream
        # held a request slot until the hard cap.
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                body = self._stream(status)
                self.assertIn("event: snapshot", body)
                self.assertIn(f'"status": "{status}"', body)

    def test_an_idle_running_stream_closes_with_a_timeout_event(self):
        # One second is enough: nothing is published on the channel, so the
        # idle timer is never reset.
        body = self._stream("running", SSE_MAX_IDLE_SECONDS=1)
        self.assertIn("event: timeout", body)
        self.assertIn('"reason": "idle"', body)

    def test_the_lifetime_cap_still_emits_the_documented_timeout_event(self):
        body = self._stream("running", SSE_MAX_STREAM_SECONDS=-1)
        self.assertIn("event: timeout", body)
        self.assertIn('"reason": "max_duration_reached"', body)


def _job_model(status):
    """A stand-in for the Job model whose DB poll always returns `status`."""
    model = MagicMock()
    model.query.get.return_value = SimpleNamespace(id="x", status=status)
    return model


def _nested(context_managers):
    """contextlib.ExitStack as a context manager over a list of CMs."""
    import contextlib

    @contextlib.contextmanager
    def _run():
        with contextlib.ExitStack() as stack:
            for cm in context_managers:
                stack.enter_context(cm)
            yield
    return _run()


if __name__ == "__main__":
    unittest.main()
