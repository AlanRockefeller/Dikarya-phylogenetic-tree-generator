"""Voucher Sync HTTP surface: login gating, per-user scoping, apply guards.

Runs against the real app factory on an in-memory SQLite database with the
worker, Redis and iNaturalist all mocked. No OpenCV is imported.
"""
import os
import unittest
import uuid
from unittest.mock import patch

import pytest

os.environ.setdefault("ALLOW_SQLITE_FALLBACK", "1")
os.environ.setdefault("SECRET_KEY", "voucher-sync-test-secret")


@pytest.fixture
def app(tmp_path):
    from app.config import config as config_map

    config_class = config_map["development"]
    saved = {name: getattr(config_class, name, None)
             for name in ("SQLALCHEMY_DATABASE_URI", "ERROR_LOG_PATH", "RATELIMIT_ENABLED")}
    had_limit = hasattr(config_class, "RATELIMIT_ENABLED")
    config_class.SQLALCHEMY_DATABASE_URI = "sqlite://"
    config_class.ERROR_LOG_PATH = tmp_path / "errors.log"
    config_class.RATELIMIT_ENABLED = False
    try:
        from app import create_app
        application = create_app("development")
    finally:
        config_class.SQLALCHEMY_DATABASE_URI = saved["SQLALCHEMY_DATABASE_URI"]
        config_class.ERROR_LOG_PATH = saved["ERROR_LOG_PATH"]
        if had_limit:
            config_class.RATELIMIT_ENABLED = saved["RATELIMIT_ENABLED"]
        else:
            delattr(config_class, "RATELIMIT_ENABLED")
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False,
                              INAT_TOKEN_ENCRYPTION_KEY="")
    from app.extensions import db
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def _make_user(email):
    from app.extensions import db
    from app.models import User
    user = User(email=email)
    user.set_password("pw")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = user.get_id()
        sess["_fresh"] = True


def _connect(user, login="mycologist"):
    from app.services.inat_user_credential_service import upsert_credential
    return upsert_credential(user.id, "access-token-secret", inat_login=login,
                             inat_user_id=42, jwt="jwt-secret")


def _scan_run(user, rows, status="completed"):
    from app.extensions import db
    from app.models import VoucherSyncRun
    run = VoucherSyncRun(id=str(uuid.uuid4()), user_id=user.id, kind="scan", status=status,
                         params={"field_id": 1907, "regex": "x", "date_start": "2026-08-01",
                                 "date_end": "2026-08-01"},
                         rows=rows, summary={"update": 1} if rows else None, progress_done=len(rows or []),
                         progress_total=len(rows or []))
    db.session.add(run)
    db.session.commit()
    return run


def _update_row(obs_id, current=None, reason="field_empty"):
    return {"observation_id": obs_id, "url": "u", "taxon": "t", "upload_date": "2026-08-01",
            "detected_voucher": f"BT-{obs_id:03d}", "current_value": current,
            "field_state": "populated" if current else "empty", "action": "update",
            "reason": reason, "ofv_id": 5 if current else None, "raw_qr": None, "raw_ocr": None}


class TestCredentialEncryption:
    def test_round_trip_and_status(self, app):
        from app.services.inat_user_credential_service import (
            credential_status, decrypt_secret, encrypt_secret, get_credential,
        )
        user = _make_user("a@example.org")
        cred = _connect(user)
        assert cred.access_token_enc != "access-token-secret"
        assert decrypt_secret(cred.access_token_enc) == "access-token-secret"
        assert encrypt_secret("x") != "x"
        assert credential_status(user.id)["inat_login"] == "mycologist"
        assert get_credential(user.id).jwt_created_at is not None

    def test_get_user_jwt_reuses_cached_then_mints(self, app):
        from app.services import inat_user_credential_service as svc
        user = _make_user("b@example.org")
        _connect(user)
        with patch.object(svc, "mint_api_jwt") as mint:
            assert svc.get_user_jwt(user.id) == "jwt-secret"
            mint.assert_not_called()
        cred = svc.get_credential(user.id)
        cred.jwt_created_at = 0
        with patch.object(svc, "mint_api_jwt", return_value="fresh-jwt") as mint:
            assert svc.get_user_jwt(user.id) == "fresh-jwt"
            mint.assert_called_once_with("access-token-secret")

    def test_revoked_grant_deletes_credential(self, app):
        from app.services import inat_user_credential_service as svc
        from app.services.inaturalist_oauth_service import InatAuthError
        user = _make_user("c@example.org")
        _connect(user)
        svc.get_credential(user.id).jwt_created_at = 0
        with patch.object(svc, "mint_api_jwt", side_effect=InatAuthError("iNaturalist HTTP 401: nope")):
            with pytest.raises(InatAuthError):
                svc.get_user_jwt(user.id)
        assert svc.get_credential(user.id) is None


