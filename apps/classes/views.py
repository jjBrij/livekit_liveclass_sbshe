from . import controls
import logging
from django.utils import timezone
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.exceptions import NotFound, PermissionDenied, APIException
from apps.core.responses import success, error
from apps.livekit import service as livekit_service
from apps.livekit.service import (
    LiveKitConfigurationError,
    LiveKitServiceError,
)
from .serializers import LiveKitTokenRequestSerializer
from .models import LiveClass, LiveClassStatus
from .serializers import (
    LiveClassCreateSerializer,
    LiveClassReadSerializer,
    LiveClassUpdateSerializer,
)
logger = logging.getLogger(__name__)
from .models import (
    LiveClass,
    LiveClassStatus,
    ClassParticipant,
    ParticipantRole,
    ParticipantStatus,
)
from .serializers import ClassParticipantSerializer
from .attendance import record_join, record_leave, compute_attendance_status
from .models import Attendance
from .serializers import (
    AttendanceSerializer,
    MyAttendanceSerializer,
)
from django.db.models import Count, Q
from apps.realtime import redis_state as rstate
from apps.realtime.broadcast import broadcast_class_event
from apps.livekit import service as livekit_service
from apps.livekit.service import (
    LiveKitServiceError as LKServiceError,
    LiveKitConfigurationError as LKConfigError,
)
from django.utils.dateparse import parse_datetime
from .models import ClassMessage, MessageVisibility
from .serializers import MessageSerializer
from apps.livekit import service as livekit_service
from apps.livekit.service import LiveKitServiceError as LKServiceError
from .models import ClassRecording, RecordingStatus
from .serializers import RecordingSerializer
from apps.livekit.service import LiveKitConfigurationError as LKConfigError
from apps.livekit import storage as s3_storage
from apps.livekit.storage import S3ConfigurationError, S3ServiceError


class ClassNotEditable(APIException):
    status_code = 409
    default_detail = "Class is not editable in its current state."
    default_code = "CLASS_NOT_EDITABLE"


class ClassNotDeletable(APIException):
    status_code = 409
    default_detail = "Class is not deletable in its current state."
    default_code = "CLASS_NOT_DELETABLE"
def _require_teacher(user):
    if not getattr(user, "is_teacher", False):
        raise PermissionDenied("Teacher role required.")


def _get_owned_class_or_raise(user, class_id: int) -> LiveClass:
    try:
        cls = LiveClass.objects.get(pk=class_id)
    except LiveClass.DoesNotExist:
        raise NotFound("Class not found.")

    if cls.teacher_id != user.user_id:
        raise PermissionDenied("You do not own this class.")

    return cls


def _paginate(request, queryset):
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except (TypeError, ValueError):
        page = 1

    try:
        page_size = int(request.query_params.get("page_size", 20))
    except (TypeError, ValueError):
        page_size = 20
    page_size = max(1, min(page_size, 100))

    total = queryset.count()
    start = (page - 1) * page_size
    end = start + page_size
    results = list(queryset[start:end])

    return results, {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
    }

def _mint_token_for_participant(user, cls, grants: dict) -> str:
    """
    Thin wrapper around livekit_service.generate_participant_token that
    centralizes the identity/name conventions. Raises the same service
    exceptions so callers can translate them into 503 responses.
    """
    identity = f"{user.role}-{user.user_id}"
    display_name = user.name or f"{user.role.capitalize()} {user.user_id}"
    return livekit_service.generate_participant_token(
        identity=identity,
        name=display_name,
        room_name=cls.room_name,
        role=user.role,
        **grants,
    )


def _get_or_create_participant(user, cls) -> ClassParticipant:
    """
    Fetch the participant row for (cls, user), creating it if absent.

    Rules:
        - banned:       reject (403)
        - removed:      reject (403)
        - left/active:  set status=active, clear left_at, bump join_count
    """
    participant, created = ClassParticipant.objects.get_or_create(
        live_class=cls,
        user_id=user.user_id,
        defaults={
            "name": user.name or f"User {user.user_id}",
            "role": user.role,
            "status": ParticipantStatus.ACTIVE,
        },
    )

    if not created:
        if participant.status == ParticipantStatus.BANNED:
            raise PermissionDenied("You are banned from this class.")
        if participant.status == ParticipantStatus.REMOVED:
            raise PermissionDenied("You have been removed from this class.")

        # Re-joining after leaving (or reconnecting while active).
        participant.status = ParticipantStatus.ACTIVE
        participant.left_at = None
        participant.name = user.name or participant.name
        participant.role = user.role
        participant.join_count = (participant.join_count or 0) + 1
        participant.save(
            update_fields=["status", "left_at", "name", "role", "join_count"]
        )
    else:
        # First-ever join: join_count starts at 1
        ClassParticipant.objects.filter(pk=participant.pk).update(join_count=1)
        participant.refresh_from_db(fields=["join_count"])

    return participant
