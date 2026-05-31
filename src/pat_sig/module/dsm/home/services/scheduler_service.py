import json
import logging
import time
from datetime import datetime
from threading import Thread
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from home.models import DSMTask, TaskType
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
                self._start_tasks()
                self._update_playing_tasks()
            except Exception as e:
                logger.exception(f"Scheduler error: {e}")

            time.sleep(5)

    @staticmethod
    def _get_task_tz(task):
        return (
            ZoneInfo(task.timezone) if task.timezone else ZoneInfo(settings.TIME_ZONE)
        )

    @staticmethod
    def _report_action(task, status: str):
        """Report a task state transition back to the DSM backend over MQTT.

        Backend (Dashobard-DSM mqtt.service) maps:
        downloaded -> DOWNLOADED, playing -> PLAYING, completed -> ARCHIVED.
        """
        device_id = getattr(settings, "DEVICE_ID", "")
        if not device_id:
            return
        topic = f"pat-sig/{device_id}/action"
        payload = json.dumps(
            {
                "DSMTaskId": task.dsm_task_id,
                "DSMId": task.dsm_id,
                "status": status,
                "name": task.name,
            },
            ensure_ascii=False,
        )
        mqtt_service.publish(topic, payload)

    def _start_tasks(self):
        now = timezone.now()

        tasks = DSMTask.objects.filter(status__in=["downloaded"])

        for task in tasks:
            if (
                not task.date_started_at
                or not task.date_end_at
                or not task.time_started_at
                or not task.time_end_at
            ):
                continue

            tz = self._get_task_tz(task)
            now_local = now.astimezone(tz)

            current_date = now_local.date()
            current_time = now_local.time()

            date_in_range = task.date_started_at <= current_date <= task.date_end_at

            time_in_range = task.time_started_at <= current_time < task.time_end_at

            should_play = date_in_range and time_in_range

            logger.info(
                f"[START CHECK] "
                f"task={task.dsm_task_id} "
                f"current_date={current_date} "
                f"current_time={current_time} "
                f"date_range={task.date_started_at} -> {task.date_end_at} "
                f"time_range={task.time_started_at} -> {task.time_end_at} "
                f"should_play={should_play}"
            )

            if should_play:
                task.status = "playing"
                task.save(update_fields=["status", "updated_at"])

                logger.info(f"DSMTask started: {task.dsm_task_id}")

                self._report_action(task, "playing")

    def _update_playing_tasks(self):
        now = timezone.now()

        tasks = DSMTask.objects.filter(
            status="playing", task_type=TaskType.PUBLICRELATION
        )

        for task in tasks:
            if (
                not task.date_started_at
                or not task.date_end_at
                or not task.time_started_at
                or not task.time_end_at
            ):
                continue

            tz = self._get_task_tz(task)
            now_local = now.astimezone(tz)

            current_date = now_local.date()
            current_time = now_local.time()

            date_in_range = task.date_started_at <= current_date <= task.date_end_at

            time_in_range = task.time_started_at <= current_time < task.time_end_at

            should_play = date_in_range and time_in_range

            logger.info(
                f"[PLAYING CHECK] "
                f"task={task.dsm_task_id} "
                f"current_date={current_date} "
                f"current_time={current_time} "
                f"date_range={task.date_started_at} -> {task.date_end_at} "
                f"time_range={task.time_started_at} -> {task.time_end_at} "
                f"should_play={should_play}"
            )

            if should_play:
                continue

            # Last day finished
            if current_date > task.date_end_at or (
                current_date == task.date_end_at and current_time >= task.time_end_at
            ):
                task.status = "completed"
                task.save(update_fields=["status", "updated_at"])

                logger.info(f"DSMTask completed: {task.dsm_task_id}")

                self._report_action(task, "completed")

            # Not last day yet -> wait for next day's schedule
            else:
                task.status = "downloaded"
                task.save(update_fields=["status", "updated_at"])

                logger.info(f"DSMTask requeued: {task.dsm_task_id}")

                self._report_action(task, "downloaded")


scheduler_service = SchedulerService()
