from django.shortcuts import render
from django.conf import settings

from home.models import DSMTask


def home(request):
    running_task = DSMTask.objects.filter(status="running").order_by("started_at").first()

    if running_task and running_task.media_local_path:
        media_url = settings.MEDIA_URL + running_task.media_local_path
        media_type = running_task.media_type
    else:
        media_url = getattr(settings, "SIGNAGE_MEDIA_URL", "")
        media_type = getattr(settings, "SIGNAGE_MEDIA_TYPE", "")

    video_format = getattr(settings, "SIGNAGE_VIDEO_FORMAT", "mp4")
    video_muted = getattr(settings, "SIGNAGE_VIDEO_MUTED", False)

    return render(request, "home/home.html", {
        "media_url": media_url,
        "media_type": media_type,
        "video_format": video_format,
        "video_muted": video_muted,
    })
