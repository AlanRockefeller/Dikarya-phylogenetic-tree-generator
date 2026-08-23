import atexit
import os
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
# Registry cleanup is cheap for Dikarya's two queues. Keep this short enough
# that an expired abandoned execution gains its public AbandonedJobError result
# promptly even if an older deployment still exports a 600-second interval.
MAX_RQ_MAINTENANCE_INTERVAL = 120


class HeartbeatWorker(Worker):
    """
    A specific worker that writes a heartbeat file periodically and reaps
    abandoned heartbeats left by previously-crashed workers.
    """
    def __init__(self, queues, name=None, default_result_ttl=None, connection=None,
                 exc_handler=None, default_worker_ttl=None, job_class=None,
                 queue_class=None, heartbeat_interval=10, worker_dir=None):
        super().__init__(
            queues,
            name=name,
            default_result_ttl=default_result_ttl,
            connection=connection,
            exc_handler=exc_handler,
            default_worker_ttl=default_worker_ttl,
            job_class=job_class,
            queue_class=queue_class,
        )
        self.heartbeat_interval = heartbeat_interval
        # Caller must supply the worker dir; we no longer carry a misleading
        # hardcoded default like "/var/phylojobs/workers".
        if not worker_dir:
            raise ValueError("worker_dir is required")
        self.worker_dir = Path(worker_dir)
        self.last_heartbeat = 0
        self._last_sweep = 0
        self._cleanup_ran = False
        self._heartbeat_thread = None

        # Ensure directory exists
        if not self.worker_dir.exists():
            try:
                self.worker_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.error(f"Could not create worker dir {self.worker_dir}: {e}")

        # Remove our own heartbeat on any orderly exit. Deliberately no
        # SIGTERM/SIGINT handler of our own: RQ's Worker.work() calls
        # _install_signal_handlers() (rq 2.6.1, worker.py) and binds both
        # signals to request_stop, so anything registered here is replaced the
        # moment the worker starts and never runs. It also would not be wanted
        # if it did -- the old handler re-raised through SIG_DFL, which kills
        # the process outright and skips RQ's warm shutdown of the running job.
        # The three real paths are covered without it:
        #   warm/cold shutdown -> work() returns or raises SystemExit, and
        #                         run_worker_with_heartbeat()'s finally clause
        #                         plus this atexit hook both clean up;
        #   SIGKILL / segfault -> uncatchable either way; the corpse is reaped
        #                         by _maybe_reap_stale_heartbeats() below.
        atexit.register(self.clean_up_heartbeat)

    def run_maintenance_tasks(self):
        """Let RQ identify abandoned executions, then reconcile their DB rows.

        RQ's StartedJobRegistry cleanup writes the public ``AbandonedJobError``
        failure result used as positive interruption evidence.  Running
        reconciliation after the normal maintenance pass means a whole-worker
        restart is recoverable once RQ's heartbeat expiry has elapsed, without
        guessing that a merely missing traceback means the workhorse died.
        """
        super().run_maintenance_tasks()
        try:
            from app.services.job_reconcile_service import reconcile_job_statuses

            reconciled = reconcile_job_statuses()
            for entry in reconciled:
                logger.info(
                    "RQ maintenance reconciliation: %s %s -> %s (rq=%s)",
                    entry["job_id"], entry["from_status"], entry["action"],
                    entry["rq_status"],
                )
        except Exception:
            # RQ maintenance itself has already succeeded.  Bookkeeping must
            # never prevent this worker from returning to its queue loop.
            logger.exception("Post-maintenance job reconciliation failed")
            # Swallowing the error is only safe if the session is left usable.
            # This context is pushed once for the worker's whole lifetime, so a
            # session abandoned mid-transaction would poison every later
            # maintenance pass with PendingRollbackError.
            try:
                from app.extensions import db
                db.session.rollback()
            except Exception:
                logger.exception("Could not roll back the worker session")

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
        self._ensure_heartbeat_thread()

        super().work(burst=burst, logging_level=logging_level, date_format=date_format, log_format=log_format, max_jobs=max_jobs, with_scheduler=with_scheduler, **kwargs)

    def _ensure_heartbeat_thread(self):
        """Start the file-heartbeat loop once per worker.

        Production calls work() exactly once, but tests and any future reuse
        can call it again, and each call used to leak another daemon loop
        touching the same file forever. The loop never returns on its own, so a
        thread that is no longer alive died on something unrecoverable; start a
        replacement rather than silently losing the heartbeat.
        """
        import threading

        existing = self._heartbeat_thread
        if existing is not None and existing.is_alive():
            return existing
        thread = threading.Thread(
            target=self._file_heartbeat_loop,
            name=f"dikarya-heartbeat-{self.name}",
            daemon=True,
        )
        self._heartbeat_thread = thread
        thread.start()
        return thread

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
        self.worker_dir.mkdir(parents=True, exist_ok=True)
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
        # A restart kills the previous work horse mid-job, and the killed process
        # never reaches the handler that would mark its DB row failed. Reconcile
        # against RQ on startup so those rows don't sit at 'running' forever
        # holding SSE streams (and therefore request slots) open.
        try:
            from app.services.job_reconcile_service import reconcile_job_statuses
            reconciled = reconcile_job_statuses()
            if reconciled:
                logger.info(
                    "Startup reconciliation: %s job(s) reconciled against RQ state.",
                    len(reconciled),
                )
                for entry in reconciled[:10]:
                    logger.info(
                        "  %s %s -> %s (rq=%s)",
                        entry["job_id"], entry["from_status"],
                        entry["action"], entry["rq_status"],
                    )
        except Exception:
            # Never block the worker from starting over a bookkeeping step.
            logger.exception("Startup job reconciliation skipped")

        conn = get_redis_connection()
        queues = [get_queue("phylo_high"), get_queue("phylo_bulk"), get_queue("voucher_sync")]
        # RQ must first expire and clean an abandoned StartedJobRegistry entry
        # before reconciliation has positive AbandonedJobError evidence. A
        # short default bounds restart recovery latency without guessing from
        # missing metadata. Operators may still override it explicitly.
        configured_maintenance_interval = HeartbeatWorker._get_int_env(
            "RQ_MAINTENANCE_INTERVAL", MAX_RQ_MAINTENANCE_INTERVAL
        )
        maintenance_interval = max(
            1, min(configured_maintenance_interval, MAX_RQ_MAINTENANCE_INTERVAL)
        )
        if configured_maintenance_interval > MAX_RQ_MAINTENANCE_INTERVAL:
            logger.warning(
                "RQ_MAINTENANCE_INTERVAL=%s exceeds Dikarya's %ss abandoned-job "
                "recovery cap; using %ss",
                configured_maintenance_interval,
                MAX_RQ_MAINTENANCE_INTERVAL,
                maintenance_interval,
            )
        result_ttl = HeartbeatWorker._get_int_env("RQ_RESULT_TTL", 86400)  # 1 day default
        worker = HeartbeatWorker(queues, connection=conn,
                                 worker_dir=app.config.get("WORKER_DIR", "var/workers"), default_result_ttl=result_ttl)

        worker.maintenance_interval = maintenance_interval

        # Cleanup on exit
        try:
            worker.work(with_scheduler=True)
        finally:
            worker.clean_up_heartbeat()
