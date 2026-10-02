"""
LiveClass — the central record of a scheduled/ongoing/finished class.

This model holds the *permanent* facts about a class:
    - who scheduled it (teacher_id from Auth Server)
    - when it is scheduled
    - its duration
    - its status
    - the LiveKit room it belongs to

It does NOT hold:
    - participant state (ClassParticipant, later)
    - attendance (Attendance, later)
    - chat, polls, recordings (own models, later)
    - LiveKit room state (LiveKit holds that)
"""

import uuid

from django.db import models
from django.db.models import Q


class LiveClassStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    LIVE = "live", "Live"
    ENDED = "ended", "Ended"
    CANCELLED = "cancelled", "Cancelled"


def generate_room_suffix() -> str:
    """Short, URL-safe, unique suffix for a LiveKit room name."""
    return uuid.uuid4().hex[:6]


class LiveClass(models.Model):
    # ---------------------------------------------------------------------
    # Ownership (Auth Server user)
    # ---------------------------------------------------------------------
    teacher_id = models.BigIntegerField(
        db_index=True,
        help_text="user_id of the teacher on the Auth Server",
    )
    teacher_name = models.CharField(
        max_length=255,
        help_text="Snapshot of the teacher's display name at creation time",
    )

    # ---------------------------------------------------------------------
    # Class metadata
    # ---------------------------------------------------------------------
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")

    scheduled_at = models.DateTimeField(
        db_index=True,
        help_text="When the class is scheduled to start (stored in UTC)",
    )
    duration_minutes = models.PositiveIntegerField(
        default=60,
        help_text="Planned duration in minutes",
    )

    # ---------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------
    status = models.CharField(
        max_length=16,
        choices=LiveClassStatus.choices,
        default=LiveClassStatus.SCHEDULED,
        db_index=True,
    )

    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    # ---------------------------------------------------------------------
    # LiveKit room
    # ---------------------------------------------------------------------
    room_name = models.CharField(
        max_length=64,
        unique=True,
        blank=True,
        help_text="LiveKit room name. Auto-generated on first save.",
    )

    # ---------------------------------------------------------------------
    # Bookkeeping
    # ---------------------------------------------------------------------
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "classes_liveclass"
        ordering = ["-scheduled_at"]
        indexes = [
            models.Index(fields=["teacher_id", "scheduled_at"], name="cls_teacher_sched_idx"),
            models.Index(fields=["status", "scheduled_at"], name="cls_status_sched_idx"),
        ]
        constraints = [
            # A class cannot end before it starts
            models.CheckConstraint(
                check=Q(ended_at__isnull=True)
                | Q(started_at__isnull=True)
                | Q(ended_at__gte=models.F("started_at")),
                name="cls_ended_after_started",
            ),
            # Duration must be sane
            models.CheckConstraint(
                check=Q(duration_minutes__gte=1) & Q(duration_minutes__lte=600),
                name="cls_duration_between_1_and_600",
            ),
        ]

    def __str__(self) -> str:
        return f"[{self.id}] {self.title} ({self.status})"

    # ---------------------------------------------------------------------
    # Save hook — auto-generate room_name after we have an id
    # ---------------------------------------------------------------------
    def save(self, *args, **kwargs):
        if not self.room_name:
            # We need the id to make the room human-readable, so save first
            # with a temporary blank then set it. To avoid an extra UPDATE
            # in the common case, only do this if the object has no pk yet.
            if self.pk is None:
                super().save(*args, **kwargs)
                self.room_name = f"class-{self.pk}-{generate_room_suffix()}"
                # Save only the room_name field; avoids re-running
                # auto_now on updated_at.
                type(self).objects.filter(pk=self.pk).update(room_name=self.room_name)
                return
            self.room_name = f"class-{self.pk}-{generate_room_suffix()}"
        super().save(*args, **kwargs)

class ParticipantRole(models.TextChoices):
    TEACHER = "teacher", "Teacher"
    STUDENT = "student", "Student"


class ParticipantStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    LEFT = "left", "Left"
    REMOVED = "removed", "Removed"
    BANNED = "banned", "Banned"


class ClassParticipant(models.Model):
    """
    One row per (class, user).

    Created on first join (or explicitly when enrollment is added later).
    Reused across reconnects. Removed/banned users keep their row so we
    have an audit trail; `status` distinguishes them from active users.
    """

    live_class = models.ForeignKey(
        LiveClass,
        on_delete=models.CASCADE,
        related_name="participants",
    )

    # Auth Server user identity
    user_id = models.BigIntegerField(db_index=True)
    name = models.CharField(max_length=255)
    role = models.CharField(
        max_length=16,
        choices=ParticipantRole.choices,
    )

    status = models.CharField(
        max_length=16,
        choices=ParticipantStatus.choices,
        default=ParticipantStatus.ACTIVE,
        db_index=True,
    )

    # First join this session
    joined_at = models.DateTimeField(auto_now_add=True)
    # Last leave (null while active)
    left_at = models.DateTimeField(null=True, blank=True)

    # Number of times this user has joined this class (resets on leave only
    # when we explicitly decide to; incremented on every join).
    join_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "classes_classparticipant"
        constraints = [
            models.UniqueConstraint(
                fields=["live_class", "user_id"],
                name="cls_participant_unique_per_class",
            ),
        ]
        indexes = [
            models.Index(
                fields=["live_class", "status"],
                name="cls_part_class_status_idx",
            ),
            models.Index(
                fields=["live_class", "role"],
                name="cls_part_class_role_idx",
            ),
        ]
        ordering = ["joined_at"]

    def __str__(self) -> str:
        return f"[{self.live_class_id}] {self.role}:{self.user_id} ({self.status})"