class TestPageAndScan:
    def test_page_requires_login(self, app):
        client = app.test_client()
        resp = client.get("/voucher-sync")
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]

    def test_page_renders_for_logged_in_user(self, app):
        client = app.test_client()
        user = _make_user("d@example.org")
        _login(client, user)
        resp = client.get("/voucher-sync")
        assert resp.status_code == 200
        assert b"Voucher Sync" in resp.data
        assert b"Sign in with iNaturalist" in resp.data or b"not configured" in resp.data

    def test_scan_requires_connection(self, app):
        client = app.test_client()
        user = _make_user("e@example.org")
        _login(client, user)
        resp = client.post("/api/voucher-sync/scan", json={"date_start": "2026-08-01"})
        assert resp.status_code == 409

    def test_scan_validates_and_enqueues_only_the_run_id(self, app):
        from app.models import VoucherSyncRun
        client = app.test_client()
        user = _make_user("f@example.org")
        _connect(user)
        _login(client, user)

        resp = client.post("/api/voucher-sync/scan", json={"date_start": "bad"})
        assert resp.status_code == 400

        with patch("app.workers.queue.enqueue_voucher_sync_run", return_value="x") as enq:
            resp = client.post("/api/voucher-sync/scan",
                               json={"date_start": "2026-08-01", "field_id": 1907})
        assert resp.status_code == 202, resp.get_json()
        run_id = resp.get_json()["run_id"]
        enq.assert_called_once_with(run_id, "scan")
        run = VoucherSyncRun.query.get(run_id)
        assert run.user_id == user.id and run.status == "queued"
        assert "token" not in str(run.params)

        # A second scan while one is active is refused.
        with patch("app.workers.queue.enqueue_voucher_sync_run", return_value="x"), \
             patch("app.api.voucher_sync_routes._reconcile_stale"):
            resp = client.post("/api/voucher-sync/scan", json={"date_start": "2026-08-01"})
        assert resp.status_code == 409
        assert resp.get_json()["active_run_id"] == run_id

    def test_enqueue_failure_marks_run_failed(self, app):
        from app.models import VoucherSyncRun
        client = app.test_client()
        user = _make_user("g@example.org")
        _connect(user)
        _login(client, user)
        with patch("app.workers.queue.enqueue_voucher_sync_run", side_effect=RuntimeError("redis down")):
            resp = client.post("/api/voucher-sync/scan", json={"date_start": "2026-08-01"})
        assert resp.status_code == 503
        run = VoucherSyncRun.query.filter_by(user_id=user.id).one()
        assert run.status == "failed"


class TestRunScopingAndApply:
    def test_other_users_run_is_404(self, app):
        owner = _make_user("h@example.org")
        other = _make_user("i@example.org")
        run = _scan_run(owner, [_update_row(1)])
        client = app.test_client()
        _login(client, other)
        with patch("app.api.voucher_sync_routes._live_slice", return_value=([], [], False)):
            assert client.get(f"/api/voucher-sync/runs/{run.id}").status_code == 404
            assert client.post(f"/api/voucher-sync/runs/{run.id}/apply", json={}).status_code == 404
            assert client.get(f"/api/voucher-sync/runs/{run.id}/export.csv").status_code == 404
        assert client.get("/api/voucher-sync/runs/not-a-valid-id").status_code == 404

    def test_run_detail_and_export_for_owner(self, app):
        owner = _make_user("j@example.org")
        run = _scan_run(owner, [_update_row(1)])
        client = app.test_client()
        _login(client, owner)
        with patch("app.api.voucher_sync_routes._live_slice", return_value=([], [], False)):
            resp = client.get(f"/api/voucher-sync/runs/{run.id}")
            body = resp.get_json()
            assert resp.status_code == 200
            assert body["source"] == "db" and body["rows"][0]["detected_voucher"] == "BT-001"
            csv_resp = client.get(f"/api/voucher-sync/runs/{run.id}/export.csv")
        assert csv_resp.status_code == 200
        assert csv_resp.mimetype == "text/csv"
        assert b"observation_id,url,taxon" in csv_resp.data
        assert b"BT-001" in csv_resp.data

    def test_apply_requires_overwrite_confirmation(self, app):
        owner = _make_user("k@example.org")
        _connect(owner)
        run = _scan_run(owner, [_update_row(1), _update_row(2, current="OLD", reason="overwrite_existing")])
        client = app.test_client()
        _login(client, owner)

        with patch("app.workers.queue.enqueue_voucher_sync_run", return_value="x") as enq:
            resp = client.post(f"/api/voucher-sync/runs/{run.id}/apply", json={})
            assert resp.status_code == 409
            assert resp.get_json()["error"] == "confirm_overwrite_required"
            assert resp.get_json()["counts"] == {"total": 2, "overwrite": 1, "ocr": 0}
            enq.assert_not_called()

            # Selecting only the empty-field row needs no confirmation.
            resp = client.post(f"/api/voucher-sync/runs/{run.id}/apply",
                               json={"observation_ids": [1]})
            assert resp.status_code == 202, resp.get_json()
            enq.assert_called_once()

    def test_apply_with_confirmation_creates_child_run(self, app):
        from app.models import VoucherSyncRun
        owner = _make_user("l@example.org")
        _connect(owner)
        run = _scan_run(owner, [_update_row(2, current="OLD", reason="ocr_fallback_overwrite")])
        client = app.test_client()
        _login(client, owner)
        with patch("app.workers.queue.enqueue_voucher_sync_run", return_value="x") as enq:
            resp = client.post(f"/api/voucher-sync/runs/{run.id}/apply",
                               json={"confirm_overwrite": True})
        assert resp.status_code == 202, resp.get_json()
        child_id = resp.get_json()["run_id"]
        enq.assert_called_once_with(child_id, "apply")
        child = VoucherSyncRun.query.get(child_id)
        assert child.kind == "apply" and child.parent_run_id == run.id
        assert child.params["allow_overwrite"] is True
        assert child.params["observation_ids"] == [2]
        assert resp.get_json()["counts"]["ocr"] == 1

    def test_apply_rejects_unfinished_or_empty_preview(self, app):
        owner = _make_user("m@example.org")
        _connect(owner)
        client = app.test_client()
        _login(client, owner)
        running = _scan_run(owner, [_update_row(1)], status="running")
        with patch("app.api.voucher_sync_routes._reconcile_stale"):
            assert client.post(f"/api/voucher-sync/runs/{running.id}/apply", json={}).status_code == 409
        running.status = "completed"
        running.rows = [dict(_update_row(1), action="flag")]
        from app.extensions import db
        db.session.commit()
        resp = client.post(f"/api/voucher-sync/runs/{running.id}/apply", json={})
        assert resp.status_code == 409
        assert "Nothing to apply" in resp.get_json()["error"]

    def test_disconnect_removes_credential(self, app):
        from app.services.inat_user_credential_service import get_credential
        owner = _make_user("n@example.org")
        _connect(owner)
        client = app.test_client()
        _login(client, owner)
        resp = client.post("/voucher-sync/oauth/disconnect")
        assert resp.status_code == 302
        assert get_credential(owner.id) is None
        resp = client.post("/api/voucher-sync/scan", json={"date_start": "2026-08-01"})
        assert resp.status_code == 409


