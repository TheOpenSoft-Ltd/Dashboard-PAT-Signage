import json
import logging
import time
from datetime import datetime
from threading import Thread
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from home.models import DSMTask
from home.services.mqtt_service import mqtt_service

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
                playing = DSMTask.objects.filter(status="playing")
                downloaded = DSMTask.objects.filter(status="downloaded")
                logger.info(
                    f"Scheduler check: {playing.count()} playing, {downloaded.count()} downloaded"
                )
                self._start_tasks()
                # self._update_playing_tasks()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            time.sleep(5)

    @staticmethod
    def _get_task_tz(task):
        return (
            ZoneInfo(task.timezone) if task.timezone else ZoneInfo(settings.TIME_ZONE)
        )

    def _start_tasks(self):
        now = timezone.now()
        pending_tasks = DSMTask.objects.filter(status="downloaded")
        for task in pending_tasks:
            if not task.date_started_at or not task.time_started_at:
                continue
            tz = self._get_task_tz(task)
            now_local = now.astimezone(tz)
            logger.info(
                f"[_start_tasks] task={task.dsm_task_id} "
                f"schedule={task.date_started_at} {task.time_started_at} "
                f"tz={task.timezone or settings.TIME_ZONE} "
                f"now_utc={now} now_local={now_local} "
                f"date_cond={task.date_started_at < now_local.date()} "
                f"time_cond={task.date_started_at == now_local.date() and task.time_started_at <= now_local.time()}"
            )
            if task.date_started_at < now_local.date() or (
                task.date_started_at == now_local.date()
                and task.time_started_at <= now_local.time()
            ):
                task.status = "playing"
                task.save(update_fields=["status", "updated_at"])
                logger.info(f"DSMTask started: {task.dsm_task_id}")

    def _update_playing_tasks(self):
        now = timezone.now()
        playing_tasks = DSMTask.objects.filter(status="playing")
        for task in playing_tasks:
            if not task.date_end_at or not task.time_end_at:
                continue
            tz = self._get_task_tz(task)
            now_local = now.astimezone(tz)
            logger.info(
                f"[_update_playing_tasks] task={task.dsm_task_id} "
                f"schedule_end={task.date_end_at} {task.time_end_at} "
                f"tz={task.timezone or settings.TIME_ZONE} "
                f"now_utc={now} now_local={now_local} "
                f"time_cond={task.time_end_at <= now_local.time()} "
                f"date_cond={task.date_end_at <= now_local.date()}"
            )
            if task.time_end_at <= now_local.time():
                if task.date_end_at <= now_local.date():
                    task.status = "completed"
                    task.save(update_fields=["status", "updated_at"])
                    logger.info(f"DSMTask completed: {task.dsm_task_id}")
                    device_id = getattr(settings, "DEVICE_ID", "")
                    if device_id:
                        topic = f"pat-sig/{device_id}/action"
                        payload = json.dumps(
                            {
                                "DSMTaskId": task.dsm_task_id,
                                "dsm_id": task.dsm_id,
                                "status": "completed",
                                "name": task.name,
                            },
                            ensure_ascii=False,
                        )
                        mqtt_service.publish(topic, payload)
                else:
                    task.status = "downloaded"
                    task.save(update_fields=["status", "updated_at"])
                    logger.info(f"DSMTask requeued: {task.dsm_task_id}")


scheduler_service = SchedulerService()
