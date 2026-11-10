"""Regressions observed in the October 28 production-log review."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import blast_service as blast, ncbi_pacing
from app.services.fasta_input_service import propose_fasta_repair

spec = importlib.util.spec_from_file_location("review_digest", Path(__file__).parents[1] / "scripts/dikarya_log_digest.py")
digest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(digest)


def test_escaped_user_agent_preserves_the_trailing_diagnostics():
    line = r'192.0.2.1 - - [27/Oct/2026:19:19:01 +0000] "GET /tree HTTP/1.0" 200 42 "-" "\"Mozilla/5.0\"" 4273 req=950a41671eb5 noise=-'
    match = digest.ACCESS_RE.fullmatch(line)
    assert match and match["micros"] == "4273" and match["req"] == "950a41671eb5"
    assert digest.ACCESS_RE.match(line + " garbage") is None


def test_dynamic_ids_are_grouped_only_in_known_route_positions():
    assert digest.normalize_path('/api/job/abcd/download/tree/newick') == '/api/job/<id>/download/tree/newick'
    assert digest.normalize_path('/api/background/Secret_Task-123?query=private') == '/api/background/<task_id>'
    assert digest.normalize_path('/job/00000000-0000-4000-8000-000000000000/view') == '/job/<id>/view'
    assert digest.normalize_path('/taxonomy/fungi') == '/taxonomy/fungi'


def test_info_operations_do_not_disappear_from_the_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(digest, 'LOG_DIR', tmp_path)
    (tmp_path / 'error.log').write_text(
        '[2026-10-27 19:19:01] [INFO] event=client.resource_load_failed Browser failure pathname=/tree message=/static/site.css fingerprint=abc visibility=hidden client=192.0.2.1 [req=123456789abc user=anon]\n'
        '[2026-10-27 19:19:02] [INFO] event=background_request.completed kind=blast status=504 duration_ms=239000 [req=123456789abc user=anon]\n'
        '[2026-10-27 19:19:03] [INFO] event=import.source_summary operation=mycomap_org source=ncbi expected=50 fetched=40 omitted=10\n')
    result = digest.analyze_operations(datetime(2026, 10, 27), datetime(2026, 10, 28))
    assert result['assets']['/static/site.css']['clients'] == {'client:192.0.2.1'}
    assert result['background'][('blast', 504)] == [239.0]
    assert result['imports'][('mycomap_org', 'ncbi')]['omitted'] == 10


def test_nginx_scope_and_coverage_are_explicit(monkeypatch):
    access = ('192.0.2.1 - - [27/Oct/2026:12:00:00 +0000] "GET /amanitaceae/pages/?section+Validae HTTP/2.0" 403 12 "-" "browser" host="dikarya.us" server="dikarya.us"\n')
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=access))
    monkeypatch.setattr(digest.subprocess, 'run', run)
    counts, coverage = digest.analyze_nginx(datetime(2026, 10, 27), datetime(2026, 10, 28))
    assert counts[('access', '403', 'GET /amanitaceae/pages/')] == 1
    assert 'app-access-all' in run.call_args_list[0].args[0]
    assert 'WARNING partial historical coverage' in coverage[0]


def test_blast_retry_resumes_the_same_rid_after_a_timeout(tmp_path, monkeypatch):
    config = SimpleNamespace(BLAST_CACHE_DIR=tmp_path)
    submit = Mock(return_value=('RID12345', 0))
    poll = Mock(side_effect=[TimeoutError('busy'), None])
    monkeypatch.setattr(blast, '_submit_blast_request', submit)
    monkeypatch.setattr(blast, '_poll_blast', poll)
    monkeypatch.setattr(blast, '_fetch_blast_results', Mock(return_value={'accessions': [], 'hit_details': []}))
    monkeypatch.setattr(blast, 'fetch_fasta_for_accessions', Mock(return_value=''))
    with pytest.raises(TimeoutError):
        blast.blast_from_sequence('ACGT' * 20, config)
    assert len(list(tmp_path.glob('*.pending.json'))) == 1
    blast.blast_from_sequence('ACGT' * 20, config)
    submit.assert_called_once()
    assert poll.call_args.args[0] == 'RID12345'
    assert not list(tmp_path.glob('*.pending.json'))


def test_shared_cooldown_is_rechecked_after_a_wait(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds
    monkeypatch.setattr(ncbi_pacing, 'time', SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    connection = Mock()
    connection.eval.side_effect = [350, 2000, 0]
    monkeypatch.setattr(ncbi_pacing, 'connection', lambda: connection)
    ncbi_pacing.wait_for_slot(blast.NCBI_EFETCH_URL, deadline=10)
    assert sleeps == [0.35, 2.0]
    assert connection.eval.call_count == 3


def test_shared_cooldown_cannot_spend_a_request_budget(monkeypatch):
    connection = Mock()
    connection.eval.return_value = -120001
    monkeypatch.setattr(ncbi_pacing, 'connection', lambda: connection)
    sleep = Mock()
    monkeypatch.setattr(ncbi_pacing, 'time', SimpleNamespace(monotonic=lambda: 0, sleep=sleep))
    with pytest.raises(TimeoutError):
        ncbi_pacing.wait_for_slot(blast.NCBI_EFETCH_URL, deadline=30)
    sleep.assert_not_called()


def test_missing_first_header_is_a_preview_and_keeps_every_base():
    text = 'specimen.name\nACGTNN\n>second\nTTTT\n'
    proposal = propose_fasta_repair(text)
    assert proposal['valid']
    assert proposal['sequence'] == '>' + text
    assert text.startswith('specimen.name')
    invalid = propose_fasta_repair('instructions\nthis is not DNA\n')
    assert not invalid['valid'] and not invalid['sequence'].startswith('>')


def test_background_failure_logs_work_time_and_returns_original_correlation(monkeypatch, caplog):
    from flask import Flask, g, jsonify
    from app.services import background_requests as background
    from app.api import routes
    from app.extensions import limiter
    app = Flask(__name__)
    app.config.update(SECRET_KEY='test', RATELIMIT_ENABLED=False)
    limiter.init_app(app)
    app.register_blueprint(routes.bp, url_prefix='/api')
    records = {}
    connection = SimpleNamespace(set=lambda key, value, **kwargs: records.update({key: value}))
    class Immediate:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(background, '_redis', lambda: connection)
    monkeypatch.setattr(background, 'threading', SimpleNamespace(Thread=Immediate))
    caplog.set_level('INFO')
    with app.test_request_context('/api/blast', method='POST'):
        g.request_id = '123456789abc'
        task_id = background.start('blast', lambda: (jsonify(error='busy'), 504))
    record = json.loads(next(iter(records.values())))
    assert record['request_id'] == '123456789abc' and record['duration_ms'] >= 0
    assert 'event=background_request.completed' in caplog.text
    assert 'status=504' in caplog.text and task_id in caplog.text
    monkeypatch.setattr(background, 'fetch', lambda _id: record)
    response = app.test_client().get('/api/background/' + task_id)
    assert response.status_code == 504
    assert response.headers['X-Origin-Request-Id'] == '123456789abc'
    assert 'owner' not in response.get_json()


def test_only_identified_facebook_bridge_errors_are_demoted(monkeypatch, caplog):
    from flask import Flask
    from flask_login import LoginManager
    from app.api import routes
    from app.extensions import limiter
    from app.workers import queue
    app = Flask(__name__)
    app.config.update(SECRET_KEY='test', RATELIMIT_ENABLED=False)
    login = LoginManager(app)
    login.user_loader(lambda _id: None)
    limiter.init_app(app)
    app.register_blueprint(routes.bp, url_prefix='/api')
    monkeypatch.setattr(queue, 'get_redis_connection', lambda: SimpleNamespace(set=lambda *a, **k: True))
    caplog.set_level('INFO')
    payload = dict(event='window_error', pathname='/tree',
                   message='Uncaught Error invoking postMessage: Java object is gone',
                   stack='at sendDataToNative (iabjs://navigation_performance_logger_android:1:23)', visibility='hidden')
    client = app.test_client()
    assert client.post('/api/log/client', json=payload, headers={'User-Agent': 'Mozilla [FB_IAB/FB4A;]'}).status_code == 200
    record = next(row for row in caplog.records if 'event=client.' in row.message)
    assert record.levelname == 'INFO' and 'origin=facebook_injected' in record.message
    caplog.clear()
    payload['stack'] = 'at renderTree (https://dikarya.us/static/js/viewer.js:1:23)'
    client.post('/api/log/client', json=payload, headers={'User-Agent': 'Mozilla [FB_IAB/FB4A;]'})
    record = next(row for row in caplog.records if 'event=client.' in row.message)
    assert record.levelname == 'ERROR' and 'origin=browser' in record.message


def test_disk_growth_estimate_uses_elapsed_time_not_a_single_free_space_read(monkeypatch):
    from app.monitoring import services
    from collections import deque
    monkeypatch.setattr(services, '_DISK_SAMPLES', deque(maxlen=61))
    assert services._disk_growth_detail(10_000, 0) == (0, None)
    growth, exhaustion = services._disk_growth_detail(4_000, 600)
    assert growth == 10 and exhaustion == 400


def test_key_rotation_does_not_inherit_a_previous_mycomap_refusal(monkeypatch):
    from app.services import mycomap_org_service as org
    values = {}
    connection = SimpleNamespace(ttl=lambda key: 600 if key in values else -2,
                                 set=lambda key, value, **kwargs: values.update({key: value}))
    monkeypatch.setattr(ncbi_pacing, 'connection', lambda: connection)
    monkeypatch.setenv('MYCOMAP_ORG_API_KEY', 'old-key')
    org._remember_search_refusal()
    assert org.search_availability()['available'] is False
    monkeypatch.setenv('MYCOMAP_ORG_API_KEY', 'new-key')
    status = org.search_availability()
    assert status['available'] is True and 'new-key' not in json.dumps(status)
