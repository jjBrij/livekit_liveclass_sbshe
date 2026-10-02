
import logging
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from apps.classes.attendance import record_join
from apps.classes.models import (
    ClassParticipant,
    LiveClass,
    LiveClassStatus,
    ParticipantStatus,
)
from apps.realtime.rate_limit import check_rate_limit
from apps.classes.models import ClassMessage, MessageVisibility, MessageType

logger = logging.getLogger(__name__)
from apps.realtime import redis_state as rstate
from django.utils import timezone
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_CONFLICT = 4409


def class_group_name(class_id: int) -> str:
    return f"class_{class_id}"


class ClassConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.class_id = int(self.scope["url_route"]["kwargs"]["class_id"])
        self.group_name = class_group_name(self.class_id)

        user = self.scope.get("auth_user")
        if user is None:
            reason = self.scope.get("auth_error") or "unauthorized"
            await self.close(code=CLOSE_UNAUTHORIZED, reason=reason)
            return

        self.user = user

        # Load the class and check access rules.
        cls = await self._load_class(self.class_id)
        if cls is None:
            await self.close(code=CLOSE_NOT_FOUND, reason="class_not_found")
            return
        self._cls = cls
        allowed, access_reason = await self._check_access(cls, user)
        if not allowed:
            code = CLOSE_FORBIDDEN if access_reason != "not_joinable" else CLOSE_CONFLICT
            await self.close(code=code, reason=access_reason)
            return

        # Join the Redis-backed group.
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept() 
        try:
            await database_sync_to_async(record_join)(self.user, cls)
        except Exception:
            logger.exception(
                "attendance.record_join failed in consumer connect (class=%s user=%s)",
                self.class_id,
                self.user.user_id,
            )

        # Tell the connecting client who they are + the current class state.
        await self.send_json(
            {
                "type": "server.hello",
                "data": {
                    "class_id": self.class_id,
                    "room_name": cls.room_name,
                    "class_status": cls.status,
                    "user": {
                        "user_id": user.user_id,
                        "role": user.role,
                        "name": user.name,
                    },
                },
            }
        )
        await self._send_current_hands()
        # Notify the group that someone connected.
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "class.event",  # -> handler class_event()
                "event": {
                    "type": "participant.joined",
                    "data": {
                        "user_id": user.user_id,
                        "role": user.role,
                        "name": user.name,
                    },
                },
            },
        )

  
    # ------------------------------------------------------------------
    # Receive
    # ------------------------------------------------------------------
    async def receive_json(self, content, **kwargs):
        msg_type = content.get("type")

        if msg_type == "ping":
            await self.send_json({
                "type": "pong",
                "data": {},
            })
            return

        if msg_type == "raise_hand":
            await self._handle_raise_hand()
            return

        if msg_type == "lower_hand":
            await self._handle_lower_hand()
            return

        if msg_type == "chat.send":
            await self._handle_chat_send(
                content.get("data") or {}
            )
            return

        if msg_type is None:
            await self.send_json({
                "type": "error",
                "error": {
                    "code": "BAD_REQUEST",
                    "message": "Missing 'type'",
                },
            })
            return

        await self.send_json({
            "type": "error",
            "error": {
                "code": "UNSUPPORTED_EVENT",
                "message": f"Event type '{msg_type}' is not supported yet.",
            },
        })

    # ------------------------------------------------------------------
    # Raise / lower hand
    # ------------------------------------------------------------------
    async def _handle_raise_hand(self):
        user = self.user

        if user.role != "student":
            await self.send_json(
                {
                    "type": "error",
                    "error": {
                        "code": "ROLE_FORBIDDEN",
                        "message": "Only students can raise their hand.",
                    },
                }
            )
            return

        # If the teacher already rejected a recent request, do not re-raise
        # until the student uses a fresh session. For Block 11 we simply
        # check the rejected_hands map and refuse.
        existing_rejection = await database_sync_to_async(
            rstate.get_rejected_hand
        )(self.class_id, user.user_id)
        if existing_rejection:
            await self.send_json(
                {
                    "type": "error",
                    "error": {
                        "code": "HAND_ALREADY_REJECTED",
                        "message": "Your previous request was rejected.",
                        "details": existing_rejection,
                    },
                }
            )
            return

        existing = await database_sync_to_async(rstate.get_raised_hand)(
            self.class_id, user.user_id
        )
        if existing:
            # Idempotent — re-send the current state to the caller.
            await self.send_json(
                {"type": "hand_raised", "data": existing}
            )
            return

        payload = {
            "user_id": user.user_id,
            "name": user.name,
            "role": user.role,
            "raised_at": timezone.now().isoformat().replace("+00:00", "Z"),
        }
        await database_sync_to_async(rstate.add_raised_hand)(
            self.class_id, payload
        )

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "class.event",
                "event": {
                    "type": "hand_raised",
                    "data": payload,
                },
            },
        )

    async def _handle_lower_hand(self):
        user = self.user

        removed = await database_sync_to_async(rstate.get_raised_hand)(
            self.class_id, user.user_id
        )
        if not removed:
            # Idempotent — no error if it was already lowered.
            await self.send_json({"type": "hand_lowered", "data": {"user_id": user.user_id}})
            return

        await database_sync_to_async(rstate.remove_raised_hand)(
            self.class_id, user.user_id
        )

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "class.event",
                "event": {
                    "type": "hand_lowered",
                    "data": {"user_id": user.user_id},
                },
            },
        )

    # ------------------------------------------------------------------
    # On connect: send the current raise-hand state to the new client
    # ------------------------------------------------------------------
    async def _send_current_hands(self):
        """
        Called from connect() after accept(). Sends a snapshot so a
        late-joining teacher immediately sees the existing hands.
        """
        raised = await database_sync_to_async(rstate.list_raised_hands)(self.class_id)
        accepted = await database_sync_to_async(rstate.list_accepted_hands)(self.class_id)
        await self.send_json(
            {
                "type": "hands.snapshot",
                "data": {"raised": raised, "accepted": accepted},
            }
        )
    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------
    async def disconnect(self, close_code):
        # group_discard is safe even if we never joined (e.g., rejected at connect).
        try:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        except Exception:
            pass

        user = getattr(self, "user", None)
        if user is None:
            return

        # Broadcast participant.left so other clients update their UI.
        #
        # We deliberately do NOT call attendance.record_leave here.
        # A temporary disconnect (page reload, network blip, mobile app
        # backgrounding) should not close an attendance session. The
        # Celery task `sweep_and_flush_presence` closes the session once
        # Redis presence has expired for this user.
        try:
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "class.event",
                    "event": {
                        "type": "participant.left",
                        "data": {
                            "user_id": user.user_id,
                            "role": user.role,
                            "name": user.name,
                            "close_code": close_code,
                        },
                    },
                },
            )
        except Exception:
            logger.exception("Failed to broadcast participant.left")

    # ------------------------------------------------------------------
    # Group message handler
    # ------------------------------------------------------------------
    async def class_event(self, message):
        """
        Handler for messages sent to the group via
        channel_layer.group_send(..., {"type": "class.event", "event": {...}}).

        Forwards the inner "event" object as-is to the WebSocket.
        """
        event = message.get("event")
        if not event:
            return
        await self.send_json(event)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    @database_sync_to_async
    def _load_class(self, class_id: int):
        try:
            return LiveClass.objects.get(pk=class_id)
        except LiveClass.DoesNotExist:
            return None

    @database_sync_to_async
    def _check_access(self, cls: LiveClass, user):
        """
        Returns (allowed: bool, reason: str).
        """
        # Class must be joinable.
        if cls.status in (LiveClassStatus.ENDED, LiveClassStatus.CANCELLED):
            return (False, "not_joinable")

        if user.role == "teacher":
            if cls.teacher_id != user.user_id:
                return (False, "not_owner")
            return (True, "owner")

        if user.role == "student":
            participant = (
                ClassParticipant.objects.filter(
                   live_class=cls, user_id=user.user_id
            ).first()
            )

            if participant is None:
                return (False, "not_enrolled")

            if participant.status == ParticipantStatus.BANNED:
                return (False, "banned")
            if participant.status == ParticipantStatus.REMOVED:
                return (False, "removed")
            if participant.status not in (
                ParticipantStatus.ACTIVE,
                ParticipantStatus.LEFT,
            ):
                return (False, "not_enrolled")

            return (True, "participant")

        return (False, "unsupported_role")


        # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------
    CHAT_RATE_LIMIT = 5
    CHAT_RATE_WINDOW_SECONDS = 5
    CHAT_MAX_LENGTH = 2000

    async def _handle_chat_send(self, data: dict):
        user = self.user

        text = (data.get("text") or "").strip()
        if not text:
            await self.send_json(
                {
                    "type": "error",
                    "error": {"code": "VALIDATION_ERROR", "message": "Message text is required."},
                }
            )
            return

        if len(text) > self.CHAT_MAX_LENGTH:
            await self.send_json(
                {
                    "type": "error",
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": f"Message must be <= {self.CHAT_MAX_LENGTH} characters.",
                    },
                }
            )
            return

        # Visibility
        requested_visibility = (data.get("visibility") or "class").lower()
        if requested_visibility not in ("class", "teacher_only"):
            await self.send_json(
                {
                    "type": "error",
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": "visibility must be 'class' or 'teacher_only'.",
                    },
                }
            )
            return

        # Teachers sending "teacher_only" is meaningless; normalize.
        if user.role == "teacher" and requested_visibility == "teacher_only":
            requested_visibility = "class"

        # Only students may target teacher_only.
        if user.role == "student" and requested_visibility == "teacher_only":
            # allowed
            pass
        # If a future non-teacher, non-student role appears, reject here.

        # Rate limit (per user, per class)
        allowed, retry_after = await database_sync_to_async(check_rate_limit)(
            f"chat_rate:{self.class_id}:{user.user_id}",
            self.CHAT_RATE_LIMIT,
            self.CHAT_RATE_WINDOW_SECONDS,
        )
        if not allowed:
            await self.send_json(
                {
                    "type": "error",
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": "You are sending messages too quickly.",
                        "details": {"retry_after_seconds": retry_after},
                    },
                }
            )
            return

        # Persist
        message = await database_sync_to_async(self._create_message)(
            user, text, requested_visibility
        )

        payload = self._serialize_message(message)
        if requested_visibility == "teacher_only":
            await self.send_json({"type": "chat.message", "data": payload})
        else:
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "class.event",
                    "event": {"type": "chat.message", "data": payload},
                },
            )  
                    


        # Broadcast
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "class.event",
                "event": {
                    "type": "chat.message",
                    "data": payload,
                },
            },
        )

    @staticmethod
    def _serialize_message(message: ClassMessage) -> dict:
        return {
            "id": message.id,
            "class_id": message.live_class_id,
            "user_id": message.user_id,
            "name": message.name,
            "role": message.role,
            "text": message.text,
            "message_type": message.message_type,
            "visibility": message.visibility,
            "created_at": message.created_at.isoformat().replace("+00:00", "Z"),
            "deleted_at": None,
            "deleted_by_user_id": None,
        }

    def _create_message(self, user, text: str, visibility: str) -> ClassMessage:
        return ClassMessage.objects.create(
            live_class=self._cls,
            user_id=user.user_id,
            name=user.name or f"User {user.user_id}",
            role=user.role,
            text=text,
            message_type=MessageType.TEXT,
            visibility=visibility,
        )