import threading

from .service import SYSTEM_ACTOR


class MaintenanceWorker:
    """Background dependency maintenance.

    It drains legacy released results batch by batch and then periodically
    recomputes active releases to catch calibrations whose due date has
    passed. Every batch runs in short independent transactions, so the
    worker never holds the write lock and new releases are not disturbed.
    """

    def __init__(self, service, interval_seconds=300, batch_size=100):
        self.service = service
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._last_report = None

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="dependency-maintenance", daemon=True
            )
            self._thread.start()

    def stop(self, timeout=5):
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def _run(self):
        while not self._stop.is_set():
            try:
                report = self.service.run_maintenance(
                    actor=SYSTEM_ACTOR, batch_size=self.batch_size
                )
                self._last_report = report
            except Exception:
                # Maintenance must never kill the process; the next tick
                # retries the same batch.
                pass
            # Wake early on stop so shutdown is prompt.
            self._stop.wait(self.interval_seconds)

    @property
    def last_report(self):
        return self._last_report
