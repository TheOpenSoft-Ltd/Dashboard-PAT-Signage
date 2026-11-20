from __future__ import annotations  # PEP 604 (X | None) on Python 3.9

import json
import logging
import mimetypes
import os
import time

import requests
from django.conf import settings

from home.media_download import (
    DownloadError,
    complete_local_file,
    fetch_to_file,
    remote_size,
)
from home.services.mqtt_service import mqtt_service
from home.task_downloads import task_downloads

logger = logging.getLogger(__name__)

# HTTP Content-Type -> (file extension, signage media category).
# The S3 object key carries NO file extension (key = "{dsmId}/{DSMTaskId}"),
# so the media type must be resolved from the Content-Type header that S3
# returns (set at upload via PutObjectCommand ContentType=file.mimetype) —
# never guessed from the URL.
CONTENT_TYPE_MAP = {
    "image/jpeg": ("jpg", "image"),
    "image/jpg": ("jpg", "image"),
    "image/png": ("png", "image"),
    "image/gif": ("gif", "gif"),
    "video/mp4": ("mp4", "video"),
    "video/webm": ("webm", "video"),
    "video/x-msvideo": ("avi", "video"),
    "video/quicktime": ("mov", "video"),
}

_VIDEO_EXTS = {"mp4", "webm", "avi", "mov"}
_ALLOWED_EXTS = _VIDEO_EXTS | {"jpg", "jpeg", "png", "gif"}


def _media_category(ext: str) -> str:
    if ext in _VIDEO_EXTS:
        return "video"
    if ext == "gif":
        return "gif"
    return "image"


def resolve_media_format(content_type: str | None, url: str) -> tuple[str, str]:
    """Resolve (extension, media_type) preferring the HTTP Content-Type header.

    Falls back to a real, known URL extension only if Content-Type is missing
    or unrecognized. Defaults to ("jpg", "image") when nothing is recognizable.
    """
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in CONTENT_TYPE_MAP:
            return CONTENT_TYPE_MAP[ct]
        guessed = mimetypes.guess_extension(ct)
        if guessed:
            ext = guessed.lstrip(".").lower()
            ext = "jpg" if ext in ("jpe", "jpeg") else ext
            if ext in _ALLOWED_EXTS:
                return ext, _media_category(ext)

    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    if ext in _ALLOWED_EXTS:
        ext = "jpg" if ext == "jpeg" else ext
        return ext, _media_category(ext)

    return "jpg", "image"


def download_media(
    url: str, dsm_task_id: str, url_provider=None
) -> tuple[str | None, str | None]:
    """Download media; return (local_relative_path, media_type).

    Streamed to ``<task>.part`` and resumed after drops (media_download.py); the
    file is renamed into place only once it is whole, so the screen never sees a
    half file. A complete copy already on the sign is kept: the backend re-sends
    a task until the sign reports it, and a rollback re-sends an older version —
    neither may download 2 GB again. ``url_provider`` returns the freshest link
    (re-sends carry new ones). media_type is "video" | "image" | "gif", from the
    response Content-Type. Returns (None, None) on failure.
    """
    if not url:
        return None, None
    media_dir = settings.MEDIA_ROOT / "signage"
    media_dir.mkdir(parents=True, exist_ok=True)

    existing = complete_local_file(media_dir, dsm_task_id)
    if existing is not None:
        size = remote_size(url)
        if size is None or size == existing.stat().st_size:
            ext = existing.suffix.lstrip(".").lower()
            logger.info(f"Media for {dsm_task_id} already on the sign: {existing.name}")
            return f"signage/{existing.name}", _media_category(ext)
        logger.info(f"Media for {dsm_task_id} changed on the server: downloading again")
        existing.unlink()

    part = media_dir / f"{dsm_task_id}.part"
    try:
        size, content_type = fetch_to_file(url_provider or (lambda: url), part)
    except DownloadError as e:
        logger.error(f"Failed to download media: {e}")
        return None, None

    ext, media_type = resolve_media_format(content_type, url)
    filename = f"{dsm_task_id}.{ext}"
    filepath = media_dir / filename
    try:
        os.replace(part, filepath)
    except OSError as e:
        logger.error(f"Failed to move media into place at {filepath}: {e}")
        return None, None

    logger.info(f"Downloaded media to {filepath} ({size} bytes, media_type={media_type})")
    return f"signage/{filename}", media_type


# Backend (Dashobard-DSM) status enum (UPPER) -> local TaskStatus value.
STATUS_MAP = {
    "PENDING": "pending",
    "DOWNLOADED": "downloaded",
    "PLAYING": "playing",
    "SKIPING": "skiping",
    "ARCHIVED": "completed",
}