# ---------------------------------------------------------------------------
# Collection: POST (create) + GET (list)
# ---------------------------------------------------------------------------
@api_view(["POST", "GET"])
def class_collection(request):
    if request.method == "POST":
        return _create_class(request)
    return _list_classes(request)


def _create_class(request):
    _require_teacher(request.user)

    serializer = LiveClassCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    with transaction.atomic():
        cls = LiveClass.objects.create(
            teacher_id=request.user.user_id,
            teacher_name=request.user.name,
            title=data["title"],
            description=data.get("description", ""),
            scheduled_at=data["scheduled_at"],
            duration_minutes=data["duration_minutes"],
            status=LiveClassStatus.SCHEDULED,
        )

    return success(
        data=LiveClassReadSerializer(cls).data,
        message="Class created successfully",
        status=201,
    )


def _list_classes(request):
    qs = LiveClass.objects.all()

    status_param = request.query_params.get("status")
    if status_param:
        valid = {c.value for c in LiveClassStatus}
        if status_param not in valid:
            return error(
                code="VALIDATION_ERROR",
                message="Invalid status filter.",
                status=400,
                details={"status": [f"Must be one of: {', '.join(sorted(valid))}"]},
            )
        qs = qs.filter(status=status_param)

    if request.query_params.get("mine") in ("true", "1", "True"):
        qs = qs.filter(teacher_id=request.user.user_id)

    if request.query_params.get("upcoming") in ("true", "1", "True"):
        qs = qs.filter(
            status=LiveClassStatus.SCHEDULED,
            scheduled_at__gte=timezone.now(),
        ).order_by("scheduled_at")

    results, pagination = _paginate(request, qs)
    serializer = LiveClassReadSerializer(results, many=True)

    return success(
        data={
            "results": serializer.data,
            "pagination": pagination,
        },
        message="Classes fetched",
        status=200,
    )


# ---------------------------------------------------------------------------
# Detail: GET / PATCH / DELETE
# ---------------------------------------------------------------------------
@api_view(["GET", "PATCH", "DELETE"])
def class_detail(request, class_id: int):
    cls = _get_owned_class_or_raise(request.user, class_id)

    if request.method == "GET":
        return success(
            data=LiveClassReadSerializer(cls).data,
            message="Class fetched",
        )

    _require_teacher(request.user)

    if request.method == "PATCH":
        return _update_class(request, cls)

    return _delete_class(request, cls)


def _update_class(request, cls: LiveClass):
    if cls.status != LiveClassStatus.SCHEDULED:
        raise ClassNotEditable(
            "Only classes in 'scheduled' status can be edited."
        )

    serializer = LiveClassUpdateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    changed = False
    for field in ("title", "description", "scheduled_at", "duration_minutes"):
        if field in data:
            setattr(cls, field, data[field])
            changed = True

    if changed:
        cls.save()

    return success(
        data=LiveClassReadSerializer(cls).data,
        message="Class updated successfully",
    )


def _delete_class(request, cls: LiveClass):
    if cls.status not in (LiveClassStatus.SCHEDULED, LiveClassStatus.CANCELLED):
        raise ClassNotDeletable(
            "Only classes in 'scheduled' or 'cancelled' status can be deleted."
        )

    cls.delete()
    return success(data=None, message="Class deleted successfully")




# ---------------------------------------------------------------------------
# LiveKit token: POST /api/classes/{id}/token/
# ---------------------------------------------------------------------------
class ClassNotJoinable(APIException):
    status_code = 409
    default_detail = "This class cannot be joined in its current state."
    default_code = "CLASS_NOT_JOINABLE"


def _grants_for_role(role: str) -> dict:
    
    if role == "teacher":
        return {
            "can_publish": True,
            "can_publish_data": True,
            "can_subscribe": True,
        }
    if role == "student":
        return {
            "can_publish": True,
            "can_publish_data": True,
            "can_subscribe": True,
        }
    # Unknown role: fail closed. Do not mint tokens for roles we do not know.
    raise PermissionDenied(f"Unsupported role: {role}")


