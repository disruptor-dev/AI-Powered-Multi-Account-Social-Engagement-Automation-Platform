"""Legacy batch status/read helpers; legacy multi-channel publishing is disabled."""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.comment_publish_batch import CommentPublishBatch, CommentPublishItem
from app.models.social_account import SocialAccount


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def text_fingerprint(value: str) -> str:
    return hashlib.sha256(normalized_text(value).encode("utf-8")).hexdigest()


def request_fingerprint(video_id: str, comments: list[dict[str, str]]) -> str:
    import json
    canonical = {
        "video_id": video_id,
        "comments": [{"account_id": item["account_id"], "text": item["text"].strip()} for item in comments],
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _retire_legacy_batch(db: Session, batch: CommentPublishBatch, now: datetime) -> None:
    """Close old multi-channel batches without making any more YouTube writes."""
    items = db.scalars(select(CommentPublishItem).where(CommentPublishItem.batch_id == batch.id)).all()
    for item in items:
        if item.status == "queued":
            item.status = "failed"
            item.error_code = "legacy_multi_channel_publishing_disabled"
            item.finished_at = now
        elif item.status == "publishing":
            # A previous worker may have sent the write before this rollout. Never retry it.
            item.status = "unknown"
            item.error_code = "provider_outcome_unknown"
            item.finished_at = now
    batch.published_count = sum(item.status == "published" for item in items)
    batch.failed_count = sum(item.status == "failed" for item in items)
    batch.unknown_count = sum(item.status == "unknown" for item in items)
    if any(item.status in {"queued", "publishing"} for item in items):
        # Fail closed if a future status is added without being handled above.
        for item in items:
            if item.status in {"queued", "publishing"}:
                item.status = "unknown" if item.status == "publishing" else "failed"
                item.error_code = "legacy_multi_channel_publishing_disabled" if item.status == "failed" else "provider_outcome_unknown"
                item.finished_at = now
        batch.failed_count = sum(item.status == "failed" for item in items)
        batch.unknown_count = sum(item.status == "unknown" for item in items)
    batch.status = "partial" if batch.published_count or batch.unknown_count else "error"
    batch.error_code = "legacy_multi_channel_publishing_disabled"
    batch.finished_at = now
    db.commit()


def process_comment_publish_batch(db: Session, batch_id: uuid.UUID, workspace_id: uuid.UUID | None = None) -> None:
    """Retire any legacy multi-channel batch. This function is permanently write-disabled."""
    batch = db.get(CommentPublishBatch, batch_id)
    if batch is None or (workspace_id is not None and batch.workspace_id != workspace_id):
        return
    items = db.scalars(select(CommentPublishItem).where(CommentPublishItem.batch_id == batch.id)).all()
    has_unresolved_legacy_work = any(item.status in {"queued", "publishing"} for item in items)
    if batch.status in {"queued", "running"} or has_unresolved_legacy_work:
        _retire_legacy_batch(db, batch, utcnow())


def batch_response(db: Session, batch: CommentPublishBatch) -> dict[str, Any]:
    items = db.scalars(select(CommentPublishItem).where(
        CommentPublishItem.batch_id == batch.id,
    ).order_by(CommentPublishItem.position.asc())).all()
    result_items = []
    for item in items:
        account = db.get(SocialAccount, item.social_account_id)
        result_items.append({
            "id": str(item.id),
            "account_id": str(item.social_account_id),
            "account_name": account.display_name if account else "Disconnected channel",
            "text": item.text,
            "status": item.status,
            "error_code": item.error_code,
            "provider_comment_id": item.provider_comment_id,
            "published_at": item.published_at.isoformat() if item.published_at else None,
        })
    return {
        "id": str(batch.id),
        "status": batch.status,
        "video_id": batch.target_video_id,
        "video_url": batch.target_video_url,
        "video_title": batch.target_video_title,
        "video_thumbnail_url": batch.target_video_thumbnail_url,
        "total_count": batch.total_count,
        "published_count": batch.published_count,
        "failed_count": batch.failed_count,
        "unknown_count": batch.unknown_count,
        "error_code": batch.error_code,
        "estimated_quota_units": 1 + 50 * batch.total_count,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "started_at": batch.started_at.isoformat() if batch.started_at else None,
        "finished_at": batch.finished_at.isoformat() if batch.finished_at else None,
        "items": result_items,
    }
