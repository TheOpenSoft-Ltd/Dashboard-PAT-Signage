import logging
import time
from datetime import datetime, timedelta
from threading import Thread

from django.db.models import Q
from django.utils import timezone

from home.models import DSMTask

logger = logging.getLogger(__name__)


class SchedulerService:
    _instance = None
    _running = False
    _thread = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def start(self):
        if self._running:
            logger.info("Scheduler already running")
            return
        self._running = True
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Scheduler started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Scheduler stopped")

    def _run(self):
        while self._running:
            try:
                now = timezone.now()
                running = DSMTask.objects.filter(status="running")
                pending = DSMTask.objects.filter(status="downloaded", started_at__lte=now)
                logger.info(f"Scheduler check: {running.count()} running, {pending.count()} pending")
                self._check_pending_tasks()
                self._check_running_tasks()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            time.sleep(5)

    def _check_pending_tasks(self):
        now = timezone.now()
        pending_tasks = DSMTask.objects.filter(
            status="downloaded",
            started_at__lte=now,
        )
        for task in pending_tasks:
            task.status = "running"
            task.save(update_fields=["status", "updated_at"])
            logger.info(f"DSMTask started: {task.dsm_task_id}")

    def _check_running_tasks(self):
        now = timezone.now()
        running_tasks = DSMTask.objects.filter(status="running")
        for task in running_tasks:
            if task.ended_at and task.ended_at <= now:
                task.status = "completed"
                task.save(update_fields=["status", "updated_at"])
                logger.info(f"DSMTask completed: {task.dsm_task_id}")


scheduler_service = SchedulerService()
