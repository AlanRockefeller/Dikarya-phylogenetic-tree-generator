"""A completed local-only MycoMap tree can request its missing NCBI export."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask

from app.config import Config
from app.main import bp


def test_manual_retry_marks_job_then_queues_reconciliation(tmp_path):
    job_id = "ytqe"
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    (job_dir / "input_info.json").write_text(json.dumps({
        "mycomap_blast_url": "https://mycomap.com/genetics/blast-search/r42/",
        "sequence_metadata": [
            {"source": "mycomap", "hit_source": "local", "name": "local tip"},
        ],
    }))
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(bp)
    record = SimpleNamespace(status="completed", metrics={})
    redis = MagicMock()
    redis.set.return_value = True
    queue = MagicMock()
    database = MagicMock()

    with (
        patch.object(Config, "JOB_DIR", tmp_path),
        patch("app.main.routes.check_job_access", return_value=(record, None, None)),
        patch("app.main.routes.db", database),
        patch("app.workers.queue.get_redis_connection", return_value=redis),
        patch("app.workers.queue.get_queue", return_value=queue),
    ):
        response = app.test_client().post(f"/job/{job_id}/retry-mycomap-ncbi")

    assert response.status_code == 302
    assert record.metrics["mycomap_blast_rerun"]["ncbi_download_failed"] is True
    assert record.metrics["mycomap_blast_rerun"]["ncbi_fallback_local_only"] is True
    database.session.commit.assert_called_once()
    queue.enqueue.assert_called_once()
    assert queue.enqueue.call_args.args[1] == job_id


def test_manual_retry_releases_lock_after_failed_commit(tmp_path):
    job_id = "ytqe"
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    (job_dir / "input_info.json").write_text(json.dumps({
        "mycomap_blast_url": "https://mycomap.com/genetics/blast-search/r42/",
        "sequence_metadata": [{"source": "mycomap", "hit_source": "local"}],
    }))
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(bp)
    record = SimpleNamespace(status="completed", metrics={})
    redis = MagicMock()
    redis.set.return_value = True
    redis.delete.side_effect = RuntimeError("Redis unavailable")
    database = MagicMock()
    database.session.commit.side_effect = [RuntimeError("commit failed"), None]

    with (
        patch.object(Config, "JOB_DIR", tmp_path),
        patch("app.main.routes.check_job_access", return_value=(record, None, None)),
        patch("app.main.routes.db", database),
        patch("app.workers.queue.get_redis_connection", return_value=redis),
    ):
        response = app.test_client().post(f"/job/{job_id}/retry-mycomap-ncbi")

    assert response.status_code == 503
    assert record.metrics == {}
    database.session.rollback.assert_called_once()
    redis.delete.assert_called_once()
