"""Redis queue publishing and database-backed sync scheduling helpers."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.redis_client import get_redis_client
from app.models.comment_sync_run import CommentSyncRun
from app.models.social_account import SocialAccount

logger = logging.getLogger(__name__)
QUEUE_NAME = "social-engagement:jobs"
DEAD_LETTER_QUEUE = "social-engagement:dead-letter"
SyncTrigger = Literal["manual", "scheduled", "webhook"]


class SyncQueueUnavailable(Exception):
    """Redis could not accept a sync job. Safe to expose as an API 503."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def queue_sync_run(
    db: Session,
    account: SocialAccount,
    *,
    trigger: SyncTrigger = "manual",
    redis_client=None,
    target_platform_media_id: str | None = None,
) -> tuple[CommentSyncRun, bool]:
    """Create and enqueue a run. Existing active runs are reused to prevent duplicate work."""
    active = db.scalar(
        select(CommentSyncRun)
        .where(
            CommentSyncRun.social_account_id == account.id,
            CommentSyncRun.status.in_(["queued", "running"]),
        )
        .order_by(CommentSyncRun.queued_at.desc())
        .limit(1)
        .with_for_update()
    )
    if active is not None:
        # Reuse only the exact same work. Callers can return a clear conflict for
        # a different video while a channel-wide or another targeted sync is active.
        return active, False

    run_id = uuid.uuid4()
    run = CommentSyncRun(
        id=run_id,
        workspace_id=account.workspace_id,
        social_account_id=account.id,
        job_key=str(run_id),
        trigger=trigger,
        status="queued",
        target_platform_media_id=target_platform_media_id,
    )
    db.add(run)
    account.last_sync_status = "queued"
    account.last_sync_error_code = None
    try:
        db.commit()
    except IntegrityError:
        # The partial unique index closes the race where two requests both observed
        # no active run before either transaction inserted one.
        db.rollback()
        active = db.scalar(
            select(CommentSyncRun)
            .where(
                CommentSyncRun.social_account_id == account.id,
                CommentSyncRun.status.in_(["queued", "running"]),
            )
            .order_by(CommentSyncRun.queued_at.desc())
            .limit(1)
        )
        if active is not None:
            return active, False
        raise
    db.refresh(run)

    payload = {
        "type": "comment_sync",
        "job_id": str(run.id),
        "run_id": str(run.id),
        "account_id": str(account.id),
        "workspace_id": str(account.workspace_id),
        "trigger": trigger,
        "attempt": 0,
    }
    if target_platform_media_id:
        payload["target_platform_media_id"] = target_platform_media_id
    try:
        (redis_client or get_redis_client()).lpush(QUEUE_NAME, json.dumps(payload, separators=(",", ":")))
    except (RedisError, OSError, ValueError):
        run.status = "error"
        run.error_code = "sync_queue_unavailable"
        run.finished_at = utcnow()
        account.last_sync_status = "error"
        account.last_sync_error_code = "sync_queue_unavailable"
        account.last_sync_completed_at = run.finished_at
        db.commit()
        logger.warning("Could not enqueue comment sync: account_id=%s", account.id)
        raise SyncQueueUnavailable from None
    return run, True


def expire_stale_runs(db: Session, *, now: datetime | None = None) -> int:
    """Mark orphaned queue/running records as timed out after a worker crash."""
    settings = get_settings()
    now = now or utcnow()
    stale_after = max(1800, max(60, settings.comment_sync_interval_seconds) * 3)
    cutoff = now - timedelta(seconds=stale_after)
    stale = db.scalars(
        select(CommentSyncRun).where(
            CommentSyncRun.status.in_(["queued", "running"]),
            CommentSyncRun.queued_at < cutoff,
        )
    ).all()
    for run in stale:
        run.status = "error"
        run.error_code = "sync_worker_timeout"
        run.finished_at = now
        account = db.get(SocialAccount, run.social_account_id)
        if account is not None:
            account.last_sync_status = "error"
            account.last_sync_error_code = "sync_worker_timeout"
            account.last_sync_completed_at = now
    if stale:
        db.commit()
    return len(stale)


def enqueue_due_account_syncs() -> int:
    """Periodically queue connected accounts that have never synced or are due again."""
    settings = get_settings()
    now = utcnow()
    interval = max(60, settings.comment_sync_interval_seconds)
    queued_count = 0
    db = SessionLocal()
    try:
        expire_stale_runs(db, now=now)
        accounts = db.scalars(select(SocialAccount).where(SocialAccount.status == "connected")).all()
        for account in accounts:
            last = account.last_sync_completed_at
            if last is not None:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (now - last).total_seconds() < interval:
                    continue
            try:
                _, queued = queue_sync_run(db, account, trigger="scheduled")
                queued_count += int(queued)
            except SyncQueueUnavailable:
                # One Redis outage should not terminate the worker's scheduler loop.
                continue
        return queued_count
    finally:
        db.close()


def enqueue_dead_letter(client, payload: object, reason: str) -> None:
    # Queue payloads are untrusted input here. Never copy arbitrary fields (which could
    # contain credentials or user content) into a durable dead-letter record.
    safe_fields = ("type", "job_id", "run_id", "account_id", "workspace_id", "attempt", "draft_id", "rule_id")
    safe_payload: dict[str, str | int | float | bool] = {}
    if isinstance(payload, dict):
        for field in safe_fields:
            value = payload.get(field)
            if isinstance(value, (str, int, float, bool)):
                safe_payload[field] = value
    else:
        safe_payload["payload_type"] = type(payload).__name__
    try:
        client.lpush(DEAD_LETTER_QUEUE, json.dumps({"reason": reason, "payload": safe_payload}, separators=(",", ":"))[:10000])
    except Exception:
        logger.warning("Could not write invalid job to dead-letter queue")
