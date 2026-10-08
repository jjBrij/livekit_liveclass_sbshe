
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from django.conf import settings

from livekit import api as lk_api

logger = logging.getLogger(__name__)


class LiveKitConfigurationError(Exception):
    """Raised when LiveKit settings are missing or invalid."""


class LiveKitServiceError(Exception):
    """Raised when the LiveKit server API returns an error or is unreachable."""


# ---------------------------------------------------------------------------
# Configuration checks
# ---------------------------------------------------------------------------
def _require_config():
    if not settings.LIVEKIT_URL:
        raise LiveKitConfigurationError("LIVEKIT_URL is not set.")
    if not settings.LIVEKIT_API_KEY:
        raise LiveKitConfigurationError("LIVEKIT_API_KEY is not set.")
    if not settings.LIVEKIT_API_SECRET:
        raise LiveKitConfigurationError("LIVEKIT_API_SECRET is not set.")


def _require_s3_config():
    if not getattr(settings, "S3_BUCKET", ""):
        raise LiveKitConfigurationError("S3_BUCKET is not set for recording.")


# ---------------------------------------------------------------------------
# LiveKit API client factory
#
# Some versions of livekit-api do not support `async with LiveKitAPI(...)`.
# This helper works with both styles: it constructs the client and yields
# it, and tries to close it on exit if a close method exists.
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _lk_api():
    _require_config()
    client = lk_api.LiveKitAPI(
        url=settings.LIVEKIT_HTTP_URL,
        api_key=settings.LIVEKIT_API_KEY,
        api_secret=settings.LIVEKIT_API_SECRET,
    )
    try:
        yield client
    finally:
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close is not None:
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.debug("LiveKitAPI close raised; ignoring", exc_info=True)


def generate_participant_token(
    *,
    identity: str,
    name: str,
    room_name: str,
    role: str = "student",
    ttl_seconds: Optional[int] = None,
    can_publish: bool = True,
    can_publish_data: bool = True,
    can_subscribe: bool = True,
) -> str:
    """
    Mint a LiveKit JWT for a participant to join a room.

    Uses with_ttl(timedelta) which is what this SDK version requires.
    """
    from datetime import timedelta

    _require_config()

    ttl = ttl_seconds or settings.LIVEKIT_TOKEN_TTL_SECONDS

    try:
        grants = lk_api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=can_publish,
            can_publish_data=can_publish_data,
            can_subscribe=can_subscribe,
        )

        token = (
            lk_api.AccessToken(
                settings.LIVEKIT_API_KEY,
                settings.LIVEKIT_API_SECRET,
            )
            .with_identity(identity)
            .with_name(name)
            .with_grants(grants)
        )

        if hasattr(token, "with_metadata"):
            token = token.with_metadata(f'{{"role":"{role}"}}')

        # This SDK takes a timedelta, not seconds.
        token = token.with_ttl(timedelta(seconds=int(ttl)))

        return token.to_jwt()
    except Exception as exc:
        logger.exception("Failed to generate LiveKit token for %s", identity)
        raise LiveKitServiceError(f"Token generation failed: {exc}") from exc
# ---------------------------------------------------------------------------
# Room management
# ---------------------------------------------------------------------------
async def _create_room_async(*, room_name: str, empty_timeout: int = 300, max_participants: int = 0):
    async with _lk_api() as lkapi:
        request = lk_api.CreateRoomRequest(
            name=room_name,
            empty_timeout=empty_timeout,
            max_participants=max_participants,
        )
        return await lkapi.room.create_room(request)


async def _list_rooms_async():
    async with _lk_api() as lkapi:
        response = await lkapi.room.list_rooms(lk_api.ListRoomsRequest())
        return response.rooms


def create_room(*, room_name: str, empty_timeout: int = 300, max_participants: int = 0):
    try:
        return asyncio.run(_create_room_async(
            room_name=room_name,
            empty_timeout=empty_timeout,
            max_participants=max_participants,
        ))
    except Exception as exc:
        logger.exception("Failed to create LiveKit room %s", room_name)
        raise LiveKitServiceError(f"Room creation failed: {exc}") from exc


