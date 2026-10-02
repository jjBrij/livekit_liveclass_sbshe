"""
Core utility endpoints.
"""

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


def health_check(request):
    """
    Public health-check endpoint. Never requires authentication.
    Never touches the database, Redis, or the Auth Server.
    """
    return JsonResponse(
        {
            "success": True,
            "message": "Live Class backend is running",
            "data": {
                "service": "sbshe-liveclass",
                "version": "0.1.0",
            },
            "error": None,
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def ping(request):
    """
    Public liveness endpoint that returns JSON via DRF.
    Kept separate from /health/ so health-check tooling
    (which may hit /health/ very frequently) does not go through DRF.
    """
    return Response(
        {
            "success": True,
            "message": "pong",
            "data": None,
            "error": None,
        }
    )


@api_view(["GET"])
def whoami(request):
    """
    Protected endpoint. Requires Authorization: Bearer <JWT>.

    Returns the identity that the Auth Server confirmed.
    Useful during development and as a smoke test for the integration.
    """
    user = request.user  # AuthUser set by AuthServerAuthentication
    return Response(
        {
            "success": True,
            "message": "Token is valid",
            "data": {
                "user_id": user.user_id,
                "role": user.role,
                "name": user.name,
            },
            "error": None,
        }
    )