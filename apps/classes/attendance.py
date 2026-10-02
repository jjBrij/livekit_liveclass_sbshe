
import logging
from datetime import datetime, timezone as dt_timezone
from django.db import transaction
from django.utils import timezone
from .models import Attendance, ParticipantRole
logger = logging.getLogger(__name__)
IDEMPOTENCY_WINDOW_SECONDS = 5
def compute_attendance_status(total_seconds: int, duration_minutes: int) -> str:
    """
    Present  >= 75%
    Partial  >= 25% and < 75%
    Absent   < 25%
    """
    if duration_minutes <= 0:
        return "absent"
    planned_seconds = duration_minutes * 60
    ratio = total_seconds / planned_seconds

    if ratio >= 0.75:
        return "present"
    if ratio >= 0.25:
        return "partial"
    return "absent"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(s) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    # Accept trailing Z
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _get_or_create_row(user, cls) -> Attendance:
    row, created = Attendance.objects.get_or_create(
        live_class=cls,
        user_id=user.user_id,
        defaults={
            "name": user.name or f"User {user.user_id}",
            "role": user.role,
            "sessions": [],
            "total_duration_seconds": 0,
        },
    )
    if not created:
        # Keep display name/role fresh
        row.name = user.name or row.name
        row.role = user.role or row.role
    return row


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
@transaction.atomic
def record_join(user, cls) -> Attendance:
    """
    Called when a user starts a session in a class.
    Idempotent within IDEMPOTENCY_WINDOW_SECONDS.
    """
    now = timezone.now()
    row = _get_or_create_row(user, cls)

    sessions = list(row.sessions or [])
    last = sessions[-1] if sessions else None

    # Case 1: last session already open. Do not create a new one.
    if last and last.get("left_at") is None:
        logger.debug(
            "attendance.record_join: open session exists (user=%s class=%s)",
            user.user_id, cls.id,
        )
        row.save(update_fields=["name", "role"])
        return row

    # Case 2: last session ended very recently. Extend it instead of
    # starting a new one.
    if last and last.get("left_at"):
        last_left = _parse_iso(last["left_at"])
        if last_left and (now - last_left).total_seconds() <= IDEMPOTENCY_WINDOW_SECONDS:
            last["left_at"] = None
            sessions[-1] = last
            row.sessions = sessions
            row.last_left_at = None
            if row.first_joined_at is None:
                row.first_joined_at = now
            row.save(update_fields=["sessions", "last_left_at", "first_joined_at", "name", "role"])
            return row

    # Case 3: brand new session.
    sessions.append({"joined_at": _iso(now), "left_at": None})
    row.sessions = sessions
    if row.first_joined_at is None:
        row.first_joined_at = now
    row.last_left_at = None
    row.save(update_fields=["sessions", "first_joined_at", "last_left_at", "name", "role"])

    logger.info(
        "attendance.record_join user=%s class=%s", user.user_id, cls.id
    )
    return row


@transaction.atomic
def record_leave(user, cls) -> Attendance | None:
    now = timezone.now()
    try:
        row = Attendance.objects.select_for_update().get(
            live_class=cls, user_id=user.user_id
        )
    except Attendance.DoesNotExist:
        logger.debug(
            "attendance.record_leave: no row (user=%s class=%s)",
            user.user_id, cls.id,
        )
        return None

    sessions = list(row.sessions or [])
    if not sessions:
        return row

    last = sessions[-1]
    if last.get("left_at") is not None:
        # Already closed. Idempotent no-op.
        return row

    joined_at = _parse_iso(last.get("joined_at"))
    if joined_at is None:
        # Corrupt data; close with zero duration and move on.
        last["left_at"] = _iso(now)
        sessions[-1] = last
        row.sessions = sessions
        row.save(update_fields=["sessions"])
        return row

    last["left_at"] = _iso(now)
    sessions[-1] = last

    # Add session duration to the running total.
    session_seconds = max(0, int((now - joined_at).total_seconds()))
    row.sessions = sessions
    row.total_duration_seconds = (row.total_duration_seconds or 0) + session_seconds
    row.last_left_at = now
    row.save(update_fields=["sessions", "total_duration_seconds", "last_left_at"])

    logger.info(
        "attendance.record_leave user=%s class=%s session_secs=%s total_secs=%s",
        user.user_id, cls.id, session_seconds, row.total_duration_seconds,
    )
    return row