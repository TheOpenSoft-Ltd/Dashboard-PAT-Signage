import json
import logging

import requests
from django.conf import settings

from home.services.mqtt_service import mqtt_service

logger = logging.getLogger(__name__)


def get_media_type(media_url: str) -> str:
    if not media_url:
        return "image"
    ext = media_url.split("?")[0].split(".")[-1].lower()
    if ext in ["mp4", "webm", "avi", "mov"]:
        return "video"
    elif ext == "gif":
        return "gif"
    return "image"


def download_media(url: str, dsm_task_id: str) -> str | None:
    if not url:
        return None
    media_dir = settings.MEDIA_ROOT / "signage"
    media_dir.mkdir(parents=True, exist_ok=True)

    ext = url.split("?")[0].split(".")[-1].lower()
    if ext not in ["mp4", "webm", "avi", "mov", "jpg", "jpeg", "png", "gif"]:
        ext = "jpg"

    filename = f"{dsm_task_id}.{ext}"
    filepath = media_dir / filename

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(response.content)
        logger.info(f"Downloaded media to {filepath}")
        return f"signage/{filename}"
    except Exception as e:
        logger.error(f"Failed to download media: {e}")
        return None


def handle_mqtt_message(sender, topic: str, payload: str, dsm_id: str = None, **kwargs):
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

        local_path = download_media(media_url, dsm_task_id)

        status = "downloaded" if local_path else "pending"
        task, created = DSMTask.objects.update_or_create(
            dsm_task_id=dsm_task_id,
            defaults={
                "dsm_id": dsm_id,
                "name": name,
                "task_type": task_type,
                "media_url": media_url,
                "media_local_path": local_path,
                "media_type": get_media_type(media_url),
                "timezone": data.get("timezone"),
                "date_started_at": data.get("dateStartedAt"),
                "time_started_at": data.get("timeStartedAt"),
                "date_end_at": data.get("dateEndAt"),
                "time_end_at": data.get("timeEndAt"),
                "status": status,
            },
        )

        logger.info(f"DSMTask {'created' if created else 'updated'}: {dsm_task_id}")

        if status == "downloaded":
            device_id = getattr(settings, "DEVICE_ID", "")
            if device_id:
                action_topic = f"pat-sig/{device_id}/action"
                action_payload = json.dumps({
                    "DSMTaskId": dsm_task_id,
                    "dsm_id": dsm_id,
                    "status": "downloaded",
                    "name": name,
                }, ensure_ascii=False)
                mqtt_service.publish(action_topic, action_payload)

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in MQTT message: {e}")
    except Exception as e:
        logger.error(f"Error processing MQTT message: {e}")


from home.models import DSMTask