def _report_status(task, status: str):
    """Report a task's resulting status back to the backend (action channel)."""
    device_id = getattr(settings, "DEVICE_ID", "")
    if not device_id:
        return
    action_payload = json.dumps(
        {
            "DSMTaskId": task.dsm_task_id,
            "DSMId": task.dsm_id,
            "status": status,
            "name": task.name,
            # When this happened, by the sign's clock (epoch ms): the backend
            # applies a sign's reports in this order and drops one older than
            # the last it applied (security register I5).
            "at": int(time.time() * 1000),
        },
        ensure_ascii=False,
    )
    mqtt_service.publish_reliable(f"pat-sig/{device_id}/action", action_payload)


def delete_task_media(task):
    """Delete the locally-downloaded media file for a finished task and clear
    its media_local_path. Called when a task becomes "completed" to free space.
    """
    if not task.media_local_path:
        return
    filepath = settings.MEDIA_ROOT / task.media_local_path
    try:
        if filepath.exists():
            filepath.unlink()
            logger.info(f"Deleted media file {filepath}")
        task.media_local_path = None
        task.save(update_fields=["media_local_path", "updated_at"])
    except Exception as e:
        logger.error(f"Failed to delete media {filepath}: {e}")


def _answer_ping(data: dict):
    """Answer the backend's alert channel check (security register I4).

    The backend pings every sign once a minute on /status; this answer on
    /action tells the dashboard the channel alerts travel on works end to end,
    which the RTMP stream status cannot. Sent directly, not through the outbox:
    a pong that cannot go out now is worthless later, and one flushed hours
    afterwards would claim a connection that was not there.
    """
    device_id = getattr(settings, "DEVICE_ID", "")
    if not device_id:
        return
    mqtt_service.publish(
        f"pat-sig/{device_id}/action",
        json.dumps(
            {
                "type": "pong",
                "nonce": data.get("nonce"),
                "sentAt": data.get("sentAt"),
                "at": int(time.time() * 1000),
            }
        ),
        qos=1,
    )


def handle_status_message(payload: str, dsm_id: str = None):
    """Apply a status command pushed by the backend (pat-sig/{deviceId}/status).

    SKIPING is a force-skip whose effect depends on the current status:
      - playing   -> stop now and revert to "downloaded" (can play again next
                     scheduled window)
      - otherwise -> "skiping" (parked; scheduler will not play it)
    Other statuses are applied as-is (mapped from the backend enum).
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in status message: {e}")
        return

    if data.get("type") == "ping":
        _answer_ping(data)
        return

    dsm_task_id = data.get("DSMTaskId")
    raw_status = (data.get("status") or "").upper()
    if not dsm_task_id or not raw_status:
        logger.warning("Status message missing DSMTaskId or status")
        return

    try:
        task = DSMTask.objects.get(dsm_task_id=dsm_task_id)
    except DSMTask.DoesNotExist:
        logger.warning(f"Status message for unknown task {dsm_task_id}")
        return

    if raw_status == "STOP":
        # Manual hard-stop of the clip on screen right now. Unlike SKIPING it
        # does NOT revert to "downloaded" (which the scheduler would replay
        # inside the same window) — it parks the task in "stop" so it stays off
        # screen until the backend re-schedules it. Idempotent: a resent STOP
        # (backend retries until acked) simply re-acks.
        if task.status != "stop":
            task.status = "stop"
            task.save(update_fields=["status", "updated_at"])
            logger.info(f"DSMTask {dsm_task_id} STOP -> stopped (parked off-screen)")
        else:
            logger.info(f"DSMTask {dsm_task_id} STOP (already stopped) -> re-ack")
        # Durable ack so the backend clears its pending-stop and stops resending.
        _report_status(task, "stopped")
        return

    if raw_status == "SKIPING":
        if task.status == "playing":
            # Force-skip while playing: stop the source now, but revert to
            # "downloaded" so the scheduler can play it again next window.
            new_status = "downloaded"
            logger.info(
                f"DSMTask {dsm_task_id} force-skip while playing -> downloaded (stopped)"
            )
        else:
            # Not playing (e.g. downloaded): park it so it will not play.
            new_status = "skiping"
            logger.info(f"DSMTask {dsm_task_id} skip -> skiping")
    else:
        new_status = STATUS_MAP.get(raw_status, raw_status.lower())
        logger.info(f"DSMTask {dsm_task_id} status -> {new_status}")

    task.status = new_status
    task.save(update_fields=["status", "updated_at"])

    # Keep the backend in sync with the resulting status (e.g. a playing task
    # that was force-skipped reports "downloaded" so the dashboard reflects it).
    _report_status(task, new_status)

    if new_status == "completed":
        delete_task_media(task)


ALERT_TYPES = {"ALERTHIGHT", "ALERTMEDIUM", "ALERTLOW"}


def alert_is_active() -> bool:
    """True while an alert task is playing. While active, the scheduler must NOT
    resume PUBLICRELATION tasks (alert has the screen). Backed by the DB so it
    survives a power-cut/reboot.
    """
    return DSMTask.objects.filter(
        status="playing", task_type__in=ALERT_TYPES
    ).exists()


def handle_alert_message(payload: str):
    """Command an alert on the /alert channel.

    action="play" (default): take over the screen NOW. Media is pre-downloaded
        via /data, so /alert carries only the task identity. Any other playing
        task is paused (back to "downloaded") and the alert becomes focused.
    action="clear": the level returned to normal -> stop the alert (back to
        "downloaded") so other tasks can resume.
    The resulting status is reported back to the backend (durably, via the
    outbox) so the backend knows the device actually received the trigger.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in alert message: {e}")
        return

    dsm_task_id = data.get("DSMTaskId")
    action = (data.get("action") or "play").lower()
    if not dsm_task_id:
        logger.warning("Alert message missing DSMTaskId")
        return

    try:
        task = DSMTask.objects.get(dsm_task_id=dsm_task_id)
    except DSMTask.DoesNotExist:
        logger.warning(f"Alert for unknown task {dsm_task_id} (not downloaded?)")
        return

    if action == "clear":
        # Stop the alert; the scheduler may resume PR tasks afterwards.
        task.status = "downloaded"
        task.save(update_fields=["status", "updated_at"])
        logger.info(f"ALERT cleared: {dsm_task_id}")
        # Report cleared as a normal-status ack so the backend can stop resending.
        _report_status(task, "downloaded")
        return

    # action == "play"
    if not task.media_local_path:
        logger.warning(f"Alert task {dsm_task_id} has no downloaded media")
        return

    # Pause any other task currently playing so the alert gets full focus.
    DSMTask.objects.filter(status="playing").exclude(
        dsm_task_id=dsm_task_id
    ).update(status="downloaded")

    task.status = "playing"
    task.save(update_fields=["status", "updated_at"])
    logger.info(f"ALERT task focused (playing now): {dsm_task_id}")
    _report_status(task, "playing")


