
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def class_group_name(class_id: int) -> str:
    return f"class_{class_id}"


def broadcast_class_event(class_id: int, event: dict) -> None:
    """
    Send `event` to the group for `class_id`.

    Fails silently (with a log) if Redis is unavailable, so a WebSocket
    outage does not break the HTTP request that triggered the broadcast.
    """
    channel_layer = get_channel_layer()
    if channel_layer is None:
        logger.error("Channel layer is not configured; dropping event %s", event)
        return

    message = {"type": "class.event", "event": event}
    try:
        async_to_sync(channel_layer.group_send)(class_group_name(class_id), message)
    except Exception:
        logger.exception("Failed to broadcast event to class %s", class_id)