@api_view(["POST"])
def class_token(request, class_id: int):
    body_serializer = LiveKitTokenRequestSerializer(data=request.data or {})
    body_serializer.is_valid(raise_exception=True)
    # client_info = body_serializer.validated_data.get("client_info", {})  # Block 8

    # 2. Load class
    try:
        cls = LiveClass.objects.get(pk=class_id)
    except LiveClass.DoesNotExist:
        raise NotFound("Class not found.")

    user = request.user

    # 3. Class must be joinable
    if cls.status in (LiveClassStatus.ENDED, LiveClassStatus.CANCELLED):
        raise ClassNotJoinable(
            f"Class is '{cls.status}' and cannot be joined."
        )

    # 4. Role checks
    if user.role == "teacher":
        if cls.teacher_id != user.user_id:
            raise PermissionDenied("You do not own this class.")
    elif user.role == "student":
        enrolled = ClassParticipant.objects.filter(
            live_class=cls,
            user_id=user.user_id,
            status__in=[ParticipantStatus.ACTIVE, ParticipantStatus.LEFT],
        ).exists()
        if not enrolled:
            raise PermissionDenied(
                "You are not enrolled in this class. Call /join/ first."
            )
    else:
        raise PermissionDenied(f"Unsupported role: {user.role}")

    # 5. Mint token
    grants = _grants_for_role(user.role)
    identity = f"{user.role}-{user.user_id}"
    display_name = user.name or f"{user.role.capitalize()} {user.user_id}"

    try:
        token = livekit_service.generate_participant_token(
            identity=identity,
            name=display_name,
            room_name=cls.room_name,
            role=user.role,
            # grants passed via kwargs to the service (see note below)
            **grants,
        )
    except LiveKitConfigurationError as exc:
        logger.error("LiveKit not configured: %s", exc)
        return error(
            code="LIVEKIT_NOT_CONFIGURED",
            message="LiveKit is not configured on the server.",
            status=500,
            details={"reason": str(exc)},
        )
    except LiveKitServiceError as exc:
        logger.error("LiveKit token mint failed: %s", exc)
        return error(
            code="LIVEKIT_UNREACHABLE",
            message="Could not obtain a LiveKit token. Try again shortly.",
            status=503,
            details={"reason": str(exc)},
        )

    # 6. Respond with the exact shape from Section 7 of the spec.
    return success(
        data={
            "room_name": cls.room_name,
            "participant_identity": identity,
            "participant_name": display_name,
            "role": user.role,
            "livekit_url": settings.LIVEKIT_URL,
            "token": token,
        },
        message="Successfully joined class",
    )

# ---------------------------------------------------------------------------
# Join: POST /api/classes/{id}/join/
# ---------------------------------------------------------------------------
@api_view(["POST"])
def class_join(request, class_id: int):
 
    # 1. Load class
    try:
        cls = LiveClass.objects.get(pk=class_id)
    except LiveClass.DoesNotExist:
        raise NotFound("Class not found.")

    user = request.user

    # 2. Class must be joinable
    if cls.status in (LiveClassStatus.ENDED, LiveClassStatus.CANCELLED):
        raise ClassNotJoinable(
            f"Class is '{cls.status}' and cannot be joined."
        )

    # 3. Role and ownership rules
    if user.role == "teacher":
        if cls.teacher_id != user.user_id:
            raise PermissionDenied("You do not own this class.")
    elif user.role == "student":
        # Students may join any scheduled/live class. Enrollment is created
        # on first join. Future blocks may restrict this to invited students.
        pass
    else:
        raise PermissionDenied(f"Unsupported role: {user.role}")

    # 4. Participant row
    participant = _get_or_create_participant(user, cls)
        # Record attendance. WebSocket connect will also call this, but with
    # a 5s idempotency window so it counts as one session.
    record_join(user, cls)
    # 5. Mint token
    grants = _grants_for_role(user.role)
    try:
        token = _mint_token_for_participant(user, cls, grants)
    except LiveKitConfigurationError as exc:
        logger.error("LiveKit not configured: %s", exc)
        return error(
            code="LIVEKIT_NOT_CONFIGURED",
            message="LiveKit is not configured on the server.",
            status=500,
            details={"reason": str(exc)},
        )
    except LiveKitServiceError as exc:
        logger.error("LiveKit token mint failed: %s", exc)
        return error(
            code="LIVEKIT_UNREACHABLE",
            message="Could not obtain a LiveKit token. Try again shortly.",
            status=503,
            details={"reason": str(exc)},
        )

    # 6. Response — same shape as /token/, plus participant info.
    return success(
        data={
            "room_name": cls.room_name,
            "participant_identity": f"{user.role}-{user.user_id}",
            "participant_name": participant.name,
            "role": user.role,
            "livekit_url": settings.LIVEKIT_URL,
            "token": token,
            "participant": ClassParticipantSerializer(participant).data,
        },
        message="Successfully joined class",
    )


# ---------------------------------------------------------------------------
# Leave: POST /api/classes/{id}/leave/
# ---------------------------------------------------------------------------
@api_view(["POST"])
def class_leave(request, class_id: int):
    try:
        cls = LiveClass.objects.get(pk=class_id)
    except LiveClass.DoesNotExist:
        raise NotFound("Class not found.")

    user = request.user
    
    try:
        participant = ClassParticipant.objects.get(
            live_class=cls,
            user_id=user.user_id,
        )
    except ClassParticipant.DoesNotExist:
        raise NotFound("You are not a participant of this class.")

    if participant.status != ParticipantStatus.LEFT:
        participant.status = ParticipantStatus.LEFT
        participant.left_at = timezone.now()
        participant.save(update_fields=["status", "left_at"])
    record_leave(user, cls)
    return success(
        data={"participant": ClassParticipantSerializer(participant).data},
        message="You have left the class",
    )


