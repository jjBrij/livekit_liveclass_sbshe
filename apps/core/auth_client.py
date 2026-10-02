
import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)
AUTH_SERVER_TIMEOUT = 5


class AuthServerError(Exception):
    """Raised when the Auth Server is unreachable or returns an unexpected response."""


class AuthUser:

    __slots__ = ("user_id", "role", "name")

    def __init__(self, user_id: int, role: str, name: str):
        self.user_id = user_id
        self.role = role
        self.name = name

    @property
    def is_teacher(self) -> bool:
        return self.role == "teacher"

    @property
    def is_student(self) -> bool:
        return self.role == "student"

    def __repr__(self) -> str:
        return f"<AuthUser id={self.user_id} role={self.role} name={self.name!r}>"


def validate_token_with_auth_server(token: str) -> AuthUser | None:
    url = f"{settings.AUTH_SERVER_URL.rstrip('/')}/api/auth/me/"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=AUTH_SERVER_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.error("Auth Server unreachable: %s", exc)
        raise AuthServerError("Auth Server is unreachable") from exc

    # Token is invalid or expired
    if response.status_code in (401, 403):
        return None

    # Auth server problem
    if response.status_code != 200:
        logger.error(
            "Auth Server returned unexpected status %s: %s",
            response.status_code,
            response.text[:200],
        )
        raise AuthServerError(
            f"Auth Server returned status {response.status_code}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        logger.error("Auth Server returned non-JSON response")
        raise AuthServerError(
            "Auth Server returned invalid JSON"
        ) from exc

    # Your Auth API response:
    #
    # {
    #     "success": true,
    #     "message": "...",
    #     "data": {...}
    # }

    if not payload.get("success"):
        return None

    data = payload.get("data")

    if not data:
        logger.error(
            "Auth Server response missing data: %s",
            payload,
        )
        raise AuthServerError(
            "Auth Server returned incomplete user data"
        )

    user_id = data.get("id")
    role = data.get("role")
    name = data.get("name", "")

    if user_id is None or not role:
        logger.error(
            "Auth Server response missing user id or role: %s",
            payload,
        )
        raise AuthServerError(
            "Auth Server returned incomplete user data"
        )

    return AuthUser(
        user_id=int(user_id),
        role=str(role),
        name=str(name),
    )