"""Human-approved, single-channel/single-video YouTube comment workflow.

Only user-authored comment text enters this queue. Publication only occurs after a human
reviews the exact text and supplies an explicit, timezone-aware scheduled_at value. The
worker uses fixed database polling and does not add randomized delays or retry uncertain writes.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.comment import SocialComment
from app.models.media_item import MediaItem
from app.models.social_account import SocialAccount
from app.models.youtube_comment_queue import YouTubeCommentQueueItem, YouTubeCommentQueueState
from app.services.comment_sync import (
    ProviderSyncError,
    YOUTUBE_FORCE_SSL_SCOPE,
    parse_provider_datetime,
    publish_youtube_top_level_comment,
)

logger = logging.getLogger(__name__)


class YouTubeCommentQueueUnavailable(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_queue_state(db: Session) -> YouTubeCommentQueueState:
    state = db.get(YouTubeCommentQueueState, 1)
    if state is None:
        state = YouTubeCommentQueueState(id=1, paused_at=None, pause_reason=None)
        db.add(state)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            state = db.get(YouTubeCommentQueueState, 1)
            if state is None:
                raise
    return state


def queue_item_response(db: Session, item: YouTubeCommentQueueItem) -> dict[str, Any]:
    account = db.get(SocialAccount, item.social_account_id)
    return {
        "id": str(item.id),
        "account_id": str(item.social_account_id),
        "account_name": account.display_name if account else "Disconnected channel",
        "account_status": account.status if account else "disconnected",
        "status": item.status,
        "video_id": item.target_video_id,
        "video_url": item.target_video_url,
        "video_channel_id": item.target_video_channel_id,
        "video_title": item.target_video_title,
        "video_description": item.target_video_description,
        "thumbnail_url": item.target_video_thumbnail_url,
        "tone": item.tone,
        "language": item.language,
        "custom_instructions": item.custom_instructions,
        "draft_text": item.draft_text,
        "draft_source": item.draft_source,
        "human_authored": item.draft_source == "human",
        "approved_text": item.approved_text,
        "quality_warnings": item.quality_warnings or [],
        "error_code": item.error_code,
        "provider_comment_id": item.provider_comment_id,
        "scheduled_at": item.scheduled_at.isoformat() if item.scheduled_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "generated_at": item.generated_at.isoformat() if item.generated_at else None,
        "approved_at": item.approved_at.isoformat() if item.approved_at else None,
        "approved_by_email": item.approved_by_email,
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
        "attempt_count": item.attempt_count,
    }


def queue_video_comment_generation(db: Session, item: YouTubeCommentQueueItem, redis_client=None) -> None:
    """Retired compatibility shim: this queue accepts human text only."""
    raise YouTubeCommentQueueUnavailable("AI generation is disabled for the Human YouTube Comment Queue")


def enqueue_due_youtube_comment_generations(*, redis_client=None, now: datetime | None = None, limit: int = 25) -> int:
    """Retire legacy AI-generation records; never enqueue a generation job."""
    now = now or utcnow()
    db = SessionLocal()
    try:
        legacy_items = db.scalars(select(YouTubeCommentQueueItem).where(
            YouTubeCommentQueueItem.status.in_(["queued_generation", "generating"]),
        ).limit(limit)).all()
        for item in legacy_items:
            item.status = "cancelled"
            item.error_code = "human_comment_required"
            item.cancelled_at = now
            item.finished_at = now
            item.last_enqueued_at = None
            item.updated_at = now
        if legacy_items:
            db.commit()
        return 0
    finally:
        db.close()


def process_youtube_comment_generation(db: Session, queue_item_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """Drain stale Redis jobs without generating AI text or making provider writes."""
    now = utcnow()
    item = db.scalar(select(YouTubeCommentQueueItem).where(
        YouTubeCommentQueueItem.id == queue_item_id,
        YouTubeCommentQueueItem.workspace_id == workspace_id,
        YouTubeCommentQueueItem.status.in_(["queued_generation", "generating"]),
    ))
    if item is None:
        return
    item.status = "cancelled"
    item.error_code = "human_comment_required"
    item.cancelled_at = now
    item.finished_at = now
    item.last_enqueued_at = None
    item.updated_at = now
    db.commit()
    logger.info("Retired legacy AI-generated YouTube queue item: id=%s", queue_item_id)


def _persist_published_comment(db: Session, item: YouTubeCommentQueueItem, account: SocialAccount, provider_response: dict[str, Any]) -> str:
    snippet = provider_response.get("snippet") if isinstance(provider_response.get("snippet"), dict) else {}
    top_comment = snippet.get("topLevelComment") if isinstance(snippet.get("topLevelComment"), dict) else {}
    comment_snippet = top_comment.get("snippet") if isinstance(top_comment.get("snippet"), dict) else {}
    provider_comment_id = str(top_comment.get("id") or "")
    if not provider_comment_id:
        raise ProviderSyncError("provider_outcome_unknown", outcome_unknown=True)

    media = db.scalar(select(MediaItem).where(
        MediaItem.workspace_id == item.workspace_id,
        MediaItem.social_account_id == account.id,
        MediaItem.platform == "youtube",
        MediaItem.platform_media_id == item.target_video_id,
    ))
    if media is None:
        media = MediaItem(
            workspace_id=item.workspace_id,
            social_account_id=account.id,
            platform="youtube",
            platform_media_id=item.target_video_id,
            media_type="video",
            source_type="external_video",
            title=item.target_video_title[:1000],
            description=item.target_video_description[:50000],
            permalink=item.target_video_url,
            thumbnail_url=item.target_video_thumbnail_url,
            raw_metadata={"external_video": True, "comment_queue_item_id": str(item.id)},
        )
        db.add(media)
        db.flush()
    elif media.source_type != "account_video":
        media.source_type = "external_video"

    existing = db.scalar(select(SocialComment).where(
        SocialComment.workspace_id == item.workspace_id,
        SocialComment.social_account_id == account.id,
        SocialComment.platform == "youtube",
        SocialComment.platform_comment_id == provider_comment_id,
    ))
    if existing is None:
        author_channel = comment_snippet.get("authorChannelId")
        author_channel = author_channel if isinstance(author_channel, dict) else {}
        published_at = parse_provider_datetime(comment_snippet.get("publishedAt"))
        existing = SocialComment(
            workspace_id=item.workspace_id,
            social_account_id=account.id,
            media_item_id=media.id,
            platform="youtube",
            platform_comment_id=provider_comment_id,
            author_id=str(author_channel.get("value") or account.platform_account_id),
            author_name=str(comment_snippet.get("authorDisplayName") or account.display_name)[:300],
            author_username=account.username,
            author_avatar_url=comment_snippet.get("authorProfileImageUrl"),
            text=str(comment_snippet.get("textOriginal") or comment_snippet.get("textDisplay") or item.approved_text or "")[:20000],
            like_count=0,
            reply_count=0,
            published_at=published_at,
            platform_updated_at=published_at,
            is_reply=False,
            is_owner_comment=True,
            has_owner_reply=False,
            workflow_status="open",
            raw_metadata={"published_via_human_approval_queue": str(item.id)},
        )
        db.add(existing)
    return provider_comment_id


def _mark_queue_paused_on_quota(db: Session, now: datetime) -> None:
    state = get_queue_state(db)
    state.paused_at = now
    state.pause_reason = "provider_quota_exceeded"
    state.updated_at = now
    db.commit()


def process_due_scheduled_youtube_comments(db: Session, *, now: datetime | None = None, limit: int = 1) -> int:
    """Poll due approved comments and process at most a small fixed number per worker tick."""
    now = now or utcnow()
    state = get_queue_state(db)
    if state.paused_at is not None:
        return 0

    # A worker crash after changing status to publishing cannot safely be retried:
    # the write may already have reached YouTube.
    stale = db.scalars(select(YouTubeCommentQueueItem).where(
        YouTubeCommentQueueItem.status == "publishing",
        YouTubeCommentQueueItem.started_at < now - timedelta(minutes=15),
    ).limit(limit * 5)).all()
    for old in stale:
        old.status = "outcome_unknown"
        old.error_code = "provider_outcome_unknown"
        old.finished_at = now
    if stale:
        db.commit()

    candidates = db.scalars(select(YouTubeCommentQueueItem).where(
        YouTubeCommentQueueItem.status == "scheduled",
        YouTubeCommentQueueItem.scheduled_at <= now,
    ).order_by(YouTubeCommentQueueItem.scheduled_at.asc(), YouTubeCommentQueueItem.created_at.asc()).limit(limit)).all()
    processed = 0
    for candidate in candidates:
        # Conditional UPDATE is the atomic claim and works on PostgreSQL and SQLite.
        claim = db.execute(update(YouTubeCommentQueueItem).where(
            YouTubeCommentQueueItem.id == candidate.id,
            YouTubeCommentQueueItem.status == "scheduled",
            YouTubeCommentQueueItem.scheduled_at <= now,
        ).values(status="publishing", started_at=now, attempt_count=YouTubeCommentQueueItem.attempt_count + 1, error_code=None, updated_at=now).execution_options(synchronize_session=False))
        db.commit()
        if claim.rowcount != 1:
            continue
        item = db.get(YouTubeCommentQueueItem, candidate.id)
        if item is None:
            continue
        if item.draft_source != "human":
            # Defense in depth: even a legacy AI row or direct database insert must
            # never reach the public YouTube write API through this queue.
            item.status = "failed"
            item.error_code = "human_comment_required"
            item.finished_at = utcnow()
            item.updated_at = item.finished_at
            db.commit()
            processed += 1
            continue
        account = db.scalar(select(SocialAccount).where(
            SocialAccount.id == item.social_account_id,
            SocialAccount.workspace_id == item.workspace_id,
        ))
        if account is None or account.platform != "youtube" or account.status != "connected" or YOUTUBE_FORCE_SSL_SCOPE not in (account.scopes or []):
            item.status = "failed"
            item.error_code = "account_not_publishable"
            item.finished_at = utcnow()
            item.updated_at = utcnow()
            db.commit()
            processed += 1
            continue
        if not (item.approved_text or "").strip() or not item.target_video_channel_id:
            item.status = "failed"
            item.error_code = "approved_payload_missing"
            item.finished_at = utcnow()
            item.updated_at = utcnow()
            db.commit()
            processed += 1
            continue
        try:
            provider_response = asyncio.run(publish_youtube_top_level_comment(
                account,
                video_id=item.target_video_id,
                video_channel_id=item.target_video_channel_id,
                text=item.approved_text,
                settings=get_settings(),
                db=db,
            ))
            provider_comment_id = _persist_published_comment(db, item, account, provider_response)
            item.provider_comment_id = provider_comment_id
            item.status = "posted"
            item.error_code = None
            item.published_at = utcnow()
            item.finished_at = item.published_at
            item.updated_at = item.published_at
            account.last_error_code = None
            db.commit()
        except ProviderSyncError as exc:
            db.rollback()
            current = db.get(YouTubeCommentQueueItem, candidate.id)
            current_account = db.get(SocialAccount, item.social_account_id)
            if current is not None:
                current.status = "blocked_quota" if exc.code == "provider_quota_exceeded" else "outcome_unknown" if exc.outcome_unknown else "failed"
                current.error_code = exc.code[:100]
                current.finished_at = utcnow()
                current.updated_at = utcnow()
            if current_account is not None and exc.reauthorization_required:
                current_account.status = "reauthorization_required"
                current_account.last_error_code = exc.code[:80]
            db.commit()
            if exc.code == "provider_quota_exceeded":
                _mark_queue_paused_on_quota(db, utcnow())
            logger.warning("Scheduled YouTube comment failed: id=%s code=%s status=%s", candidate.id, exc.code, current.status if current else "missing")
        except Exception:
            # Any unexpected failure after beginning the provider write is treated as
            # unknown; do not retry and potentially create a duplicate public comment.
            db.rollback()
            current = db.get(YouTubeCommentQueueItem, candidate.id)
            if current is not None:
                current.status = "outcome_unknown"
                current.error_code = "provider_outcome_unknown"
                current.finished_at = utcnow()
                current.updated_at = utcnow()
                db.commit()
            logger.exception("Unexpected error while publishing queued YouTube comment id=%s", candidate.id)
        processed += 1
        state = get_queue_state(db)
        if state.paused_at is not None:
            break
    return processed