# ---------------------------------------------------------------------------
# Participants: GET /api/classes/{id}/participants/
# ---------------------------------------------------------------------------
@api_view(["GET"])
def class_participants(request, class_id: int):
    """
    List participants of a class. Teacher (owner) only for now.
    Student visibility is decided in Block 13.
    """
    try:
        cls = LiveClass.objects.get(pk=class_id)
    except LiveClass.DoesNotExist:
        raise NotFound("Class not found.")

    if cls.teacher_id != request.user.user_id:
        raise PermissionDenied("You do not own this class.")

    qs = ClassParticipant.objects.filter(live_class=cls)

    status_param = request.query_params.get("status")
    if status_param:
        valid = {c.value for c in ParticipantStatus}
        if status_param not in valid:
            return error(
                code="VALIDATION_ERROR",
                message="Invalid status filter.",
                status=400,
                details={"status": [f"Must be one of: {', '.join(sorted(valid))}"]},
            )
        qs = qs.filter(status=status_param)

    role_param = request.query_params.get("role")
    if role_param:
        valid = {c.value for c in ParticipantRole}
        if role_param not in valid:
            return error(
                code="VALIDATION_ERROR",
                message="Invalid role filter.",
                status=400,
                details={"role": [f"Must be one of: {', '.join(sorted(valid))}"]},
            )
        qs = qs.filter(role=role_param)

    serializer = ClassParticipantSerializer(qs, many=True)
    data = serializer.data

    return success(
        data={"participants": data, "total": len(data)},
        message="Participants fetched",
    )   

# ---------------------------------------------------------------------------
# Class attendance (teacher owner): GET /api/classes/{id}/attendance/
# ---------------------------------------------------------------------------
@api_view(["GET"])
def class_attendance(request, class_id: int):
    try:
        cls = LiveClass.objects.get(pk=class_id)
    except LiveClass.DoesNotExist:
        raise NotFound("Class not found.")

    if cls.teacher_id != request.user.user_id:
        raise PermissionDenied("You do not own this class.")

    qs = Attendance.objects.filter(live_class=cls).select_related("live_class")

    role_param = request.query_params.get("role")
    if role_param:
        if role_param not in ("teacher", "student"):
            return error(
                code="VALIDATION_ERROR",
                message="Invalid role filter.",
                status=400,
                details={"role": ["Must be 'teacher' or 'student'"]},
            )
        qs = qs.filter(role=role_param)

    # Compute per-row status and (optionally) filter by it in Python.
    # The set is bounded by class size; safe to filter in memory.
    serializer = AttendanceSerializer(qs, many=True)
    rows = serializer.data

    status_param = request.query_params.get("status")
    if status_param:
        if status_param not in ("present", "partial", "absent"):
            return error(
                code="VALIDATION_ERROR",
                message="Invalid status filter.",
                status=400,
                details={"status": ["Must be 'present', 'partial', or 'absent'"]},
            )
        rows = [r for r in rows if r["attendance_status"] == status_param]

    summary_counts = {"present": 0, "partial": 0, "absent": 0}
    for r in rows:
        summary_counts[r["attendance_status"]] += 1

    return success(
        data={
            "class": {
                "id": cls.id,
                "title": cls.title,
                "scheduled_at": cls.scheduled_at,
                "duration_minutes": cls.duration_minutes,
                "status": cls.status,
            },
            "summary": {**summary_counts, "total": len(rows)},
            "attendance": rows,
        },
        message="Attendance fetched",
    )


# ---------------------------------------------------------------------------
# My attendance: GET /api/attendance/me/
# ---------------------------------------------------------------------------
@api_view(["GET"])
def my_attendance(request):
    qs = (
        Attendance.objects
        .filter(user_id=request.user.user_id)
        .select_related("live_class")
        .order_by("-live_class__scheduled_at")
    )

    results, pagination = _paginate(request, qs)
    serializer = MyAttendanceSerializer(results, many=True)

    return success(
        data={
            "results": serializer.data,
            "pagination": pagination,
        },
        message="Attendance fetched",
    )



# ---------------------------------------------------------------------------
# Hands: list, accept, reject
# ---------------------------------------------------------------------------
def _get_owned_class_or_404_and_owner_check(request, class_id: int) -> LiveClass:
    try:
        cls = LiveClass.objects.get(pk=class_id)
    except LiveClass.DoesNotExist:
        raise NotFound("Class not found.")
    if cls.teacher_id != request.user.user_id:
        raise PermissionDenied("You do not own this class.")
    return cls


@api_view(["GET"])
def class_hands(request, class_id: int):
    """
    GET /api/classes/{id}/hands/

    Teacher (owner) only. Returns the currently raised hands and accepted set.
    """
    _get_owned_class_or_404_and_owner_check(request, class_id)

    raised = rstate.list_raised_hands(class_id)
    accepted = rstate.list_accepted_hands(class_id)

    return success(
        data={
            "raised": raised,
            "accepted": accepted,
            "raised_count": len(raised),
            "accepted_count": len(accepted),
        },
        message="Raised hands fetched",
    )


