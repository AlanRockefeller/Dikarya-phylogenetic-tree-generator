"""In-place mutation of JSON columns has to reach the database.

Several worker paths read `db_job.metrics`, mutate the dict in place and assign
the *same object* back. For a plain db.JSON column SQLAlchemy compares the new
value with the committed one, finds them equal (they are literally the same
object), records no history and emits no UPDATE -- so legitimate metrics
changes were silently dropped. MutableDict/MutableList track the mutation
itself; the PostgreSQL column type is unchanged, so no migration is involved.
"""

import unittest

from flask import Flask

from app.extensions import db
from app.models import ApiToken, Job, User


class JsonMutationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
        self.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        self.app.config["SECRET_KEY"] = "test"
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        user = User(email="t@example.com", password_hash="x")
        db.session.add(user)
        db.session.commit()
        self.user_id = user.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    # Sentinel, so "no metrics argument" and "metrics is explicitly NULL" are
    # distinguishable. Defaulting None to {} meant the NULL test below never
    # actually stored NULL, and so proved nothing about the case it names.
    _UNSET = object()

    def _job(self, metrics=_UNSET):
        job = Job(id="job-1", user_id=self.user_id, status="running",
                  job_dir="/tmp/job-1", input_type="pasted_sequence",
                  metrics={} if metrics is self._UNSET else metrics)
        db.session.add(job)
        db.session.commit()
        return job

    def _reload(self):
        db.session.expunge_all()
        return db.session.get(Job, "job-1")

    def test_mutating_and_reassigning_the_same_dict_persists(self):
        # The exact worker pattern: `metrics = db_job.metrics or {}` ...
        # `db_job.metrics = metrics`.
        job = self._job({"tree_method": "raxml"})
        metrics = job.metrics or {}
        metrics["completed_at"] = "2026-08-23T00:00:00"
        job.metrics = metrics
        db.session.commit()

        self.assertEqual(
            self._reload().metrics,
            {"tree_method": "raxml", "completed_at": "2026-08-23T00:00:00"},
        )

    def test_in_place_mutation_without_reassignment_persists(self):
        job = self._job({"a": 1})
        job.metrics["b"] = 2
        db.session.commit()

        self.assertEqual(self._reload().metrics, {"a": 1, "b": 2})

    def test_in_place_deletion_persists(self):
        job = self._job({"a": 1, "reconciled_reason": "gone"})
        metrics = job.metrics
        metrics.pop("reconciled_reason", None)
        job.metrics = metrics
        db.session.commit()

        self.assertEqual(self._reload().metrics, {"a": 1})

    def test_replacing_with_a_fresh_dict_still_persists(self):
        job = self._job({"a": 1})
        job.metrics = dict(job.metrics or {}, b=2)
        db.session.commit()

        self.assertEqual(self._reload().metrics, {"a": 1, "b": 2})

    def test_metrics_starting_from_null_can_be_populated(self):
        job = self._job(None)
        # The column really holds NULL before the update is exercised.
        self.assertIsNone(self._reload().metrics)
        job = db.session.get(Job, "job-1")
        metrics = job.metrics or {}
        metrics["started_at"] = "now"
        job.metrics = metrics
        db.session.commit()

        self.assertEqual(self._reload().metrics, {"started_at": "now"})

    def test_token_scopes_track_in_place_mutation(self):
        token = ApiToken(user_id=self.user_id, name="t", token_hash="h" * 64,
                         token_prefix="dikarya_abc", scopes=["jobs:read"])
        db.session.add(token)
        db.session.commit()

        token_id = token.id
        token.scopes.append("jobs:write")
        db.session.commit()

        db.session.expunge_all()
        reloaded = db.session.get(ApiToken, token_id)
        self.assertEqual(reloaded.scopes, ["jobs:read", "jobs:write"])
        self.assertTrue(reloaded.has_scope("jobs:write"))

    def test_metrics_remain_plain_json_serializable(self):
        import json
        job = self._job({"a": 1})
        job.metrics["nested"] = {"b": [1, 2]}
        db.session.commit()
        self.assertEqual(
            json.loads(json.dumps(self._reload().metrics)),
            {"a": 1, "nested": {"b": [1, 2]}},
        )


if __name__ == "__main__":
    unittest.main()
