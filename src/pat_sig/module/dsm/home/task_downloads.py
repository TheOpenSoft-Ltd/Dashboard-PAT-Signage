"""Run task downloads off the MQTT thread — never two for the same task.

MQTT messages are handled on paho's network thread. Downloading there (as the
app used to) meant that while a file was coming in the sign handled no alert,
sent no keepalive (the broker dropped it after ~90 s) and queued every other
message. A 2 GB promo clip over 4G takes up to two hours.

So a /data message only records the task here and returns; a small pool of
worker threads does the download and the bookkeeping. The backend re-sends a
pending task every 10 s with a fresh link until the sign reports it: a re-send
of a task that is already queued or downloading does not start a second
download — it only hands the running one the newest link. Several workers, so
a small alert image never waits behind a large PR clip.
"""
from __future__ import annotations  # PEP 604 (X | None) on Python 3.9

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

MAX_PARALLEL = 3


class TaskDownloads:
    def __init__(self, max_parallel: int = MAX_PARALLEL):
        self._lock = threading.Lock()
        self._latest: Dict[str, dict] = {}  # task id -> newest message, while in flight
        self._max_parallel = max_parallel
        self._pool: Optional[ThreadPoolExecutor] = None

    def submit(self, task_id: str, message: dict, job: Callable[[str], None]) -> bool:
        """Queue ``job(task_id)``. Returns False when the task is already in flight
        (its newest message is kept for the running job to use)."""
        with self._lock:
            if task_id in self._latest:
                self._latest[task_id] = message
                logger.info(f"DSMTask {task_id} already downloading: newer link kept")
                return False
            self._latest[task_id] = message
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=self._max_parallel, thread_name_prefix="task-download"
                )
        self._pool.submit(self._run, task_id, job)
        return True

    def latest(self, task_id: str) -> Optional[dict]:
        """The newest message for a task in flight (its link is the freshest)."""
        with self._lock:
            return self._latest.get(task_id)

    def in_flight(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._latest

    def _run(self, task_id: str, job: Callable[[str], None]) -> None:
        try:
            job(task_id)
        except Exception:
            logger.exception(f"DSMTask {task_id}: download job failed")
        finally:
            with self._lock:
                self._latest.pop(task_id, None)
            try:
                # Each worker thread has its own DB connection; do not leak them.
                from django.db import connection

                connection.close()
            except Exception:
                pass

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
        if pool:
            pool.shutdown(wait=wait)


task_downloads = TaskDownloads()
