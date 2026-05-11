import atexit
import os
import signal
import time
import socket
import logging
from pathlib import Path
from rq import Worker

logger = logging.getLogger(__name__)

# How long a .heartbeat file can sit untouched before we consider it
# abandoned and reap it. Well past the "dead" threshold (5 min) used by
# the dashboard, so we never delete a heartbeat that any living worker
# could still be racing to update.
STALE_HEARTBEAT_AGE_SECONDS = 3600  # 1 hour
# How often the in-process reaper sweeps the worker dir.
STALE_HEARTBEAT_SWEEP_INTERVAL = 300  # 5 minutes


class HeartbeatWorker(Worker):
    """
    A specific worker that writes a heartbeat file periodically and reaps
    abandoned heartbeats left by previously-crashed workers.
    """
    def __init__(self, queues, name=None, default_result_ttl=None, connection=None,
                 exc_handler=None, default_worker_ttl=None, job_class=None,
                 queue_class=None, heartbeat_interval=10, worker_dir=None):
        super().__init__(queues, name, default_result_ttl, connection,
                         exc_handler, default_worker_ttl, job_class, queue_class)
        self.heartbeat_interval = heartbeat_interval
        # Caller must supply the worker dir; we no longer carry a misleading
        # hardcoded default like "/var/phylojobs/workers".
        if not worker_dir:
            raise ValueError("worker_dir is required")
        self.worker_dir = Path(worker_dir)
        self.last_heartbeat = 0
        self._last_sweep = 0
        self._cleanup_ran = False

        # Ensure directory exists
        if not self.worker_dir.exists():
            try:
                self.worker_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.error(f"Could not create worker dir {self.worker_dir}: {e}")

        # Always try to remove our own heartbeat on shutdown, including
        # SIGTERM (what systemd sends), SIGINT (Ctrl-C), and normal exit.
        # SIGKILL and segfaults can't be caught -- those corpses are reaped
        # later by the in-process sweeper below.
        atexit.register(self.clean_up_heartbeat)
        try:
            signal.signal(signal.SIGTERM, self._signal_cleanup)
            signal.signal(signal.SIGINT, self._signal_cleanup)
        except (ValueError, OSError):
            # signal() only works in the main thread; fine to skip otherwise.
            pass

    def _signal_cleanup(self, signum, frame):
        self.clean_up_heartbeat()
        # Re-raise with the default handler so RQ's own shutdown still runs.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    def work(self, burst=False, logging_level="INFO", date_format="%Y-%m-%d %H:%M:%S", log_format="%(asctime)s %(levelname)s %(name)s: %(message)s", max_jobs=None, with_scheduler=False, **kwargs):
        """Override work to start heartbeat loop or hook into it."""
        # RQ's work loop is blocking. We can override `register_birth` or `monitor_work_horse`
        # But RQ doesn't make it super easy to run a side thread without custom loop.
        # simpler: override `heartbeat` method if available (used for redis), 
        # or just hook into the loop. 
        # Actually, RQ workers have a `work` loop that calls `play_work_horse`.
        # We can implement a method that updates the file.
        
        # We'll rely on the main loop calling `self.register_birth()` and `self.set_state()`.
        # But those update Redis. We want a file.
        # Let's wrap `register_birth` and `set_shutdown_requested_date` or similar?
        # A simple way is to extend `perform_job`? No, that's for jobs.
        
        # We can spawn a thread in __init__?
        import threading
        t = threading.Thread(target=self._file_heartbeat_loop, daemon=True)
        t.start()
        
        super().work(burst=burst, logging_level=logging_level, date_format=date_format, log_format=log_format, max_jobs=max_jobs, with_scheduler=with_scheduler, **kwargs)

    @staticmethod
    def _get_int_env(name: str, default: int) -> int:
        v = os.getenv(name, "")
        v = v.strip() if isinstance(v, str) else ""
        if not v:
            return default
        try:
            return int(v)
        except ValueError:
            return default


    def _file_heartbeat_loop(self):
        while True:
            try:
                self.touch_heartbeat_file()
                self._maybe_reap_stale_heartbeats()
            except Exception as e:
                logger.error(f"Heartbeat file error: {e}")
            time.sleep(self.heartbeat_interval)

    def _maybe_reap_stale_heartbeats(self):
        """Delete .heartbeat files older than STALE_HEARTBEAT_AGE_SECONDS.

        Runs at most once every STALE_HEARTBEAT_SWEEP_INTERVAL seconds so
        we don't stat the dir on every tick. Crash-killed siblings get
        cleaned up by whichever worker is still alive.
        """
        now = time.time()
        if now - self._last_sweep < STALE_HEARTBEAT_SWEEP_INTERVAL:
            return
        self._last_sweep = now
        if not self.worker_dir.exists():
            return
        cutoff = now - STALE_HEARTBEAT_AGE_SECONDS
        for hb_file in self.worker_dir.glob("*.heartbeat"):
            try:
                if hb_file.stat().st_mtime < cutoff:
                    hb_file.unlink()
                    logger.info("Reaped stale heartbeat: %s", hb_file.name)
            except FileNotFoundError:
                # Another worker beat us to it; fine.
                continue
            except Exception as e:
                logger.warning("Could not reap %s: %s", hb_file, e)

    def touch_heartbeat_file(self):
        if not self.worker_dir.exists():
            return
        filepath = self.worker_dir / f"{self.name}.heartbeat"
        filepath.touch()

    def clean_up_heartbeat(self):
        # Idempotent: safe to call from atexit + signal handler + finally.
        if self._cleanup_ran:
            return
        self._cleanup_ran = True
        filepath = self.worker_dir / f"{self.name}.heartbeat"
        try:
            filepath.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Could not clean up heartbeat %s: %s", filepath, e)

def run_worker_with_heartbeat(app):
    """
    Entry point to run this worker.
    """
    from app.workers.queue import get_queue
    from app.workers.queue import get_redis_connection
    
    with app.app_context():
        conn = get_redis_connection()
        # Default queue
        queue = get_queue() # returns Queue object
        maintenance_interval = HeartbeatWorker._get_int_env("RQ_MAINTENANCE_INTERVAL", 600)
        result_ttl = HeartbeatWorker._get_int_env("RQ_RESULT_TTL", 86400)  # 1 day default
        # We need to pass queues list
        worker = HeartbeatWorker([queue], connection=conn, 
                                 worker_dir=app.config.get("WORKER_DIR", "var/workers"), default_result_ttl=result_ttl)

        worker.maintenance_interval = maintenance_interval
        
        # Cleanup on exit
        try:
            worker.work()
        finally:
            worker.clean_up_heartbeat()
