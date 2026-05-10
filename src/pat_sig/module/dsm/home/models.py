import uuid
from enum import EnumType

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
    RUNNING = "running", "Running"
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
    started_at = models.DateTimeField(blank=True, null=True)
    ended_at = models.DateTimeField(blank=True, null=True)
    status = models.CharField(
        max_length=20, choices=TaskStatus.choices, default=TaskStatus.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "dsm_task"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.status})"