@api_view(["POST"])
def class_hand_accept(request, class_id: int, user_id: int):
    """
    POST /api/classes/{id}/hands/{user_id}/accept/

    Teacher (owner) accepts the student's raised hand.

    Effects:
        1. Adds the student to accepted_hands:{class_id} in Redis.
        2. Removes them from raised_hands:{class_id}.
        3. Updates LiveKit grants so the student may publish.
        4. Broadcasts hand_accepted to the class WebSocket group.
    """
    cls = _get_owned_class_or_404_and_owner_check(request, class_id)

    # Verify there is a raised hand to accept (idempotency: if already
    # accepted, this still succeeds).
    raised = rstate.get_raised_hand(class_id, user_id)
    already_accepted = rstate.is_hand_accepted(class_id, user_id)

    if not raised and not already_accepted:
        raise NotFound("No raised hand for that user.")

    rstate.add_accepted_hand(class_id, user_id)
    rstate.remove_raised_hand(class_id, user_id)

    # Update LiveKit permissions. If it fails, log and continue: the
    # accept still succeeded from the user's perspective; the frontend
    # will tell the student to retry if their mic does not work.
    identity = f"student-{user_id}"
    try:
        livekit_service.update_participant_permissions(
            room_name=cls.room_name,
            identity=identity,
            can_publish=True,
            can_publish_data=True,
            can_subscribe=True,
        )
    except (LKServiceError, LKConfigError) as exc:
        logger.warning(
            "LiveKit permission update failed for %s in %s: %s",
            identity, cls.room_name, exc,
        )

    broadcast_class_event(
        class_id,
        {
            "type": "hand_accepted",
            "data": {
                "user_id": user_id,
                "accepted_at": timezone.now().isoformat().replace("+00:00", "Z"),
            },
        },
    )

    return success(
        data={"user_id": user_id, "accepted": True},
        message="Hand accepted",
    )


@api_view(["POST"])
def class_hand_reject(request, class_id: int, user_id: int):
    """
    POST /api/classes/{id}/hands/{user_id}/reject/

    Teacher (owner) rejects a raised hand. Optional body:
        {"reason": "..."}
    """
    cls = _get_owned_class_or_404_and_owner_check(request, class_id)

    raised = rstate.get_raised_hand(class_id, user_id)
    if not raised:
        # Idempotent — nothing to reject.
        raise NotFound("No raised hand for that user.")

    reason = ""
    if isinstance(request.data, dict):
        reason = str(request.data.get("reason", ""))[:200]

    rejection = {
        "user_id": user_id,
        "reason": reason,
        "rejected_at": timezone.now().isoformat().replace("+00:00", "Z"),
    }

    rstate.add_rejected_hand(class_id, rejection)
    rstate.remove_raised_hand(class_id, user_id)

    broadcast_class_event(
        class_id,
        {
            "type": "hand_rejected",
            "data": rejection,
        },
    )

    return success(
        data={"user_id": user_id, "rejected": True, "reason": reason},
        message="Hand rejected",
    )



# ---------------------------------------------------------------------------
# Chat: history + delete
# ---------------------------------------------------------------------------
@api_view(["GET"])
def class_messages(request, class_id: int):
    """
    GET /api/classes/{id}/messages/

    Access:
        - teacher (owner):     sees all messages
        - participant student: sees class messages + own teacher_only messages
        - non-participant:     403
    """
    try:
        cls = LiveClass.objects.get(pk=class_id)
    except LiveClass.DoesNotExist:
        raise NotFound("Class not found.")

    user = request.user

    is_owner = (user.role == "teacher" and cls.teacher_id == user.user_id)
    is_participant = ClassParticipant.objects.filter(
        live_class=cls,
        user_id=user.user_id,
        status__in=[ParticipantStatus.ACTIVE, ParticipantStatus.LEFT],
    ).exists()

    if not (is_owner or is_participant):
        raise PermissionDenied("You do not have access to this class.")

    qs = ClassMessage.objects.filter(live_class=cls).select_related("live_class")

    if not is_owner:
        # Students see only class-visible messages, plus their own teacher_only.
        from django.db.models import Q
        qs = qs.filter(
            Q(visibility=MessageVisibility.CLASS)
            | Q(visibility=MessageVisibility.TEACHER_ONLY, user_id=user.user_id)
        )

    # Optional filters
    visibility_param = request.query_params.get("visibility")
    if visibility_param:
        if visibility_param not in ("class", "teacher_only"):
            return error(
                code="VALIDATION_ERROR",
                message="Invalid visibility filter.",
                status=400,
                details={"visibility": ["Must be 'class' or 'teacher_only'"]},
            )
        if not is_owner and visibility_param == "teacher_only":
            # Students only ever see their own teacher_only messages; filter below.
            qs = qs.filter(visibility=visibility_param, user_id=user.user_id)
        else:
            qs = qs.filter(visibility=visibility_param)

    since_param = request.query_params.get("since")
    if since_param:
        since_dt = parse_datetime(since_param)
        if since_dt is None:
            return error(
                code="VALIDATION_ERROR",
                message="Invalid 'since' timestamp.",
                status=400,
            )
        if timezone.is_naive(since_dt):
            since_dt = timezone.make_aware(since_dt, timezone.utc)
        qs = qs.filter(created_at__gte=since_dt)

    results, pagination = _paginate(request, qs)
    serializer = MessageSerializer(results, many=True)

    return success(
        data={
            "results": serializer.data,
            "pagination": pagination,
        },
        message="Messages fetched",
    )


