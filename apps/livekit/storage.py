
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Optional

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

logger = logging.getLogger(__name__)


class S3ConfigurationError(Exception):
    """Raised when S3 settings are missing or obviously invalid."""


class S3ServiceError(Exception):
    """Raised when S3 operations fail (network, permissions, bad key)."""


# One client per process. boto3 clients are thread-safe; we still keep it
# simple by lazily constructing it once.
_client = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _require_config() -> None:
    missing = [
        name for name in ("S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY")
        if not getattr(settings, name, "")
    ]
    # Region is required for AWS but not for MinIO/R2 in path-style mode.
    if not settings.S3_REGION and not settings.S3_ENDPOINT_URL:
        missing.append("S3_REGION")
    if missing:
        raise S3ConfigurationError(
            f"Missing S3 config: {', '.join(missing)}"
        )


def _build_client():
    _require_config()

    session_kwargs = {
        "aws_access_key_id": settings.S3_ACCESS_KEY,
        "aws_secret_access_key": settings.S3_SECRET_KEY,
    }
    if settings.S3_REGION:
        session_kwargs["region_name"] = settings.S3_REGION

    config = BotoConfig(
        signature_version="s3v4",
        s3={"addressing_style": "path" if settings.S3_ENDPOINT_URL else "auto"},
        retries={"max_attempts": 3, "mode": "standard"},
    )

    client_kwargs = {**session_kwargs, "config": config}
    if settings.S3_ENDPOINT_URL:
        client_kwargs["endpoint_url"] = settings.S3_ENDPOINT_URL

    return boto3.client("s3", **client_kwargs)


def get_client():
    global _client
    if _client is None:
        _client = _build_client()
    return _client


# ---------------------------------------------------------------------------
# Presigned URL
# ---------------------------------------------------------------------------
def generate_presigned_get_url(
    *,
    key: str,
    ttl_seconds: int,
    bucket: Optional[str] = None,
) -> tuple[str, datetime]:
    """
    Return (url, expires_at_utc).

    boto3's generate_presigned_url is a pure computation — no network call.
    """
    if not key:
        raise S3ServiceError("Cannot generate a URL for an empty key.")

    bucket = bucket or settings.S3_BUCKET
    if not bucket:
        raise S3ConfigurationError("S3_BUCKET is not set.")

    ttl = max(60, min(int(ttl_seconds), 86400))

    try:
        client = get_client()
        url = client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=ttl,
            HttpMethod="GET",
        )
    except (BotoCoreError, ClientError) as exc:
        logger.exception("Presign failed for key=%s bucket=%s", key, bucket)
        raise S3ServiceError(f"Presign failed: {exc}") from exc
    except S3ConfigurationError:
        raise
    except Exception as exc:
        logger.exception("Unexpected presign error for key=%s", key)
        raise S3ServiceError(f"Presign failed: {exc}") from exc

    expires_at = datetime.now(dt_timezone.utc) + timedelta(seconds=ttl)
    return url, expires_at


# ---------------------------------------------------------------------------
# Connectivity check
# ---------------------------------------------------------------------------
def head_bucket() -> dict:
    """
    Verify the configured bucket is reachable with current credentials.

    Returns a small dict. Raises S3ConfigurationError or S3ServiceError.
    """
    _require_config()
    client = get_client()
    bucket = settings.S3_BUCKET

    try:
        client.head_bucket(Bucket=bucket)
        return {"head_bucket_ok": True}
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        # 404 (NoSuchBucket) or 403 (AccessDenied) are both meaningful.
        logger.warning("S3 head_bucket failed for %s: %s", bucket, code)
        raise S3ServiceError(f"head_bucket failed: {code or exc}")
    except BotoCoreError as exc:
        logger.exception("S3 head_bucket network error for %s", bucket)
        raise S3ServiceError(f"head_bucket failed: {exc}") from exc