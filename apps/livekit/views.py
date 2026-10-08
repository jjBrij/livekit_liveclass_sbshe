

import logging
from rest_framework.decorators import api_view
from rest_framework.exceptions import PermissionDenied
from apps.core.responses import success, error
from . import service
from rest_framework.exceptions import PermissionDenied
from . import storage as s3_storage
from .storage import S3ConfigurationError, S3ServiceError
from django.conf import settings
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


@api_view(["GET"])
def s3_ping(request):
    """
    Verify Django can reach the configured S3 bucket.

    Teacher-only. Useful during deployment and troubleshooting.
    """
    if not getattr(request.user, "is_teacher", False):
        raise PermissionDenied("Teacher role required.")

    try:
        result = s3_storage.head_bucket()
    except S3ConfigurationError as exc:
        return error(
            code="S3_NOT_CONFIGURED",
            message="S3 is not configured on the server.",
            status=500,
            details={"reason": str(exc)},
        )
    except S3ServiceError as exc:
        return error(
            code="S3_UNREACHABLE",
            message="S3 bucket is not reachable.",
            status=502,
            details={"reason": str(exc)},
        )

    return success(
        data={
            "bucket": settings.S3_BUCKET,
            "region": settings.S3_REGION,
            "endpoint": settings.S3_ENDPOINT_URL,
            **result,
        },
        message="S3 bucket is reachable",
    )