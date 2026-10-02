
import logging
from urllib.parse import parse_qs
from channels.db import database_sync_to_async
from apps.core.auth_client import (
    AuthUser,
    AuthServerError,
    validate_token_with_auth_server,
)

logger = logging.getLogger(__name__)


def _get_token_from_scope(scope) -> str | None:
    raw_qs = scope.get("query_string", b"")
    if not raw_qs:
        return None
    try:
        parsed = parse_qs(raw_qs.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    tokens = parsed.get("token")
    if not tokens:
        return None
    return tokens[0].strip()


class WebSocketAuthMiddleware:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        scope = dict(scope)  # do not mutate the caller's dict
        scope["auth_user"] = None
        scope["auth_error"] = None

        token = _get_token_from_scope(scope)

        if not token:
            scope["auth_error"] = "missing_token"
            return await self.inner(scope, receive, send)

        try:
            user: AuthUser | None = await database_sync_to_async(
                validate_token_with_auth_server, thread_sensitive=False
            )(token)
        except AuthServerError as exc:
            logger.warning("WebSocket auth: Auth Server error: %s", exc)
            scope["auth_error"] = "auth_server_unavailable"
            return await self.inner(scope, receive, send)

        if user is None:
            scope["auth_error"] = "invalid_token"
            return await self.inner(scope, receive, send)

        scope["auth_user"] = user
        scope["auth_token"] = token
        return await self.inner(scope, receive, send)