"""Regression checks for queue isolation and live monitoring fixes."""

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from flask import Flask
import pytest


@pytest.mark.parametrize('method, target', [('raxml', 'phylo_bulk'), ('fasttree', 'phylo_high')])
def test_recompute_uses_classifier_and_preserves_cross_queue_dedup(method, target):
    from app.workers import queue as module
    queues = {name: Mock() for name in ('phylo_high', 'phylo_bulk')}
    for q in queues.values():
        q.fetch_job.return_value = None
        q.enqueue_call.return_value.id = 'job-id'
    with patch.object(module, 'get_queue', side_effect=queues.__getitem__):
        assert module.enqueue_recompute_job('job-id', {'tree_method': method}) == 'job-id'
    queues[target].enqueue_call.assert_called_once()
    other = 'phylo_high' if target == 'phylo_bulk' else 'phylo_bulk'
    queues[other].enqueue_call.assert_not_called()
    existing = Mock(id='job-id', meta={'tree_state_snapshot_mtime': 123.0})
    existing.get_status.return_value = 'started'
    queues[other].fetch_job.return_value = existing
    queues[target].enqueue_call.reset_mock()
    with patch.object(module, 'get_queue', side_effect=queues.__getitem__):
        assert module.enqueue_recompute_job('job-id', {'tree_method': method}, return_created=True) == ('job-id', False)
        assert module.active_recompute_snapshot_mtime('job-id') == 123.0
    queues[target].enqueue_call.assert_not_called()


def test_monitoring_prefers_staged_recompute_logs(tmp_path):
    from app.monitoring.services import _tool_progress, _job_activity
    app = Flask(__name__)
    app.config['JOB_DIR'] = tmp_path
    job_dir = tmp_path / 'test-job'
    live = job_dir / 'tree'
    staged = job_dir / '.recompute-test' / 'tree'
    live.mkdir(parents=True)
    staged.mkdir(parents=True)
    (live / 'run.raxml.log').write_text('Bootstrap tree #999\n')
    (staged / 'run.raxml.log').write_text('Bootstrap tree #12, private label\n')
    with app.app_context():
        progress = _tool_progress('test-job', {'bootstrap_cap': 100})
        assert progress['current'] == 12
        assert progress['percent'] == 12
        assert 'private' not in str(progress)
        assert '.recompute-test/tree/' in _job_activity('test-job')['file']
        (staged / 'run.raxml.log').unlink()
        assert _tool_progress('test-job', {}) is None


def test_monitoring_uses_rq_length_not_end_index():
    from app.monitoring.services import get_active_jobs
    jobs = [Mock(enqueued_at=datetime.utcnow() - timedelta(seconds=age)) for age in (100, 20)]
    queue = Mock(count=2)
    queue.get_jobs.side_effect = lambda offset, length: jobs[offset:offset + length]
    queue.failed_job_registry.get_job_ids.return_value = []
    with (
        patch('app.workers.queue.get_redis_connection'),
        patch('rq.Worker.all', return_value=[]),
        patch('rq.Queue', return_value=queue),
        patch('rq.registry.StartedJobRegistry') as registry,
        patch('app.monitoring.services._describe_rq_job', return_value={'wait_seconds': 0}),
    ):
        registry.return_value.get_job_ids.return_value = []
        result = get_active_jobs(max_queued=2)
    assert result['available']
    assert len(result['queued']) == 4  # two queues, two entries each
    assert all(99 <= q['oldest_wait_seconds'] <= 102 for q in result['queues'])


def test_scanner_fallback_expires_and_bounds_memory(monkeypatch):
    from app.services import scanner_burst
    clock = Mock(return_value=0)
    monkeypatch.setattr(scanner_burst, 'monotonic', clock)
    fallback = scanner_burst.ScannerBurstFallback(window=60, max_clients=2)
    assert fallback.count('a', True) == 1
    clock.return_value = 61
    assert fallback.count('a') == 0
    for key in ('a', 'b', 'c'):
        fallback.count(key, True)
    assert len(fallback.entries) == 2
