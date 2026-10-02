

import logging
from django.db import transaction
from django.utils import timezone
from apps.livekit import service as livekit_service
from apps.livekit.service import LiveKitServiceError
from apps.realtime import redis_state as rstate
from apps.realtime.broadcast import broadcast_class_event

from .models import (
    ClassParticipant,
    LiveClass,
    LiveClassStatus,
    ParticipantRole,
    ParticipantStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ControlError(Exception):
    """Base for control-flow errors. Views translate these to HTTP responses."""

    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _livekit_identity(role: str, user_id: int) -> str:
    return f"{role}-{user_id}"


def _get_participant(cls: LiveClass, user_id: int) -> ClassParticipant:
    try:
        return ClassParticipant.objects.get(live_class=cls, user_id=user_id)
    except ClassParticipant.DoesNotExist:
        raise ControlError(
            "NOT_FOUND",
            "Participant not found in this class.",
            status=404,
        )


def _assert_class_controllable(cls: LiveClass) -> None:
    if cls.status == LiveClassStatus.ENDED:
        raise ControlError("CLASS_ALREADY_ENDED", "Class has already ended.", status=409)
    if cls.status == LiveClassStatus.CANCELLED:
        raise ControlError("CLASS_CANCELLED", "Class was cancelled.", status=409)


def _tracks_of_type(participant, lk_type: str):
    return [t for t in participant.tracks if t.type == lk_type]


# LiveKit uses integer enums for track type. TrackType.AUDIO == 0, VIDEO == 1
# in the standard proto. We compare against strings coming from the SDK's
# __str__ on some versions; be tolerant.
def _is_audio(track) -> bool:
    t = str(getattr(track, "type", "")).upper()
    return "AUDIO" in t or t == "0"


def _is_video(track) -> bool:
    t = str(getattr(track, "type", "")).upper()
    return "VIDEO" in t or t == "1"


# ---------------------------------------------------------------------------
# Mute / unmute
# ---------------------------------------------------------------------------
def set_microphone_muted(cls: LiveClass, target_user_id: int, muted: bool) -> dict:
    _assert_class_controllable(cls)
    participant_row = _get_participant(cls, target_user_id)
    identity = _livekit_identity(participant_row.role, participant_row.user_id)

    try:
        lk_participants = livekit_service.list_participants(cls.room_name)
    except LiveKitServiceError as exc:
        raise ControlError("LIVEKIT_UPSTREAM_ERROR", str(exc), status=502) from exc

    target = next((p for p in lk_participants if p.identity == identity), None)
    if target is None:
        # They are not currently connected to LiveKit, but they may reconnect.
        # We cannot force a future mute at the SFU level, so we do not
        # broadcast success. The teacher's UI should tell the user to join.
        raise ControlError(
            "PARTICIPANT_NOT_CONNECTED",
            "Participant is not connected to the room right now.",
            status=409,
        )

    audio_tracks = [t for t in target.tracks if _is_audio(t)]
    if not audio_tracks:
        # No microphone track published. Nothing to mute.
        raise ControlError(
            "NO_AUDIO_TRACK",
            "Participant has no microphone track published.",
            status=409,
        )

    for track in audio_tracks:
        try:
            livekit_service.mute_track(
                room_name=cls.room_name,
                identity=identity,
                track_sid=track.sid,
                muted=muted,
            )
        except LiveKitServiceError as exc:
            raise ControlError("LIVEKIT_UPSTREAM_ERROR", str(exc), status=502) from exc

    event_type = "participant.muted" if muted else "participant.mic_enabled"
    broadcast_class_event(
        cls.id,
        {
            "type": event_type,
            "data": {
                "user_id": participant_row.user_id,
                "role": participant_row.role,
                "by_user_id": None,  # filled by view if needed
            },
        },
    )

    return {
        "user_id": participant_row.user_id,
        "muted": muted,
        "tracks_affected": len(audio_tracks),
    }


# ---------------------------------------------------------------------------
# Camera on / off
# ---------------------------------------------------------------------------
def set_camera_enabled(cls: LiveClass, target_user_id: int, enabled: bool) -> dict:
    """
    LiveKit semantics: we do NOT revoke can_publish for camera toggles.
    We only mute/unmute published video tracks.
    """
    _assert_class_controllable(cls)
    participant_row = _get_participant(cls, target_user_id)
    identity = _livekit_identity(participant_row.role, participant_row.user_id)

    try:
        lk_participants = livekit_service.list_participants(cls.room_name)
    except LiveKitServiceError as exc:
        raise ControlError("LIVEKIT_UPSTREAM_ERROR", str(exc), status=502) from exc

    target = next((p for p in lk_participants if p.identity == identity), None)
    if target is None:
        raise ControlError(
            "PARTICIPANT_NOT_CONNECTED",
            "Participant is not connected to the room right now.",
            status=409,
        )

    video_tracks = [t for t in target.tracks if _is_video(t)]
    if not video_tracks:
        raise ControlError(
            "NO_VIDEO_TRACK",
            "Participant has no camera track published.",
            status=409,
        )

    for track in video_tracks:
        try:
            livekit_service.mute_track(
                room_name=cls.room_name,
                identity=identity,
                track_sid=track.sid,
                muted=not enabled,
            )
        except LiveKitServiceError as exc:
            raise ControlError("LIVEKIT_UPSTREAM_ERROR", str(exc), status=502) from exc

    event_type = "participant.camera_enabled" if enabled else "participant.camera_disabled"
    broadcast_class_event(
        cls.id,
        {
            "type": event_type,
            "data": {
                "user_id": participant_row.user_id,
                "role": participant_row.role,
            },
        },
    )

    return {
        "user_id": participant_row.user_id,
        "camera_enabled": enabled,
        "tracks_affected": len(video_tracks),
    }


# ---------------------------------------------------------------------------
# Remove participant
# ---------------------------------------------------------------------------
@transaction.atomic
def remove_participant(cls: LiveClass, target_user_id: int, reason: str = "") -> dict:
    _assert_class_controllable(cls)

    participant_row = (
        ClassParticipant.objects
        .select_for_update()
        .get(live_class=cls, user_id=target_user_id)
        if ClassParticipant.objects.filter(live_class=cls, user_id=target_user_id).exists()
        else None
    )
    if participant_row is None:
        raise ControlError("NOT_FOUND", "Participant not found.", status=404)

    if participant_row.status == ParticipantStatus.REMOVED:
        # Idempotent — already removed.
        return {
            "user_id": target_user_id,
            "status": ParticipantStatus.REMOVED,
            "already_removed": True,
        }

    participant_row.status = ParticipantStatus.REMOVED
    participant_row.left_at = timezone.now()
    participant_row.save(update_fields=["status", "left_at"])

    # Revoke LiveKit publishing and remove them from the room.
    identity = _livekit_identity(participant_row.role, participant_row.user_id)
    try:
        livekit_service.update_participant_permissions(
            room_name=cls.room_name,
            identity=identity,
            can_publish=False,
            can_publish_data=False,
            can_subscribe=True,
        )
    except LiveKitServiceError as exc:
        # Non-fatal: the participant is removed in the DB. They cannot rejoin.
        logger.warning("LiveKit grant revoke failed during remove: %s", exc)

    try:
        livekit_service.remove_participant(room_name=cls.room_name, identity=identity)
    except LiveKitServiceError as exc:
        # Also non-fatal: they will be dropped when the room deletes them
        # at end of class. Meanwhile, status=removed prevents rejoin.
        logger.warning("LiveKit remove_participant failed: %s", exc)

    broadcast_class_event(
        cls.id,
        {
            "type": "participant.removed",
            "data": {
                "user_id": target_user_id,
                "role": participant_row.role,
                "reason": reason,
            },
        },
    )

    return {
        "user_id": target_user_id,
        "status": ParticipantStatus.REMOVED,
        "already_removed": False,
    }


# ---------------------------------------------------------------------------
# End class
# ---------------------------------------------------------------------------
@transaction.atomic
def end_class(cls: LiveClass) -> dict:
    if cls.status == LiveClassStatus.ENDED:
        return {
            "class_id": cls.id,
            "status": cls.status,
            "already_ended": True,
        }

    if cls.status == LiveClassStatus.CANCELLED:
        raise ControlError("CLASS_CANCELLED", "Class was cancelled.", status=409)

    now = timezone.now()
    cls.status = LiveClassStatus.ENDED
    cls.ended_at = now
    if cls.started_at is None:
        # If a teacher ends a class that was never formally started, record
        # started_at as ended_at for bookkeeping. Alternatively, we could
        # refuse. We choose to record and end.
        cls.started_at = now
    cls.save(update_fields=["status", "ended_at", "started_at"])

    # Move all still-active participants to 'left'.
    ClassParticipant.objects.filter(
        live_class=cls,
        status=ParticipantStatus.ACTIVE,
    ).update(
        status=ParticipantStatus.LEFT,
        left_at=now,
    )

    # Broadcast before deleting the room so clients get a graceful signal.
    broadcast_class_event(
        cls.id,
        {
            "type": "class.ended",
            "data": {
                "class_id": cls.id,
                "ended_at": now.isoformat().replace("+00:00", "Z"),
            },
        },
    )

    # Delete the LiveKit room (best-effort).
    try:
        livekit_service.delete_room(cls.room_name)
    except LiveKitServiceError as exc:
        logger.warning("LiveKit delete_room failed for %s: %s", cls.room_name, exc)

    # Clear Redis state for the class.
    try:
        rstate.clear_class_state(cls.id)
    except Exception:
        logger.exception("Redis clear_class_state failed for class %s", cls.id)

    return {
        "class_id": cls.id,
        "status": cls.status,
        "ended_at": now.isoformat().replace("+00:00", "Z"),
        "already_ended": False,
    }