"""Redis-backed fixed-window rate limiting for sensitive API operations.

Keys are hashed to avoid retaining raw IP/email identifiers in Redis. Only the
socket peer address is used by default. Forwarded headers are honored only when
explicitly enabled and the deployment proxy is configured to overwrite them.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Callable

from fastapi import HTTPException, Request, status

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_INCREMENT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
local ttl = redis.call('TTL', KEYS[1])
return {current, ttl}
"""


def client_address(request: Request) -> str:
    settings = get_settings()
    peer = request.client.host if request.client else "unknown"
    if settings.rate_limit_trust_proxy_headers:
        # Must only be enabled when a trusted reverse proxy overwrites X-Forwarded-For.
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            candidate = forwarded.split(",", 1)[0].strip()
            if candidate and len(candidate) <= 64:
                return candidate
    return peer or "unknown"


def _identifier_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:40]


def enforce_rate_limit(request: Request, *, bucket: str, limit: int, window_seconds: int) -> None:
    settings = get_settings()
    if not settings.rate_limit_enabled or settings.app_env == "test":
        return
    if limit < 1 or window_seconds < 1:
        raise RuntimeError("Rate-limit limits and windows must be positive")

    key = f"socialdesk:rl:v1:{bucket}:{_identifier_hash(client_address(request))}"
    try:
        # Lazy import avoids making application startup depend on importing Redis in unit-only environments.
        from app.core.redis_client import get_redis_client
        result = get_redis_client().eval(_INCREMENT_SCRIPT, 1, key, window_seconds)
        count, ttl = int(result[0]), max(1, int(result[1]))
    except Exception as exc:
        # Never leak the configured Redis URL or exception text to clients/logs.
        if settings.effective_rate_limit_fail_closed:
            logger.error("rate_limit_backend_unavailable bucket=%s error_type=%s", bucket, type(exc).__name__)
            raise HTTPException(status_code=503, detail="Security rate-limit service is temporarily unavailable") from None
        logger.warning("rate_limit_fail_open bucket=%s error_type=%s", bucket, type(exc).__name__)
        return

    if count > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please wait before trying again.",
            headers={"Retry-After": str(min(ttl, window_seconds)), "Cache-Control": "no-store"},
        )


def rate_limit(bucket: str, *, limit_attr: str, window_attr: str) -> Callable:
    """FastAPI dependency factory reading configured per-operation limits."""
    def dependency(request: Request) -> None:
        settings = get_settings()
        enforce_rate_limit(
            request,
            bucket=bucket,
            limit=int(getattr(settings, limit_attr)),
            window_seconds=int(getattr(settings, window_attr)),
        )
    dependency.__name__ = f"rate_limit_{bucket.replace('-', '_')}"
    return dependency
