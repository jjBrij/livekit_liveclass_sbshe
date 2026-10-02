"""
LiveKit admin/debug endpoints.

These are NOT the join endpoints. Join is in Block 8.
"""

import logging

from rest_framework.decorators import api_view
from rest_framework.exceptions import PermissionDenied

from apps.core.responses import success, error

from . import service

logger = logging.getLogger(__name__)

from .webhooks import egress_webhook as _egress_webhook
egress_webhook = _egress_webhook   # re-export for URLs


@api_view(["GET"])
def livekit_ping(request):
    """
    Verify Django can reach the LiveKit server API.

    Any authenticated user may call this during development.
    In production, restrict to teachers/admins (Block 19).
    """
    result = service.ping()

    if result["reachable"]:
        return success(
            data={
                "livekit_url": "***",  # do not leak the internal URL
                "rooms_count": result["rooms_count"],
            },
            message="LiveKit server is reachable",
        )

    return error(
        code="LIVEKIT_UNREACHABLE",
        message="LiveKit server is not reachable",
        status=503,
        details={"reason": result["error"]},
    )


@api_view(["GET"])
def livekit_list_rooms(request):
    """
    List active LiveKit rooms.

    Teacher-only. Useful for debugging and for the "live classes"
    dashboard. Not used by students.
    """
    if not getattr(request.user, "is_teacher", False):
        raise PermissionDenied("Teacher role required.")

    try:
        rooms = service.list_rooms()
    except service.LiveKitServiceError as exc:
        return error(
            code="LIVEKIT_ERROR",
            message="Failed to list LiveKit rooms",
            status=503,
            details={"reason": str(exc)},
        )

    data = [
        {
            "name": room.name,
            "num_participants": room.num_participants,
            "creation_time": room.creation_time,
            "empty_timeout": room.empty_timeout,
        }
        for room in rooms
    ]

    return success(
        data={"rooms": data, "total": len(data)},
        message="LiveKit rooms fetched",
    )