
from datetime import timedelta
from .models import ClassParticipant 
from django.utils import timezone
from rest_framework import serializers

from .models import LiveClass, LiveClassStatus
from .models import Attendance
from .models import ClassMessage  
from .models import ClassRecording 
import logging
from django.conf import settings
# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
class LiveClassReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = LiveClass
        fields = (
            "id",
            "title",
            "description",
            "teacher_id",
            "teacher_name",
            "scheduled_at",
            "duration_minutes",
            "status",
            "started_at",
            "ended_at",
            "room_name",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Write (shared validation logic)
# ---------------------------------------------------------------------------
MIN_DURATION_MINUTES = 1
MAX_DURATION_MINUTES = 600
MAX_SCHEDULE_AHEAD_DAYS = 365


class _BaseWriteSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    description = serializers.CharField(
        required=False, allow_blank=True, default=""
    )
    scheduled_at = serializers.DateTimeField()
    duration_minutes = serializers.IntegerField(
        min_value=MIN_DURATION_MINUTES,
        max_value=MAX_DURATION_MINUTES,
    )

    def validate_title(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Title must not be empty.")
        return value

    def validate_scheduled_at(self, value):
        # Allow up to 1 minute in the past to absorb small clock skew
        # between the client and our server.
        if value < timezone.now() - timedelta(minutes=1):
            raise serializers.ValidationError(
                "scheduled_at must not be in the past."
            )
        max_future = timezone.now() + timedelta(days=MAX_SCHEDULE_AHEAD_DAYS)
        if value > max_future:
            raise serializers.ValidationError(
                f"scheduled_at must be within {MAX_SCHEDULE_AHEAD_DAYS} days."
            )
        return value


class LiveClassCreateSerializer(_BaseWriteSerializer):
    """Input serializer for POST /api/classes/."""


class LiveClassUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255, required=False)
    description = serializers.CharField(
        required=False, allow_blank=True
    )
    scheduled_at = serializers.DateTimeField(required=False)
    duration_minutes = serializers.IntegerField(
        required=False,
        min_value=MIN_DURATION_MINUTES,
        max_value=MAX_DURATION_MINUTES,
    )

    def validate_title(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Title must not be empty.")
        return value

    def validate_scheduled_at(self, value):
        # Only allow rescheduling to the future (same rules as create).
        if value < timezone.now() - timedelta(minutes=1):
            raise serializers.ValidationError(
                "scheduled_at must not be in the past."
            )
        max_future = timezone.now() + timedelta(days=MAX_SCHEDULE_AHEAD_DAYS)
        if value > max_future:
            raise serializers.ValidationError(
                f"scheduled_at must be within {MAX_SCHEDULE_AHEAD_DAYS} days."
            )
        return value

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "At least one field must be provided."
            )
        return attrs

# ---------------------------------------------------------------------------
# LiveKit token request (Block 7)
# ---------------------------------------------------------------------------
class LiveKitTokenRequestSerializer(serializers.Serializer):
    client_info = serializers.DictField(required=False)

    def validate_client_info(self, value):
        # Keep it small; this is not a place to dump arbitrary client state.
        if not isinstance(value, dict):
            raise serializers.ValidationError("client_info must be an object.")
        if len(str(value)) > 2000:
            raise serializers.ValidationError("client_info is too large.")
        return value

class ClassParticipantSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassParticipant
        fields = (
            "id",
            "user_id",
            "name",
            "role",
            "status",
            "joined_at",
            "left_at",
            "join_count",
        )
        read_only_fields = fields    

class AttendanceSerializer(serializers.ModelSerializer):
    attendance_status = serializers.SerializerMethodField()

    class Meta:
        model = Attendance
        fields = (
            "id",
            "user_id",
            "name",
            "role",
            "first_joined_at",
            "last_left_at",
            "total_duration_seconds",
            "attendance_status",
            "sessions",
        )
        read_only_fields = fields

    def get_attendance_status(self, obj) -> str:
        from .attendance import compute_attendance_status
        return compute_attendance_status(
            obj.total_duration_seconds,
            obj.live_class.duration_minutes,
        )


class MyAttendanceSerializer(serializers.ModelSerializer):
    class_id = serializers.IntegerField(source="live_class.id", read_only=True)
    class_title = serializers.CharField(source="live_class.title", read_only=True)
    scheduled_at = serializers.DateTimeField(source="live_class.scheduled_at", read_only=True)
    duration_minutes = serializers.IntegerField(source="live_class.duration_minutes", read_only=True)
    class_status = serializers.CharField(source="live_class.status", read_only=True)
    attendance_status = serializers.SerializerMethodField()

    class Meta:
        model = Attendance
        fields = (
            "id",
            "class_id",
            "class_title",
            "scheduled_at",
            "duration_minutes",
            "class_status",
            "total_duration_seconds",
            "attendance_status",
            "first_joined_at",
            "last_left_at",
        )
        read_only_fields = fields

    def get_attendance_status(self, obj) -> str:
        from .attendance import compute_attendance_status
        return compute_attendance_status(
            obj.total_duration_seconds,
            obj.live_class.duration_minutes,
        )
class MessageSerializer(serializers.ModelSerializer):
    class_id = serializers.IntegerField(source="live_class_id", read_only=True)

    class Meta:
        model = ClassMessage
        fields = (
            "id",
            "class_id",
            "user_id",
            "name",
            "role",
            "text",
            "message_type",
            "visibility",
            "created_at",
            "deleted_at",
            "deleted_by_user_id",
        )
        read_only_fields = fields


class RecordingSerializer(serializers.ModelSerializer):
    class_id = serializers.IntegerField(source="live_class_id", read_only=True)

    class Meta:
        model = ClassRecording
        fields = (
            "id",
            "class_id",
            "status",
            "egress_id",
            "layout",
            "started_at",
            "stopped_at",
            "duration_seconds",
            "file_size_bytes",
            "error_message",
            "playback_url",
            "storage_key",
            "bucket",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields        

class RecordingSerializer(serializers.ModelSerializer):
    """
    Serializer for ClassRecording.

    `playback_url` is only populated when the caller passes
    `include_playback=True` in the serializer context AND the recording
    is completed. It is never read from the DB column (that column is
    effectively dead). See Block 15 docstring note.
    """

    class_id = serializers.IntegerField(source="live_class_id", read_only=True)
    playback_url = serializers.SerializerMethodField()

    class Meta:
        model = ClassRecording
        fields = (
            "id",
            "class_id",
            "status",
            "egress_id",
            "layout",
            "started_at",
            "stopped_at",
            "duration_seconds",
            "file_size_bytes",
            "error_message",
            "playback_url",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_playback_url(self, obj):
        include = self.context.get("include_playback", False)
        if not include:
            return None

        from apps.classes.models import RecordingStatus
        from apps.livekit import storage as s3_storage

        if obj.status != RecordingStatus.COMPLETED or not obj.storage_key:
            return None

        ttl = self.context.get("playback_ttl", settings.S3_PRESIGNED_URL_TTL_SECONDS)
        try:
            url, _ = s3_storage.generate_presigned_get_url(
                key=obj.storage_key,
                ttl_seconds=ttl,
                bucket=obj.bucket or settings.S3_BUCKET,
            )
            return url
        except Exception:
            # Never fail the whole list because one presign failed.
            logger = logging.getLogger(__name__)
            logger.exception("Presign failed for recording %s", obj.id)
            return None        