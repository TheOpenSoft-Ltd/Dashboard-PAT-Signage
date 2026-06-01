import json
import logging
import mimetypes

import requests
from django.conf import settings

from home.services.mqtt_service import mqtt_service

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


def download_media(url: str, dsm_task_id: str) -> tuple[str | None, str | None]:
    """Download media; return (local_relative_path, media_type).

    media_type is one of "video" | "image" | "gif", derived from the response
    Content-Type. Returns (None, None) on any failure.
    """
    if not url:
        return None, None
    media_dir = settings.MEDIA_ROOT / "signage"
    media_dir.mkdir(parents=True, exist_ok=True)

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to download media: {e}")
        return None, None

    ext, media_type = resolve_media_format(response.headers.get("Content-Type"), url)

    filename = f"{dsm_task_id}.{ext}"
    filepath = media_dir / filename
    try:
        with open(filepath, "wb") as f:
            f.write(response.content)
    except Exception as e:
        logger.error(f"Failed to write media to {filepath}: {e}")
        return None, None

    logger.info(f"Downloaded media to {filepath} (media_type={media_type})")
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


def handle_mqtt_message(sender, topic: str, payload: str, dsm_id: str = None, **kwargs):
    # Status/command channel: pat-sig/{deviceId}/status (vs the /data task feed)
    if topic and topic.endswith("/status"):
        handle_status_message(payload, dsm_id)
        return
    # Alert channel: pat-sig/{deviceId}/alert -> play immediately (override)
    if topic and topic.endswith("/alert"):
        handle_alert_message(payload)
        return
    try:
        data = json.loads(payload)
        logger.info(f"Processing MQTT message: {data}")

        dsm_task_id = data.get("DSMTaskId")
        name = data.get("name", "")
        task_type = data.get("type")

        try:
            media_url = data["url"]["url"]
        except (TypeError, KeyError):
            media_url = data.get("url", "")

        dsm_id = data.get("DSMId") or dsm_id or ""

        if not dsm_task_id:
            logger.warning("No DSMTaskId in message")
            return

        local_path, media_type = download_media(media_url, dsm_task_id)

        status = "downloaded" if local_path else "pending"
        task, created = DSMTask.objects.update_or_create(
            dsm_task_id=dsm_task_id,
            defaults={
                "dsm_id": dsm_id,
                "name": name,
                "task_type": task_type,
                "media_url": media_url,
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

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in MQTT message: {e}")
    except Exception as e:
        logger.error(f"Error processing MQTT message: {e}")


from home.models import DSMTask
