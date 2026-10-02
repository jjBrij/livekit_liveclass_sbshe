
import logging
import time
from .redis_state import get_client
logger = logging.getLogger(__name__)

def check_rate_limit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    client = get_client()
    now = int(time.time())
    window_start = now - (now % window_seconds)
    redis_key = f"{key}:{window_start}"

    pipe = client.pipeline()
    pipe.incr(redis_key, 1)
    pipe.expire(redis_key, window_seconds)
    count, _ = pipe.execute()

    if count > limit:
        retry_after = window_seconds - (now % window_seconds)
        return False, max(1, retry_after)
    return True, 0