def _media_url(data: dict) -> str:
    try:
        return data["url"]["url"]
    except (TypeError, KeyError):
        return data.get("url", "") or ""


def apply_task_message(dsm_task_id: str, data: dict | None = None) -> None:
    """Download a task's media and record the task (runs on a download worker).

    The newest message for the task is used — a re-send that arrived while the
    file was coming in carries a fresher link and wins.
    """
    data = task_downloads.latest(dsm_task_id) or data or {}
    name = data.get("name", "")
    task_type = data.get("type")
    media_url = _media_url(data)
    dsm_id = data.get("DSMId") or ""

    def freshest_link() -> str:
        return _media_url(task_downloads.latest(dsm_task_id) or data) or media_url

    local_path, media_type = download_media(media_url, dsm_task_id, freshest_link)

    data = task_downloads.latest(dsm_task_id) or data
    status = "downloaded" if local_path else "pending"
    task, created = DSMTask.objects.update_or_create(
        dsm_task_id=dsm_task_id,
        defaults={
            "dsm_id": dsm_id,
            "name": name,
            "task_type": task_type,
            "media_url": _media_url(data) or media_url,
            "media_local_path": local_path,
            "media_type": media_type or "image",
            "timezone": data.get("timezone"),
            "date_started_at": data.get("dateStartedAt"),
            "time_started_at": data.get("timeStartedAt"),
            "date_end_at": data.get("dateEndAt"),
            "time_end_at": data.get("timeEndAt"),
            "status": status,
        },
    )

    logger.info(f"DSMTask {'created' if created else 'updated'}: {dsm_task_id}")

    # /data only PRE-DOWNLOADS the media; it never starts playback.
    # Playing an alert is commanded separately via the /alert channel.
    if status == "downloaded":
        _report_status(task, "downloaded")


def handle_mqtt_message(sender, topic: str, payload: str, dsm_id: str = None, **kwargs):
    # Status/command channel: pat-sig/{deviceId}/status (vs the /data task feed)
    if topic and topic.endswith("/status"):
        handle_status_message(payload, dsm_id)
        return
    # Alert channel: pat-sig/{deviceId}/alert -> play immediately (override)
    if topic and topic.endswith("/alert"):
        handle_alert_message(payload)
        return
    # Task feed: hand the download to a worker and return at once, so this
    # thread (paho's network loop) stays free for alerts and keepalives.
    try:
        data = json.loads(payload)
        logger.info(f"Processing MQTT message: {data}")
        dsm_task_id = data.get("DSMTaskId")
        if not dsm_task_id:
            logger.warning("No DSMTaskId in message")
            return
        if not data.get("DSMId") and dsm_id:
            data["DSMId"] = dsm_id
        task_downloads.submit(dsm_task_id, data, apply_task_message)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in MQTT message: {e}")
    except Exception as e:
        logger.error(f"Error processing MQTT message: {e}")


from home.models import DSMTask