@api_view(["POST"])
def class_message_delete(request, class_id: int, message_id: int):
    """
    POST /api/classes/{id}/messages/{message_id}/delete/

    Teacher (owner) only. Soft-deletes the message and broadcasts.
    Idempotent.
    """
    cls = _get_owned_class_or_404_and_owner_check(request, class_id)

    try:
        message = ClassMessage.objects.get(pk=message_id, live_class=cls)
    except ClassMessage.DoesNotExist:
        raise NotFound("Message not found.")

    if message.deleted_at is None:
        message.deleted_at = timezone.now()
        message.deleted_by_user_id = request.user.user_id
        message.save(update_fields=["deleted_at", "deleted_by_user_id"])

        broadcast_class_event(
            class_id,
            {
                "type": "chat.message_deleted",
                "data": {
                    "id": message.id,
                    "deleted_by_user_id": message.deleted_by_user_id,
                    "deleted_at": message.deleted_at.isoformat().replace("+00:00", "Z"),
                },
            },
        )

    return success(
        data={
            "id": message.id,
            "deleted_at": message.deleted_at.isoformat().replace("+00:00", "Z"),
        },
        message="Message deleted",
    )


# ---------------------------------------------------------------------------
# Teacher controls
# ---------------------------------------------------------------------------
def _require_owner(request, class_id: int) -> LiveClass:
    try:
        cls = LiveClass.objects.get(pk=class_id)
    except LiveClass.DoesNotExist:
        raise NotFound("Class not found.")
    if request.user.role != "teacher" or cls.teacher_id != request.user.user_id:
        raise PermissionDenied("You do not own this class.")
    return cls


def _control_response(fn, *args, **kwargs):
    """
    Small helper: run a controls function, translate ControlError into the
    standard envelope. Keeps the views tiny.
    """
    try:
        data = fn(*args, **kwargs)
    except controls.ControlError as exc:
        return error(
            code=exc.code,
            message=exc.message,
            status=exc.status,
        )
    return success(data=data, message="OK")


@api_view(["POST"])
def control_mute(request, class_id: int):
    cls = _require_owner(request, class_id)
    target_user_id = int(request.data.get("user_id", 0))
    return _control_response(controls.set_microphone_muted, cls, target_user_id, True)


@api_view(["POST"])
def control_unmute(request, class_id: int):
    cls = _require_owner(request, class_id)
    target_user_id = int(request.data.get("user_id", 0))
    return _control_response(controls.set_microphone_muted, cls, target_user_id, False)


@api_view(["POST"])
def control_camera_disable(request, class_id: int):
    cls = _require_owner(request, class_id)
    target_user_id = int(request.data.get("user_id", 0))
    return _control_response(controls.set_camera_enabled, cls, target_user_id, False)


@api_view(["POST"])
def control_camera_enable(request, class_id: int):
    cls = _require_owner(request, class_id)
    target_user_id = int(request.data.get("user_id", 0))
    return _control_response(controls.set_camera_enabled, cls, target_user_id, True)


@api_view(["POST"])
def control_remove(request, class_id: int):
    cls = _require_owner(request, class_id)
    target_user_id = int(request.data.get("user_id", 0))
    reason = str(request.data.get("reason", ""))[:200]
    return _control_response(controls.remove_participant, cls, target_user_id, reason)


@api_view(["POST"])
def control_end(request, class_id: int):
    cls = _require_owner(request, class_id)
    return _control_response(controls.end_class, cls)


@api_view(["GET"])
def control_livekit_participants(request, class_id: int):
    """
    Returns the LiveKit-side view of who is connected and their permissions.
    Useful for a teacher's real-time control panel.
    """
    cls = _require_owner(request, class_id)

    try:
        lk_participants = livekit_service.list_participants(cls.room_name)
    except LKServiceError as exc:
        return error(
            code="LIVEKIT_UPSTREAM_ERROR",
            message=str(exc),
            status=502,
        )

    def _track(t):
        return {
            "sid": t.sid,
            "type": str(t.type).upper(),
            "muted": bool(t.muted),
            "source": getattr(t, "source", None),
        }

    data = [
        {
            "identity": p.identity,
            "name": getattr(p, "name", None),
            "state": str(getattr(p, "state", "")).upper(),
            "joined_at": getattr(p, "joined_at", None),
            "is_publisher": getattr(p, "is_publisher", None),
            "permissions": {
                "can_publish": getattr(p.permission, "can_publish", None) if hasattr(p, "permission") else None,
                "can_subscribe": getattr(p.permission, "can_subscribe", None) if hasattr(p, "permission") else None,
            } if hasattr(p, "permission") else None,
            "tracks": [_track(t) for t in getattr(p, "tracks", [])],
        }
        for p in lk_participants
    ]

    return success(
        data={"participants": data, "total": len(data)},
        message="LiveKit participants fetched",
    )