def list_rooms():
    try:
        return asyncio.run(_list_rooms_async())
    except Exception as exc:
        logger.exception("Failed to list LiveKit rooms")
        raise LiveKitServiceError(f"Room listing failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------
async def _list_participants_async(room_name: str):
    async with _lk_api() as lkapi:
        request = lk_api.ListParticipantsRequest(room=room_name)
        response = await lkapi.room.list_participants(request)
        return response.participants


def list_participants(room_name: str):
    try:
        return asyncio.run(_list_participants_async(room_name))
    except Exception as exc:
        logger.exception("Failed to list participants in %s", room_name)
        raise LiveKitServiceError(f"List participants failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Participant permissions
# ---------------------------------------------------------------------------
async def _update_participant_permissions_async(
    *,
    room_name: str,
    identity: str,
    can_publish: bool,
    can_publish_data: bool,
    can_subscribe: bool,
):
    _require_config()
    async with lk_api.LiveKitAPI(
        url=settings.LIVEKIT_HTTP_URL,
        api_key=settings.LIVEKIT_API_KEY,
        api_secret=settings.LIVEKIT_API_SECRET,
    ) as lkapi:
        request = lk_api.UpdateParticipantRequest(
            room=room_name,
            identity=identity,
            permission=lk_api.ParticipantPermission(
                can_publish=can_publish,
                can_publish_data=can_publish_data,
                can_subscribe=can_subscribe,
            ),
        )
        return await lkapi.room.update_participant(request)


def update_participant_permissions(
    *,
    room_name: str,
    identity: str,
    can_publish: bool,
    can_publish_data: bool = True,
    can_subscribe: bool = True,
):
    """
    Update a participant's LiveKit grants in place, without forcing a
    reconnect.

    Raises LiveKitServiceError on failure. Callers should typically
    tolerate failure and log it (the student will just not be able to
    publish until a manual fix).
    """
    try:
        return asyncio.run(
            _update_participant_permissions_async(
                room_name=room_name,
                identity=identity,
                can_publish=can_publish,
                can_publish_data=can_publish_data,
                can_subscribe=can_subscribe,
            )
        )
    except Exception as exc:
        logger.exception(
            "Failed to update LiveKit permissions for %s in %s", identity, room_name
        )
        raise LiveKitServiceError(f"Permission update failed: {exc}") from exc

# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------
async def _mute_track_async(*, room_name: str, identity: str, track_sid: str, muted: bool):
    async with _lk_api() as lkapi:
        request = lk_api.MuteRoomTrackRequest(
            room=room_name,
            identity=identity,
            track_sid=track_sid,
            muted=muted,
        )
        return await lkapi.room.mute_published_track(request)


def mute_track(*, room_name: str, identity: str, track_sid: str, muted: bool):
    try:
        return asyncio.run(_mute_track_async(
            room_name=room_name, identity=identity, track_sid=track_sid, muted=muted,
        ))
    except Exception as exc:
        logger.exception("Failed to mute track %s (%s) in %s", track_sid, identity, room_name)
        raise LiveKitServiceError(f"Mute track failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Room / participant removal
# ---------------------------------------------------------------------------
async def _remove_participant_async(*, room_name: str, identity: str):
    async with _lk_api() as lkapi:
        request = lk_api.RoomParticipantIdentity(room=room_name, identity=identity)
        return await lkapi.room.remove_participant(request)


def remove_participant(*, room_name: str, identity: str):
    try:
        return asyncio.run(_remove_participant_async(room_name=room_name, identity=identity))
    except Exception as exc:
        logger.exception("Failed to remove participant %s in %s", identity, room_name)
        raise LiveKitServiceError(f"Remove participant failed: {exc}") from exc


async def _delete_room_async(room_name: str):
    async with _lk_api() as lkapi:
        request = lk_api.DeleteRoomRequest(room=room_name)
        return await lkapi.room.delete_room(request)


def delete_room(room_name: str):
    try:
        return asyncio.run(_delete_room_async(room_name))
    except Exception as exc:
        logger.exception("Failed to delete room %s", room_name)
        raise LiveKitServiceError(f"Delete room failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Egress (recording)
# ---------------------------------------------------------------------------
async def _start_room_composite_egress_async(*, room_name: str, layout: str = "grid", audio_only: bool = False):
    _require_s3_config()
    async with _lk_api() as lkapi:
        s3 = lk_api.S3Upload(
            access_key=settings.S3_ACCESS_KEY,
            secret=settings.S3_SECRET_KEY,
            region=settings.S3_REGION or None,
            bucket=settings.S3_BUCKET,
            endpoint=settings.S3_ENDPOINT_URL or None,
            force_path_style=bool(settings.S3_ENDPOINT_URL),
        )

        file_output = lk_api.EncodedFileOutput(
            file_type=lk_api.EncodedFileType.MP4,
            filepath=f"recordings/{{room_name}}/{{time}}.mp4",
            s3=s3,
        )

        if audio_only:
            raise LiveKitConfigurationError("Audio-only recording is not enabled yet.")

        request = lk_api.RoomCompositeEgressRequest(
            room_name=room_name,
            layout=layout,
            audio_only=False,
            video_only=False,
            file_outputs=[file_output],
        )
        return await lkapi.egress.start_room_composite_egress(request)


async def _stop_egress_async(egress_id: str):
    async with _lk_api() as lkapi:
        return await lkapi.egress.stop_egress(
            lk_api.StopEgressRequest(egress_id=egress_id)
        )


def start_room_composite_egress(*, room_name: str, layout: str = "grid", audio_only: bool = False):
    try:
        return asyncio.run(_start_room_composite_egress_async(
            room_name=room_name, layout=layout, audio_only=audio_only,
        ))
    except Exception as exc:
        logger.exception("Failed to start Egress for room %s", room_name)
        raise LiveKitServiceError(f"Start egress failed: {exc}") from exc


def stop_egress(egress_id: str):
    try:
        return asyncio.run(_stop_egress_async(egress_id))
    except Exception as exc:
        logger.exception("Failed to stop Egress %s", egress_id)
        raise LiveKitServiceError(f"Stop egress failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Connectivity check
# ---------------------------------------------------------------------------
def ping() -> dict:
    _require_config()
    try:
        rooms = list_rooms()
        return {"reachable": True, "rooms_count": len(rooms), "error": None}
    except LiveKitServiceError as exc:
        return {"reachable": False, "rooms_count": None, "error": str(exc)}

# ---------------------------------------------------------------------------
# Track and participant operations
# ---------------------------------------------------------------------------
async def _list_participants_async(room_name: str):
    async with _lk_api() as lkapi:
        request = lk_api.ListParticipantsRequest(room=room_name)
        response = await lkapi.room.list_participants(request)
        return response.participants

async def _mute_track_async(*, room_name: str, identity: str, track_sid: str, muted: bool):
    _require_config()
    async with lk_api.LiveKitAPI(
        url=settings.LIVEKIT_HTTP_URL,
        api_key=settings.LIVEKIT_API_KEY,
        api_secret=settings.LIVEKIT_API_SECRET,
    ) as lkapi:
        request = lk_api.MuteRoomTrackRequest(
            room=room_name,
            identity=identity,
            track_sid=track_sid,
            muted=muted,
        )
        return await lkapi.room.mute_published_track(request)


async def _remove_participant_async(*, room_name: str, identity: str):
    _require_config()
    async with lk_api.LiveKitAPI(
        url=settings.LIVEKIT_HTTP_URL,
        api_key=settings.LIVEKIT_API_KEY,
        api_secret=settings.LIVEKIT_API_SECRET,
    ) as lkapi:
        request = lk_api.RoomParticipantIdentity(room=room_name, identity=identity)
        return await lkapi.room.remove_participant(request)


async def _delete_room_async(room_name: str):
    _require_config()
    async with lk_api.LiveKitAPI(
        url=settings.LIVEKIT_HTTP_URL,
        api_key=settings.LIVEKIT_API_KEY,
        api_secret=settings.LIVEKIT_API_SECRET,
    ) as lkapi:
        request = lk_api.DeleteRoomRequest(room=room_name)
        return await lkapi.room.delete_room(request)


def list_participants(room_name: str):
    try:
        return asyncio.run(_list_participants_async(room_name))
    except Exception as exc:
        logger.exception("Failed to list participants in %s", room_name)
        raise LiveKitServiceError(f"List participants failed: {exc}") from exc


def mute_track(*, room_name: str, identity: str, track_sid: str, muted: bool):
    try:
        return asyncio.run(_mute_track_async(
            room_name=room_name, identity=identity, track_sid=track_sid, muted=muted,
        ))
    except Exception as exc:
        logger.exception("Failed to mute track %s (%s) in %s", track_sid, identity, room_name)
        raise LiveKitServiceError(f"Mute track failed: {exc}") from exc


def remove_participant(*, room_name: str, identity: str):
    try:
        return asyncio.run(_remove_participant_async(room_name=room_name, identity=identity))
    except Exception as exc:
        logger.exception("Failed to remove participant %s in %s", identity, room_name)
        raise LiveKitServiceError(f"Remove participant failed: {exc}") from exc


def delete_room(room_name: str):
    try:
        return asyncio.run(_delete_room_async(room_name))
    except Exception as exc:
        logger.exception("Failed to delete room %s", room_name)
        raise LiveKitServiceError(f"Delete room failed: {exc}") from exc    

# ---------------------------------------------------------------------------
# Egress
# ---------------------------------------------------------------------------
def _require_s3_config():
    missing = [
        name for name in ("S3_BUCKET",)
        if not getattr(settings, name, "")
    ]
    if missing:
        raise LiveKitConfigurationError(
            f"Missing S3 config for recording: {', '.join(missing)}"
        )


async def _start_room_composite_egress_async(
    *,
    room_name: str,
    layout: str = "grid",
    audio_only: bool = False,
):
    _require_config()
    _require_s3_config()

    async with _lk_api() as lkapi:
        s3 = lk_api.S3Upload(
            access_key=settings.S3_ACCESS_KEY,
            secret=settings.S3_SECRET_KEY,
            region=settings.S3_REGION,
            bucket=settings.S3_BUCKET,
            endpoint=settings.S3_ENDPOINT_URL or "",
            force_path_style=bool(settings.S3_ENDPOINT_URL),
        )

        file_output = lk_api.EncodedFileOutput(
            file_type=lk_api.EncodedFileType.MP4,
            filepath=f"recordings/{{room_name}}/{{time}}.mp4",
            s3=s3,
        )

        if audio_only:
            raise LiveKitConfigurationError("Audio-only recording is not enabled yet.")

        request = lk_api.RoomCompositeEgressRequest(
            room_name=room_name,
            layout=layout,
            audio_only=False,
            video_only=False,
            file_outputs=[file_output],
        )
        return await lkapi.egress.start_room_composite_egress(request)

async def _stop_egress_async(egress_id: str):
    async with _lk_api() as lkapi:
        return await lkapi.egress.stop_egress(
            lk_api.StopEgressRequest(egress_id=egress_id)
        )

def start_room_composite_egress(
    *,
    room_name: str,
    layout: str = "grid",
    audio_only: bool = False,
):
    """
    Start a room composite recording. Returns the EgressInfo object from
    LiveKit, which includes the egress_id.
    """
    try:
        return asyncio.run(_start_room_composite_egress_async(
            room_name=room_name, layout=layout, audio_only=audio_only,
        ))
    except Exception as exc:
        logger.exception("Failed to start Egress for room %s", room_name)
        raise LiveKitServiceError(f"Start egress failed: {exc}") from exc


def stop_egress(egress_id: str):
    try:
        return asyncio.run(_stop_egress_async(egress_id))
    except Exception as exc:
        logger.exception("Failed to stop Egress %s", egress_id)
        raise LiveKitServiceError(f"Stop egress failed: {exc}") from exc

    