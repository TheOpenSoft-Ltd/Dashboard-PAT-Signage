from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from home.models import DSMTask


ALERT_TYPES = ["ALERTHIGHT", "ALERTMEDIUM", "ALERTLOW"]


def home(request):
    # An alert that is playing always takes priority over a PUBLICRELATION task.
    running_task = (
        DSMTask.objects.filter(status="playing", task_type__in=ALERT_TYPES)
        .order_by("-updated_at")
        .first()
    )
    if not running_task:
        running_task = (
            DSMTask.objects.filter(status="playing")
            .order_by("date_started_at", "time_started_at")
            .first()
        )
    if not running_task:
        # Nothing playing: the pushed default banner, before the built-in
        # default.png. The one received most recently is current (a rollback
        # re-sends the previous version, which makes it the most recent). It is
        # never "playing", so alerts and PR tasks take the screen exactly as
        # before, and the scheduler ignores it (no dates).
        running_task = (
            DSMTask.objects.filter(task_type="DEFAULT")
            .exclude(media_local_path__isnull=True)
            .exclude(media_local_path="")
            .order_by("-updated_at")
            .first()
        )
    if running_task:
        media_type = running_task.media_type
        if running_task.media_local_path:
            media_url = settings.MEDIA_URL + running_task.media_local_path
        elif running_task.media_url:
            media_url = running_task.media_url
        else:
            media_url = getattr(settings, "SIGNAGE_MEDIA_URL", "")
    else:
        media_url = getattr(settings, "SIGNAGE_MEDIA_URL", "")
        media_type = getattr(settings, "SIGNAGE_MEDIA_TYPE", "")

    video_format = getattr(settings, "SIGNAGE_VIDEO_FORMAT", "mp4")
    video_muted = getattr(settings, "SIGNAGE_VIDEO_MUTED", False)

    context = {
        "media_url": media_url,
        "media_type": media_type,
        "video_format": video_format,
        "video_muted": video_muted,
    }

    if request.headers.get("Accept") == "application/json":
        return JsonResponse(context)

    return render(request, "home/home.html", context)