# ---------------------------------------------------------------------------
# Recording endpoints
# ---------------------------------------------------------------------------
ACTIVE_RECORDING_STATUSES = (
    RecordingStatus.STARTING,
    RecordingStatus.ACTIVE,
    RecordingStatus.STOPPING,
)


@api_view(["POST"])
def recording_start(request, class_id: int):
    cls = _require_owner(request, class_id)

    if cls.status in (LiveClassStatus.ENDED, LiveClassStatus.CANCELLED):
        return error(
            code="CLASS_ALREADY_ENDED",
            message="Cannot start a recording for a class that has ended or was cancelled.",
            status=409,
        )

    # One active recording per class.
    if ClassRecording.objects.filter(
        live_class=cls, status__in=ACTIVE_RECORDING_STATUSES
    ).exists():
        return error(
            code="RECORDING_IN_PROGRESS",
            message="A recording is already in progress for this class.",
            status=409,
        )

    layout = str(request.data.get("layout") or settings.LIVEKIT_EGRESS_DEFAULT_LAYOUT)[:32]
    audio_only = bool(request.data.get("audio_only", False))

    recording = ClassRecording.objects.create(
        live_class=cls,
        layout=layout,
        status=RecordingStatus.STARTING,
        started_at=timezone.now(),
        bucket=settings.S3_BUCKET or "",
    )

    try:
        egress_info = livekit_service.start_room_composite_egress(
            room_name=cls.room_name,
            layout=layout,
            audio_only=audio_only,
        )
    except LKConfigError as exc:
        recording.status = RecordingStatus.FAILED
        recording.error_message = str(exc)[:2000]
        recording.save(update_fields=["status", "error_message"])
        return error(
            code="LIVEKIT_NOT_CONFIGURED",
            message="LiveKit/S3 is not configured for recording.",
            status=500,
            details={"reason": str(exc)},
        )
    except LKServiceError as exc:
        recording.status = RecordingStatus.FAILED
        recording.error_message = str(exc)[:2000]
        recording.save(update_fields=["status", "error_message"])
        return error(
            code="LIVEKIT_UPSTREAM_ERROR",
            message="Could not start recording.",
            status=502,
            details={"reason": str(exc)},
        )

    recording.egress_id = getattr(egress_info, "egress_id", None)
    recording.status = RecordingStatus.ACTIVE  # optimistic; webhook confirms
    recording.save(update_fields=["egress_id", "status"])

    broadcast_class_event(
        cls.id,
        {
            "type": "recording.started",
            "data": {
                "recording_id": recording.id,
                "started_at": recording.started_at.isoformat().replace("+00:00", "Z"),
            },
        },
    )

    return success(
        data=RecordingSerializer(recording).data,
        message="Recording started",
        status=201,
    )


@api_view(["POST"])
def recording_stop(request, class_id: int):
    cls = _require_owner(request, class_id)

    recording = ClassRecording.objects.filter(
        live_class=cls, status__in=ACTIVE_RECORDING_STATUSES
    ).order_by("-started_at").first()

    if recording is None:
        return error(
            code="RECORDING_NOT_ACTIVE",
            message="No active recording for this class.",
            status=409,
        )

    if not recording.egress_id:
        return error(
            code="RECORDING_NOT_ACTIVE",
            message="Recording has no egress id; cannot stop.",
            status=409,
        )

    try:
        livekit_service.stop_egress(recording.egress_id)
    except LKServiceError as exc:
        msg = str(exc)
        if "failed_precondition" in msg or "cannot be stopped" in msg:
            recording.status = RecordingStatus.FAILED
            recording.stopped_at = recording.stopped_at or timezone.now()
            recording.error_message = msg[:2000]
            recording.save(update_fields=["status", "stopped_at", "error_message"])
            return success(
                data=RecordingSerializer(recording).data,
                message="Recording had already ended on the media server.",
            )  

        return error(
            code="LIVEKIT_UPSTREAM_ERROR",
            message="Could not stop recording.",
            status=502,
            details={"reason": str(exc)},
        )

    recording.status = RecordingStatus.STOPPING
    recording.stopped_at = timezone.now()
    recording.save(update_fields=["status", "stopped_at"])

    broadcast_class_event(
        cls.id,
        {
            "type": "recording.stopped",
            "data": {
                "recording_id": recording.id,
                "stopped_at": recording.stopped_at.isoformat().replace("+00:00", "Z"),
            },
        },
    )

    return success(
        data=RecordingSerializer(recording).data,
        message="Recording stopping",
    )


