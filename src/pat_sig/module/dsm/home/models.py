from django.db import models


class MediaType(models.TextChoices):
    VIDEO = "video", "Video"
    IMAGE = "image", "Image"
    GIF = "gif", "GIF"


class TaskType(models.TextChoices):
    ALERTHIGHT = "ALERTHIGHT", "Alert High"
    ALERTMEDIUM = "ALERTMEDIUM", "Alert Medium"
    ALERTLOW = "ALERTLOW", "Alert Low"
    PUBLICRELATION = "PUBLICRELATION", "Public Relation"


class TaskStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    DOWNLOADED = "downloaded", "Downloaded"
    PLAYING = "playing", "playing"
    SKIPING = "skiping", "skiping"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class DSMTask(models.Model):
    dsm_task_id = models.CharField(max_length=255, primary_key=True)
    dsm_id = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    task_type = models.CharField(
        max_length=20, choices=TaskType.choices, default=TaskType.PUBLICRELATION
    )
    media_url = models.URLField(max_length=500, blank=True, null=True)
    media_local_path = models.CharField(max_length=500, blank=True, null=True)
    media_type = models.CharField(
        max_length=10, choices=MediaType.choices, default=MediaType.IMAGE
    )
    timezone = models.CharField(max_length=50, blank=True, null=True)
    date_started_at = models.DateField(blank=True, null=True)
    time_started_at = models.TimeField(blank=True, null=True)
    date_end_at = models.DateField(blank=True, null=True)
    time_end_at = models.TimeField(blank=True, null=True)
    status = models.CharField(
        max_length=20, choices=TaskStatus.choices, default=TaskStatus.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)

    class Meta:
        db_table = "dsm_task"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.status})"


class OutboxReport(models.Model):
    """Durable queue of device -> backend MQTT reports.

    Every report is persisted here first; a row is deleted only after it is
    successfully published. Because it lives in the local SQLite DB it survives
    process restarts, so reports are never lost while the backend/broker is
    unreachable — they are resent on reconnect / next scheduler tick.
    """

    topic = models.CharField(max_length=255)
    payload = models.TextField()
    attempts = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "outbox_report"
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.topic} (#{self.pk})"
