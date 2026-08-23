"""Regression coverage for ordinary web DB-before-RQ submission ordering."""

from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, g

from app.api import routes


FASTA = ">A\nACGTACGTACGTACGTACGT\n>B\nACGTACGTACGTACGTACGA\n"


def _undecorated(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


class RecordingDb:
    def __init__(self, events, fail_commits=()):
        self.events = events
        self.fail_commits = set(fail_commits)
        self.commit_count = 0
        self.added = []
        self.session = SimpleNamespace(
            add=self.add,
            commit=self.commit,
            rollback=lambda: self.events.append("rollback"),
        )

    def add(self, value):
        self.added.append(value)
        self.events.append("add")

    def commit(self):
        self.commit_count += 1
        self.events.append("commit")
        if self.commit_count in self.fail_commits:
            raise RuntimeError("database unavailable")


def _submit(*, fail_commits=(), enqueue_error=None, prepare_warning=False):
    events = []
    database = RecordingDb(events, fail_commits=fail_commits)
    queued = {}

    def make_job(**kwargs):
        return SimpleNamespace(**kwargs)

    def prepare(params):
        events.append("prepare")
        if prepare_warning:
            params["input_warnings"] = ["same warning"]

    def enqueue(params, **kwargs):
        events.append("enqueue")
        queued.update(kwargs)
        queued["params"] = dict(params)
        if enqueue_error:
            raise enqueue_error
        return kwargs["job_id"]

    app = Flask(__name__)
    with (
        app.test_request_context(method="POST", json={
            "input_type": "sequence",
            "sequence": FASTA,
            "tree_method": "nj",
        }),
        patch.object(routes, "db", database),
        patch.object(routes, "Job", side_effect=make_job),
        patch.object(routes, "prepare_phylo_job_params", side_effect=prepare),
        patch.object(routes, "enqueue_job", side_effect=enqueue),
        patch.object(routes, "current_user", SimpleNamespace(
            is_authenticated=True, id=42,
        )),
    ):
        g.request_id = "test-request"
        response = app.make_response(_undecorated(routes.create_job)())
    return response, events, database, queued


def test_web_commit_precedes_enqueue_and_uses_the_same_uuid():
    response, events, database, queued = _submit(prepare_warning=True)
    row = database.added[0]

    assert response.status_code == 202
    assert events == ["prepare", "add", "commit", "enqueue"]
    assert queued["job_id"] == row.id
    assert queued["prepare"] is False
    assert row.user_id == 42
    assert row.metrics["input_warnings"] == ["same warning"]
    assert response.get_json() == {
        "status": "queued", "job_id": row.id, "warnings": ["same warning"]
    }


def test_web_failed_commit_never_enqueues():
    response, events, database, queued = _submit(fail_commits={1})

    assert response.status_code == 500
    assert "enqueue" not in events
    assert not queued
    assert database.added[0].status == "queued"


def test_web_failed_enqueue_retains_and_marks_the_row_failed():
    response, events, database, _queued = _submit(
        enqueue_error=RuntimeError("redis unavailable")
    )
    row = database.added[0]

    assert response.status_code == 500
    assert events == ["prepare", "add", "commit", "enqueue", "commit"]
    assert row.status == "failed"
    assert row.metrics["enqueue_error"] == "RuntimeError"
    assert "never started" in row.metrics["error"]


def test_web_submission_keeps_the_default_high_priority_queue():
    _response, _events, _database, queued = _submit()

    # No queue override means enqueue_job's established phylo_high default.
    assert "queue_name" not in queued