def _can_view_recordings(user, cls: LiveClass) -> bool:
    if user.role == "teacher" and cls.teacher_id == user.user_id:
        return True
    return ClassParticipant.objects.filter(
        live_class=cls,
        user_id=user.user_id,
        status__in=[ParticipantStatus.ACTIVE, ParticipantStatus.LEFT],
    ).exists()


@api_view(["GET"])
def recording_current(request, class_id: int):
    try:
        cls = LiveClass.objects.get(pk=class_id)
    except LiveClass.DoesNotExist:
        raise NotFound("Class not found.")

    if not _can_view_recordings(request.user, cls):
        raise PermissionDenied("You do not have access to this class.")

    recording = (
        ClassRecording.objects
        .filter(live_class=cls)
        .order_by("-started_at")
        .first()
    )
    if recording is None:
        return success(data=None, message="No recording for this class")

    return success(
        data=RecordingSerializer(recording).data,
        message="Recording state fetched",
    )


@api_view(["GET"])
def recording_list(request, class_id: int):
    try:
        cls = LiveClass.objects.get(pk=class_id)
    except LiveClass.DoesNotExist:
        raise NotFound("Class not found.")

    if not _can_view_recordings(request.user, cls):
        raise PermissionDenied("You do not have access to this class.")

    qs = ClassRecording.objects.filter(live_class=cls).order_by("-started_at")
    results, pagination = _paginate(request, qs)
    serializer = RecordingSerializer(results, many=True)

    return success(
        data={"results": serializer.data, "pagination": pagination},
        message="Recordings fetched",
    )



@api_view(["GET"])
def recording_list(request, class_id: int):
    try:
        cls = LiveClass.objects.get(pk=class_id)
    except LiveClass.DoesNotExist:
        raise NotFound("Class not found.")

    if not _can_view_recordings(request.user, cls):
        raise PermissionDenied("You do not have access to this class.")

    include_playback = request.query_params.get("include_playback", "false").lower() in ("1", "true", "yes")

    try:
        ttl = int(request.query_params.get("ttl", settings.S3_PRESIGNED_URL_TTL_SECONDS))
    except (TypeError, ValueError):
        ttl = settings.S3_PRESIGNED_URL_TTL_SECONDS

    qs = ClassRecording.objects.filter(live_class=cls).order_by("-started_at")
    results, pagination = _paginate(request, qs)

    serializer = RecordingSerializer(
        results,
        many=True,
        context={
            "include_playback": include_playback,
            "playback_ttl": ttl,
        },
    )

    return success(
        data={"results": serializer.data, "pagination": pagination},
        message="Recordings fetched",
    )



# ---------------------------------------------------------------------------
# Playback URL for a single recording
# ---------------------------------------------------------------------------
@api_view(["GET"])
def recording_playback_url(request, class_id: int, recording_id: int):
    try:
        cls = LiveClass.objects.get(pk=class_id)
    except LiveClass.DoesNotExist:
        raise NotFound("Class not found.")

    if not _can_view_recordings(request.user, cls):
        raise PermissionDenied("You do not have access to this class.")

    try:
        recording = ClassRecording.objects.get(pk=recording_id, live_class=cls)
    except ClassRecording.DoesNotExist:
        raise NotFound("Recording not found.")

    if recording.status != RecordingStatus.COMPLETED:
        return error(
            code="RECORDING_NOT_COMPLETED",
            message=f"Recording is not completed (status={recording.status}).",
            status=409,
        )

    if not recording.storage_key:
        return error(
            code="RECORDING_MISSING_STORAGE_KEY",
            message="Recording is marked completed but has no storage key.",
            status=409,
        )

    try:
        ttl = int(request.query_params.get("ttl", settings.S3_PRESIGNED_URL_TTL_SECONDS))
    except (TypeError, ValueError):
        ttl = settings.S3_PRESIGNED_URL_TTL_SECONDS
    ttl = max(60, min(ttl, 86400))

    try:
        url, expires_at = s3_storage.generate_presigned_get_url(
            key=recording.storage_key,
            ttl_seconds=ttl,
            bucket=recording.bucket or settings.S3_BUCKET,
        )
    except S3ConfigurationError as exc:
        return error(
            code="S3_NOT_CONFIGURED",
            message="S3 is not configured on the server.",
            status=500,
            details={"reason": str(exc)},
        )
    except S3ServiceError as exc:
        return error(
            code="S3_UPSTREAM_ERROR",
            message="Could not generate a playback URL.",
            status=502,
            details={"reason": str(exc)},
        )

    return success(
        data={
            "recording_id": recording.id,
            "class_id": cls.id,
            "status": recording.status,
            "playback_url": url,
            "expires_in_seconds": ttl,
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        },
        message="Playback URL generated",
    )