"""
ASGI entrypoint for HTTP + WebSocket.

HTTP goes through Django's normal ASGI app.
WebSocket goes through Channels routing.

Auth for WebSockets is done inside the consumer stack via
apps.realtime.auth.populate_auth_user_from_token.
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.asgi import get_asgi_application

# Initialize Django ASGI application early so that anything imported
# after this point (routing, consumers) can safely import models.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from apps.realtime.auth import WebSocketAuthMiddleware  # noqa: E402
from apps.realtime.routing import websocket_urlpatterns  # noqa: E402


application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": WebSocketAuthMiddleware(URLRouter(websocket_urlpatterns)),
    }
)