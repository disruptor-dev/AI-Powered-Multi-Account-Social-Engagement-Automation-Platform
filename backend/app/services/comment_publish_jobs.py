"""Legacy queue helpers retained for status compatibility; new public writes use the human-approval queue."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.redis_client import get_redis_client
from app.models.comment_publish_batch import CommentPublishBatch, CommentPublishItem
from app.services.comment_jobs import QUEUE_NAME

logger = logging.getLogger(__name__)


class PublishQueueUnavailable(Exception):
    """The publish batch could not be delivered to the background queue."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _queue_payload(batch: CommentPublishBatch) -> dict[str, str]:
    return {"type": "comment_publish_batch", "batch_id": str(batch.id), "workspace_id": str(batch.workspace_id)}


def queue_comment_publish_batch(db: Session, batch: CommentPublishBatch, redis_client=None) -> None:
    """Persist enqueue time before Redis delivery so fast workers always see committed rows."""
    client = redis_client or get_redis_client()
    batch.last_enqueued_at = utcnow()
    db.commit()
    try:
        client.lpush(QUEUE_NAME, json.dumps(_queue_payload(batch), separators=(",", ":")))
    except (RedisError, OSError, ValueError):
        batch.status = "error"
        batch.error_code = "publish_queue_unavailable"
        batch.finished_at = utcnow()
        for item in db.scalars(select(CommentPublishItem).where(CommentPublishItem.batch_id == batch.id, CommentPublishItem.status == "queued")).all():
            item.status = "failed"
            item.error_code = "publish_queue_unavailable"
            item.finished_at = batch.finished_at
        db.commit()
        logger.warning("Could not enqueue comment publish batch id=%s", batch.id)
        raise PublishQueueUnavailable from None


def _finish_batch_from_items(db: Session, batch: CommentPublishBatch, now: datetime) -> None:
    items = db.scalars(select(CommentPublishItem).where(CommentPublishItem.batch_id == batch.id)).all()
    batch.published_count = sum(item.status == "published" for item in items)
    batch.failed_count = sum(item.status == "failed" for item in items)
    batch.unknown_count = sum(item.status == "unknown" for item in items)
    pending = sum(item.status in {"queued", "publishing"} for item in items)
    if pending:
        return
    if batch.published_count == batch.total_count:
        batch.status = "success"
        batch.error_code = None
    elif batch.published_count > 0 or batch.unknown_count > 0:
        batch.status = "partial"
        batch.error_code = batch.error_code or "publish_partial_failure"
    else:
        batch.status = "error"
        if not batch.error_code:
            batch.error_code = "publish_all_failed"
    batch.finished_at = now


def enqueue_due_comment_publish_batches(*, redis_client=None, now: datetime | None = None, limit: int = 50) -> int:
    """Retire pre-approval multi-channel batches; never enqueue legacy public writes."""
    now = now or utcnow()
    db = SessionLocal()
    try:
        legacy_batches = db.scalars(select(CommentPublishBatch).where(
            CommentPublishBatch.status.in_(["queued", "running"]),
        ).order_by(CommentPublishBatch.created_at.asc()).limit(limit)).all()
        retired = 0
        for batch in legacy_batches:
            items = db.scalars(select(CommentPublishItem).where(CommentPublishItem.batch_id == batch.id)).all()
            for item in items:
                if item.status == "queued":
                    item.status = "failed"
                    item.error_code = "legacy_multi_channel_publishing_disabled"
                    item.finished_at = now
                elif item.status == "publishing":
                    item.status = "unknown"
                    item.error_code = "provider_outcome_unknown"
                    item.finished_at = now
            batch.published_count = sum(item.status == "published" for item in items)
            batch.failed_count = sum(item.status == "failed" for item in items)
            batch.unknown_count = sum(item.status == "unknown" for item in items)
            batch.status = "partial" if batch.published_count or batch.unknown_count else "error"
            batch.error_code = "legacy_multi_channel_publishing_disabled"
            batch.finished_at = now
            retired += 1
        if legacy_batches:
            db.commit()
        return retired
    finally:
        db.close()