class AttendanceStatus(models.TextChoices):
    PRESENT = "present", "Present"
    PARTIAL = "partial", "Partial"
    ABSENT = "absent", "Absent"


class Attendance(models.Model):
    """
    One row per (class, user). Sums all sessions for that pair.

    `sessions` is a list of {"joined_at": iso, "left_at": iso|null}.
    If the last session has left_at == null, the user is currently present.
    """

    live_class = models.ForeignKey(
        LiveClass,
        on_delete=models.CASCADE,
        related_name="attendance",
    )
    user_id = models.BigIntegerField(db_index=True)
    name = models.CharField(max_length=255)
    role = models.CharField(max_length=16, choices=ParticipantRole.choices)

    first_joined_at = models.DateTimeField(null=True, blank=True)
    last_left_at = models.DateTimeField(null=True, blank=True)

    # Denormalized total from `sessions`. Kept in sync by the recorder.
    total_duration_seconds = models.IntegerField(default=0)

    # List of {"joined_at": iso_str, "left_at": iso_str|null}
    sessions = models.JSONField(default=list)

    class Meta:
        db_table = "classes_attendance"
        constraints = [
            models.UniqueConstraint(
                fields=["live_class", "user_id"],
                name="cls_attendance_unique_per_class",
            ),
        ]
        indexes = [
            models.Index(
                fields=["live_class", "user_id"],
                name="cls_att_class_user_idx",
            ),
            models.Index(
                fields=["user_id"],
                name="cls_att_user_idx",
            ),
        ]
        ordering = ["first_joined_at"]

    def __str__(self) -> str:
        return f"[{self.live_class_id}] user={self.user_id} secs={self.total_duration_seconds}"

class MessageVisibility(models.TextChoices):
    CLASS = "class", "Class"
    TEACHER_ONLY = "teacher_only", "Teacher Only"


class MessageType(models.TextChoices):
    TEXT = "text", "Text"
    SYSTEM = "system", "System"       # reserved for announcements / system events
    POLL = "poll", "Poll"             # reserved for Block 14


class ClassMessage(models.Model):
    """
    A single chat message inside a class.

    Persisted to PostgreSQL. Broadcast over WebSocket at creation.
    Soft-deleted by teachers for moderation.
    """

    live_class = models.ForeignKey(
        LiveClass,
        on_delete=models.CASCADE,
        related_name="messages",
    )

    # Sender identity (Auth Server user)
    user_id = models.BigIntegerField(db_index=True)
    name = models.CharField(max_length=255)
    role = models.CharField(max_length=16, choices=ParticipantRole.choices)

    text = models.TextField()

    message_type = models.CharField(
        max_length=16,
        choices=MessageType.choices,
        default=MessageType.TEXT,
    )
    visibility = models.CharField(
        max_length=16,
        choices=MessageVisibility.choices,
        default=MessageVisibility.CLASS,
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Soft delete (teacher moderation)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "classes_classmessage"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["live_class", "-created_at"],
                name="cls_msg_class_created_idx",
            ),
            models.Index(
                fields=["live_class", "visibility"],
                name="cls_msg_class_vis_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"[{self.live_class_id}] {self.user_id}: {self.text[:30]!r}"


class RecordingStatus(models.TextChoices):
    STARTING = "starting", "Starting"
    ACTIVE = "active", "Active"
    STOPPING = "stopping", "Stopping"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    ABORTED = "aborted", "Aborted"


class ClassRecording(models.Model):
    """
    Metadata for one recording of a class.

    The video file itself lives in S3-compatible storage (uploaded by
    LiveKit Egress). This model stores only the orchestration facts.
    """

    live_class = models.ForeignKey(
        LiveClass,
        on_delete=models.CASCADE,
        related_name="recordings",
    )

    # Set when the recording finishes; may be empty while active.
    storage_key = models.CharField(max_length=512, blank=True, default="")
    bucket = models.CharField(max_length=255, blank=True, default="")
    layout = models.CharField(max_length=32, blank=True, default="")

    # LiveKit Egress id (unique per egress on the LiveKit server).
    egress_id = models.CharField(
        max_length=128,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
    )

    status = models.CharField(
        max_length=16,
        choices=RecordingStatus.choices,
        default=RecordingStatus.STARTING,
        db_index=True,
    )

    started_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)

    duration_seconds = models.IntegerField(null=True, blank=True)
    file_size_bytes = models.BigIntegerField(null=True, blank=True)

    # Last error from LiveKit Egress, if any.
    error_message = models.TextField(blank=True, default="")

    # Filled by Block 15 with a presigned URL when requested.
    # Left null here; no plain-text URL is stored.
    playback_url = models.URLField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "classes_classrecording"
        ordering = ["-started_at"]
        indexes = [
            models.Index(
                fields=["live_class", "-started_at"],
                name="cls_rec_class_started_idx",
            ),
            models.Index(
                fields=["status"],
                name="cls_rec_status_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"[{self.live_class_id}] rec={self.id} {self.status} egress={self.egress_id}"    
    