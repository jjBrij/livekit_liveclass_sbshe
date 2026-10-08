
import json
import logging
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from apps.classes.models import ClassRecording, RecordingStatus
from apps.realtime.broadcast import broadcast_class_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------
def _verify_webhook(auth_header: str, body: bytes) -> dict | None:
    
    raw = (auth_header or "").strip()
    if raw.lower().startswith("bearer "):
        token = raw[7:].strip()
    else:
        token = raw    

    try:
        from livekit import api as lk_api
        verifier = lk_api.TokenVerifier(
            api_key=settings.LIVEKIT_API_KEY,
            api_secret=settings.LIVEKIT_API_SECRET,
        )
        receiver = lk_api.WebhookReceiver(verifier)
        event = receiver.receive(body.decode("utf-8"), token)
       
        return event
    except ImportError:
        logger.error("livekit.api.WebhookReceiver not available")
        return None
    except Exception as exc:
        logger.warning("Webhook verification failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------
def _parse_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        # Egress sends RFC3339 like '2026-10-01T12:35:00.000Z'
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main webhook
# ---------------------------------------------------------------------------
@csrf_exempt
@require_POST
def egress_webhook(request):
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    logger.warning("DEBUG webhook auth_header=%r body_len=%d", auth_header[:80], len(request.body or b""))
    body = request.body or b""

    event = _verify_webhook(auth_header, body)
    if event is None:
        # Do NOT leak why verification failed.
        return HttpResponseBadRequest("invalid signature")

    event_type = getattr(event, "event", "")  # e.g., "egress_started"
    egress_info = getattr(event, "egress_info", None)

    if egress_info is None:
        return HttpResponse(status=200)

    egress_id = getattr(egress_info, "egress_id", None)
    if not egress_id:
        return HttpResponse(status=200)

    try:
        recording = ClassRecording.objects.get(egress_id=egress_id)
    except ClassRecording.DoesNotExist:
        # Unknown egress; ignore but log for visibility.
        logger.info("Egress webhook for unknown egress_id=%s type=%s", egress_id, event_type)
        return HttpResponse(status=200)

    status_str = str(getattr(egress_info, "status", "")).upper()

    if event_type == "egress_ended":
        _handle_egress_ended(recording, egress_info)
    elif event_type in ("egress_started", "egress_updated"):
        _handle_egress_in_progress(recording, egress_info, status_str)
    else:
        logger.debug("Unhandled egress event: %s", event_type)

    return HttpResponse(status=200)


def _handle_egress_in_progress(recording: ClassRecording, info, status_str: str):
    """
    Egress is running or transitioning. Sync the status if we can.
    """
    if status_str == "EGRESS_ACTIVE" and recording.status == RecordingStatus.STARTING:
        recording.status = RecordingStatus.ACTIVE
        recording.save(update_fields=["status"])
        broadcast_class_event(
            recording.live_class_id,
            {
                "type": "recording.started",
                "data": {
                    "recording_id": recording.id,
                    "started_at": timezone.now().isoformat().replace("+00:00", "Z"),
                },
            },
        )
    # No other transitions matter here; we rely on egress_ended for closure.


def _handle_egress_ended(recording: ClassRecording, info):
    """
    Final update: status, file details, storage key.
    """
    updates = {}

    if str(getattr(info, "status", "")).upper() == "EGRESS_FAILED":
        updates["status"] = RecordingStatus.FAILED
        updates["error_message"] = str(getattr(info, "error", ""))[:2000]
    else:
        updates["status"] = RecordingStatus.COMPLETED

    # File details
    file_results = getattr(info, "file", None)
    if file_results is None:
        # Some SDK versions use file_results list
        file_results = getattr(info, "file_results", None)

    # LiveKit sends either a single `file` or a list `file_results`.
    files = []
    if file_results is not None:
        if isinstance(file_results, (list, tuple)):
            files = list(file_results)
        else:
            files = [file_results]

    if files:
        f = files[0]
        updates["file_size_bytes"] = int(getattr(f, "size", 0) or 0)
        # Duration is in nanoseconds
        dur_ns = int(getattr(f, "duration", 0) or 0)
        updates["duration_seconds"] = int(dur_ns / 1_000_000_000) if dur_ns else None
        # The filename in S3; LiveKit names the object.
        updates["storage_key"] = str(getattr(f, "filename", "") or "")[:512]

    # Bucket comes from our own settings (Egress uploaded to our configured bucket).
    updates["bucket"] = settings.S3_BUCKET or ""

    stopped = _parse_dt(getattr(info, "ended_at", None))
    if stopped is not None:
        updates["stopped_at"] = stopped

    for k, v in updates.items():
        setattr(recording, k, v)
    recording.save(update_fields=list(updates.keys()) + ["updated_at"])

    if recording.status == RecordingStatus.COMPLETED:
        broadcast_class_event(
            recording.live_class_id,
            {
                "type": "recording.completed",
                "data": {
                    "recording_id": recording.id,
                    "duration_seconds": recording.duration_seconds,
                    "file_size_bytes": recording.file_size_bytes,
                    "playback_url": None,  # Block 15 fills this
                },
            },
        )
    else:
        broadcast_class_event(
            recording.live_class_id,
            {
                "type": "recording.failed",
                "data": {
                    "recording_id": recording.id,
                    "reason": recording.error_message,
                },
            },
        )