class TestWorkerTask:
    def test_scan_job_persists_rows_and_summary(self, app):
        from app.models import VoucherSyncRun
        from app.workers import voucher_sync_tasks as tasks
        owner = _make_user("o@example.org")
        _connect(owner, login="scanner")
        run_id = _scan_run(owner, None, status="queued").id

        class FakeRedis:
            def __init__(self):
                self.lists = {}
            def rpush(self, key, value):
                self.lists.setdefault(key, []).append(value)
            def expire(self, key, ttl):
                pass
            def exists(self, key):
                return 0
            def delete(self, key):
                pass

        fake_redis = FakeRedis()
        obs = {"id": 7, "taxon": {"name": "Boletus"}, "created_at_details": {"date": "2026-08-01"},
               "observation_photos": [{"position": 0, "photo": {"url": "https://cdn/square.jpg"}}]}

        class FakeClient:
            def __init__(self, jwt):
                assert jwt == "jwt-secret"
            def verify_token(self):
                return {"login": "scanner", "id": 42}
            def fetch_observations(self, login, d1, d2, max_observations=None):
                assert login == "scanner"
                yield obs

        def fake_scan(client, obs_list, **kw):
            row = {"observation_id": 7, "url": "u", "taxon": "Boletus", "upload_date": "2026-08-01",
                   "detected_voucher": "BT-007", "current_value": None, "field_state": "empty",
                   "action": "update", "reason": "field_empty", "ofv_id": None,
                   "raw_qr": "BT-007", "raw_ocr": None}
            kw["on_row"](1, 1, row)
            return [row], False

        with patch.object(tasks, "get_redis_connection", return_value=fake_redis), \
             patch("app.services.voucher_sync_service.INatClient", FakeClient), \
             patch("app.services.voucher_sync_service.scan_observations", side_effect=fake_scan), \
             patch("app.services.voucher_sync_service.ocr_engine_available", return_value=False):
            result = tasks.run_voucher_scan_job(run_id)

        assert result["status"] == "completed"
        stored = VoucherSyncRun.query.get(run_id)
        assert stored.status == "completed"
        assert stored.summary["update"] == 1
        assert stored.rows[0]["detected_voucher"] == "BT-007"
        assert stored.progress_done == 1 and stored.progress_total == 1
        assert any("Authenticated as scanner" in l for l in fake_redis.lists[tasks.run_keys(run_id)["log"]])
        assert len(fake_redis.lists[tasks.run_keys(run_id)["rows"]]) == 1

    def test_scan_job_without_credential_fails_cleanly(self, app):
        from app.models import VoucherSyncRun
        from app.workers import voucher_sync_tasks as tasks
        owner = _make_user("p@example.org")
        run_id = _scan_run(owner, None, status="queued").id

        class FakeRedis:
            def rpush(self, *a): pass
            def expire(self, *a): pass
            def exists(self, *a): return 0
            def delete(self, *a): pass

        with patch.object(tasks, "get_redis_connection", return_value=FakeRedis()):
            result = tasks.run_voucher_scan_job(run_id)
        assert result["status"] == "failed"
        stored = VoucherSyncRun.query.get(run_id)
        assert stored.status == "failed"
        assert "Connect your iNaturalist account" in stored.error


if __name__ == "__main__":
    unittest.main()
