import os
import time
import socket
import logging
from pathlib import Path
from rq import Worker

logger = logging.getLogger(__name__)

class HeartbeatWorker(Worker):
    """
    A specific worker that writes a heartbeat file periodically.
    """
    def __init__(self, queues, name=None, default_result_ttl=None, connection=None,
                 exc_handler=None, default_worker_ttl=None, job_class=None,
                 queue_class=None, heartbeat_interval=10, worker_dir="/var/phylojobs/workers"):
        super().__init__(queues, name, default_result_ttl, connection,
                         exc_handler, default_worker_ttl, job_class, queue_class)
        self.heartbeat_interval = heartbeat_interval
        self.worker_dir = Path(worker_dir)
        self.last_heartbeat = 0
        
        # Ensure directory exists
        if not self.worker_dir.exists():
            try:
                self.worker_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.error(f"Could not create worker dir {self.worker_dir}: {e}")

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
            except Exception as e:
                logger.error(f"Heartbeat file error: {e}")
            time.sleep(self.heartbeat_interval)

    def touch_heartbeat_file(self):
        # file name: <hostname>.<pid>.<name>.heartbeat ?
        # Or just self.name (which is usually uuid)
        if not self.worker_dir.exists():
             return
             
        filepath = self.worker_dir / f"{self.name}.heartbeat"
        filepath.touch()
        
    def clean_up_heartbeat(self):
        filepath = self.worker_dir / f"{self.name}.heartbeat"
        if filepath.exists():
            filepath.unlink()

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
