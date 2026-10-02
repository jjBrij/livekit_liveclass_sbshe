"""
Custom DRF exception handler.

Wraps DRF-raised exceptions (ValidationError, NotFound, PermissionDenied,
AuthenticationFailed, etc.) into the standard envelope so clients always
see the same response shape.
"""

from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework import exceptions as drf_exc


# Map DRF exception classes to our error codes and HTTP statuses.
# Kept intentionally small and explicit.
_CODE_MAP = {
    drf_exc.ValidationError: ("VALIDATION_ERROR", 400),
    drf_exc.AuthenticationFailed: ("UNAUTHENTICATED", 401),
    drf_exc.NotAuthenticated: ("UNAUTHENTICATED", 401),
    drf_exc.PermissionDenied: ("FORBIDDEN", 403),
    drf_exc.NotFound: ("NOT_FOUND", 404),
    drf_exc.MethodNotAllowed: ("METHOD_NOT_ALLOWED", 405),
    drf_exc.NotAcceptable: ("NOT_ACCEPTABLE", 406),
    drf_exc.UnsupportedMediaType: ("UNSUPPORTED_MEDIA_TYPE", 415),
    drf_exc.Throttled: ("THROTTLED", 429),
}


def envelope_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)

    # Not a DRF exception (e.g., a real 500). Let Django handle it.
    if response is None:
        return None

    # Find our code + message
    code = "ERROR"
    message = "An error occurred"

    for exc_class, (mapped_code, _) in _CODE_MAP.items():
        if isinstance(exc, exc_class):
            code = mapped_code
            message = _message_from_exc(exc)
            break

    # Preserve DRF details (field errors, etc.)
    details = response.data
    if isinstance(details, list):
        details = {"non_field_errors": details}
    elif not isinstance(details, dict):
        details = {"detail": details}

    response.data = {
        "success": False,
        "message": message,
        "data": None,
        "error": {
            "code": code,
            "details": details,
        },
    }
    return response


def _message_from_exc(exc) -> str:
    """
    Build a human-readable message from a DRF exception.
    Preference order:
        1. exc.detail if it is a plain string
        2. first field error if exc.detail is a dict
        3. a fallback based on the exception class
    """
    detail = getattr(exc, "detail", None)

    if isinstance(detail, str):
        return detail

    if isinstance(detail, dict):
        for value in detail.values():
            if isinstance(value, (list, tuple)) and value:
                return str(value[0])
            if isinstance(value, str):
                return value

    if isinstance(detail, (list, tuple)) and detail:
        return str(detail[0])

    return exc.__class__.__name__