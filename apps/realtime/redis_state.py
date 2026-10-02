
import json
import logging
from typing import Any
import redis
from django.conf import settings
logger = logging.getLogger(__name__)
_client: redis.Redis | None = None


def get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client

CLASS_STATE_TTL = 24 * 60 * 60


def _key_raised(class_id: int) -> str:
    return f"raised_hands:{class_id}"


def _key_accepted(class_id: int) -> str:
    return f"accepted_hands:{class_id}"


def _key_rejected(class_id: int) -> str:
    return f"rejected_hands:{class_id}"

def add_raised_hand(class_id: int, payload: dict[str, Any]) -> None:
    client = get_client()
    key = _key_raised(class_id)
    client.hset(key, str(payload["user_id"]), json.dumps(payload))
    client.expire(key, CLASS_STATE_TTL)


def remove_raised_hand(class_id: int, user_id: int) -> None:
    client = get_client()
    client.hdel(_key_raised(class_id), str(user_id))


def get_raised_hand(class_id: int, user_id: int) -> dict | None:
    client = get_client()
    raw = client.hget(_key_raised(class_id), str(user_id))
    return json.loads(raw) if raw else None


def list_raised_hands(class_id: int) -> list[dict]:
    client = get_client()
    raw = client.hgetall(_key_raised(class_id))
    return [json.loads(v) for v in raw.values()]

def add_accepted_hand(class_id: int, user_id: int) -> None:
    client = get_client()
    key = _key_accepted(class_id)
    client.sadd(key, str(user_id))
    client.expire(key, CLASS_STATE_TTL)


def is_hand_accepted(class_id: int, user_id: int) -> bool:
    client = get_client()
    return bool(client.sismember(_key_accepted(class_id), str(user_id)))


def list_accepted_hands(class_id: int) -> list[int]:
    client = get_client()
    return [int(x) for x in client.smembers(_key_accepted(class_id))]


def remove_accepted_hand(class_id: int, user_id: int) -> None:
    client = get_client()
    client.srem(_key_accepted(class_id), str(user_id))

def add_rejected_hand(class_id: int, payload: dict[str, Any]) -> None:
    client = get_client()
    key = _key_rejected(class_id)
    client.hset(key, str(payload["user_id"]), json.dumps(payload))
    client.expire(key, CLASS_STATE_TTL)


def get_rejected_hand(class_id: int, user_id: int) -> dict | None:
    client = get_client()
    raw = client.hget(_key_rejected(class_id), str(user_id))
    return json.loads(raw) if raw else None

def clear_class_state(class_id: int) -> None:
    client = get_client()
    client.delete(_key_raised(class_id), _key_accepted(class_id), _key_rejected(class_id))