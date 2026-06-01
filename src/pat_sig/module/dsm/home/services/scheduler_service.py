import json
import logging
import time
from datetime import datetime
from threading import Thread
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from home.handlers import alert_is_active, delete_task_media
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
                # Retry any reports queued while the backend/broker was down.
                mqtt_service.flush_outbox()
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
    def _report_action(task, status: str, date: str = None):
        """Report a task state transition back to the DSM backend over MQTT.

        Backend (Dashobard-DSM mqtt.service) maps:
        downloaded -> DOWNLOADED, playing -> PLAYING, requeue -> DOWNLOADED,
        completed -> ARCHIVED. For "requeue" and "completed" (a play round just
        finished) `date` carries the local date of that round so the backend
        can record an accurate DSM_history row.
        """
        device_id = getattr(settings, "DEVICE_ID", "")
        if not device_id:
            return
        topic = f"pat-sig/{device_id}/action"
        body = {
            "DSMTaskId": task.dsm_task_id,
            "DSMId": task.dsm_id,
            "status": status,
            "name": task.name,
        }
        if date:
            body["date"] = date
        payload = json.dumps(body, ensure_ascii=False)
        mqtt_service.publish_reliable(topic, payload)

    def _start_tasks(self):
        now = timezone.now()

        # Priority gate: while an alert is playing it owns the screen — do not
        # start/resume any PUBLICRELATION task until the alert is cleared.
        if alert_is_active():
            return

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
                continue

            # Missed window: task stayed "downloaded" but today's play window has
            # already ended (e.g. the process was down during the 5-min window),
            # so it never went "playing". Roll it forward anyway so it still
            # produces a history row and is not stuck forever.
            window_passed = current_date > task.date_end_at or (
                current_date <= task.date_end_at
                and current_time >= task.time_end_at
            )
            if not window_passed:
                continue

            # Idempotency: only roll a genuinely-stale task forward ONCE per
            # local day. updated_at >= today means it was already handled today
            # (by a previous tick or by _update_playing_tasks), so skip to avoid
            # re-firing requeue/history every 5s.
            already_handled_today = (
                task.updated_at is not None
                and task.updated_at.astimezone(tz).date() >= current_date
            )
            if already_handled_today:
                continue

            if current_date >= task.date_end_at:
                task.status = "completed"
                task.save(update_fields=["status", "updated_at"])
                logger.info(
                    f"DSMTask completed (missed window): {task.dsm_task_id}"
                )
                self._report_action(
                    task, "completed", date=current_date.isoformat()
                )
                delete_task_media(task)
            else:
                # Has future days: keep "downloaded" but bump updated_at to mark
                # today handled, then record a history row for the missed round.
                task.save(update_fields=["updated_at"])
                logger.info(
                    f"DSMTask requeued (missed window): {task.dsm_task_id}"
                )
                self._report_action(
                    task, "requeue", date=current_date.isoformat()
                )

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

                self._report_action(
                    task, "completed", date=current_date.isoformat()
                )
                delete_task_media(task)

            # Not last day yet -> wait for next day's schedule
            else:
                task.status = "downloaded"
                task.save(update_fields=["status", "updated_at"])

                logger.info(f"DSMTask requeued: {task.dsm_task_id}")

                # "requeue" (distinct from a first-time "downloaded") tells the
                # backend a play round just finished -> record a history row.
                self._report_action(
                    task, "requeue", date=current_date.isoformat()
                )


scheduler_service = SchedulerService()
