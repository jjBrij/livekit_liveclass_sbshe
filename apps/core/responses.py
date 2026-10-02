

from rest_framework.response import Response
def success(data=None, message: str = "OK", status: int = 200) -> Response:
    return Response(
        {
            "success": True,
            "message": message,
            "data": data,
            "error": None,
        },
        status=status,
    )


def error(
    code: str,
    message: str,
    status: int = 400,
    details=None,
) -> Response:
    return Response(
        {
            "success": False,
            "message": message,
            "data": None,
            "error": {
                "code": code,
                "details": details or {},
            },
        },
        status=status,
    )