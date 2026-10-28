"""Public, signed webhook endpoints for supported social platforms.

Meta's GET handshake deliberately does not use owner-session/CSRF protection: Meta
needs to reach it server-to-server. POST notifications are authenticated by their
X-Hub-Signature-256 HMAC using the Meta App Secret. Signed comment events enqueue a background sync; comment content is fetched from the provider API.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.social_account import SocialAccount
from app.services.comment_jobs import SyncQueueUnavailable, queue_sync_run
from sqlalchemy import select

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


@router.get(
    "/instagram",
    summary="Verify the Instagram webhook callback URL",
    response_class=PlainTextResponse,
)
def verify_instagram_webhook(
    mode: str | None = Query(default=None, alias="hub.mode"),
    verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    """Match Meta's webhook handshake and return the raw challenge as plain text."""
    settings = get_settings()
    expected_token = settings.meta_webhook_verify_token.strip()
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Meta webhook verify token is not configured",
        )
    if (
        mode != "subscribe"
        or not verify_token
        or not hmac.compare_digest(verify_token, expected_token)
        or challenge is None
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Webhook verification failed")
    return PlainTextResponse(content=challenge, status_code=status.HTTP_200_OK)


@router.post("/instagram", summary="Receive a signed Instagram webhook notification")
async def receive_instagram_webhook(request: Request) -> Response:
    """Validate Meta's signature and acknowledge the payload without exposing it.

    Valid comment events only trigger an idempotent background sync. Provider content
    is fetched by the worker rather than trusted directly from the webhook payload.
    """
    settings = get_settings()
    app_secret = settings.instagram_app_secret.strip()
    if not app_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Instagram App Secret is not configured",
        )

    raw_body = await request.body()
    received_signature = request.headers.get("X-Hub-Signature-256", "").strip()
    expected_signature = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    if not received_signature or not hmac.compare_digest(received_signature, expected_signature):
        logger.warning("Rejected Instagram webhook with invalid/missing signature")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON webhook payload") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook payload must be a JSON object")

    entries = payload.get("entry")
    entry_count = len(entries) if isinstance(entries, list) else 0
    relevant_ids: set[str] = set()
    if payload.get("object") == "instagram" and isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            changes = entry.get("changes")
            if not isinstance(changes, list) or not any(
                isinstance(change, dict) and change.get("field") == "comments" for change in changes
            ):
                continue
            account_id = entry.get("id")
            if isinstance(account_id, (str, int)) and str(account_id):
                relevant_ids.add(str(account_id))

    queued = 0
    if relevant_ids:
        db = SessionLocal()
        try:
            accounts = db.scalars(select(SocialAccount).where(
                SocialAccount.platform == "instagram",
                SocialAccount.platform_account_id.in_(relevant_ids),
                SocialAccount.status == "connected",
            )).all()
            for account in accounts:
                try:
                    _, created = queue_sync_run(db, account, trigger="webhook")
                    queued += int(created)
                except SyncQueueUnavailable:
                    logger.warning("Signed Instagram webhook could not enqueue account sync")
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Comment sync queue is temporarily unavailable",
                    ) from None
        finally:
            db.close()

    logger.info("Accepted signed Instagram webhook: entries=%d sync_jobs_queued=%d", entry_count, queued)
    return Response(status_code=status.HTTP_200_OK)
