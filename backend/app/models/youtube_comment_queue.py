from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text,
    UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class YouTubeCommentQueueItem(Base):
    """A single top-level YouTube comment, bound to one channel and one video.

    Nothing is sent to YouTube until a human approves the final text and an explicit
    publication timestamp. Provider write outcomes that are uncertain are never retried.
    """

    __tablename__ = "youtube_comment_queue_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued_generation', 'generating', 'pending_approval', 'scheduled', 'publishing', 'posted', 'failed', 'blocked_quota', 'outcome_unknown', 'cancelled')",
            name="ck_youtube_comment_queue_status",
        ),
        CheckConstraint("length(target_video_id) = 11", name="ck_youtube_comment_queue_video_id_length"),
        CheckConstraint("draft_source IN ('human', 'legacy_ai')", name="ck_youtube_comment_queue_draft_source"),
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_youtube_comment_queue_workspace_idempotency"),
        Index("ix_youtube_comment_queue_due", "status", "scheduled_at"),
        Index("ix_youtube_comment_queue_workspace_created", "workspace_id", "created_at"),
        Index("ix_youtube_comment_queue_account_created", "social_account_id", "created_at"),
        # Within a workspace, a target video can have only one live/posted queue item,
        # so different connected channels cannot coordinate on the same target video.
        Index(
            "uq_youtube_comment_queue_workspace_text_once",
            "workspace_id", "text_fingerprint", unique=True,
            postgresql_where=text("text_fingerprint IS NOT NULL AND status IN ('scheduled', 'publishing', 'posted', 'blocked_quota', 'outcome_unknown')"),
            sqlite_where=text("text_fingerprint IS NOT NULL AND status IN ('scheduled', 'publishing', 'posted', 'blocked_quota', 'outcome_unknown')"),
        ),
        Index(
            "uq_youtube_comment_queue_workspace_video_once",
            "workspace_id", "target_video_id", unique=True,
            postgresql_where=text("status IN ('queued_generation', 'generating', 'pending_approval', 'scheduled', 'publishing', 'posted', 'blocked_quota', 'outcome_unknown')"),
            sqlite_where=text("status IN ('queued_generation', 'generating', 'pending_approval', 'scheduled', 'publishing', 'posted', 'blocked_quota', 'outcome_unknown')"),
        ),
        # A connected channel may have only one active target at a time. Once a
        # comment is posted, failed safely, or cancelled, the channel may be reused.
        Index(
            "uq_youtube_comment_queue_workspace_active_channel_once",
            "workspace_id", "social_account_id", unique=True,
            postgresql_where=text("status IN ('queued_generation', 'generating', 'pending_approval', 'scheduled', 'publishing', 'blocked_quota', 'outcome_unknown')"),
            sqlite_where=text("status IN ('queued_generation', 'generating', 'pending_approval', 'scheduled', 'publishing', 'blocked_quota', 'outcome_unknown')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    social_account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued_generation", server_default="queued_generation")

    target_video_id: Mapped[str] = mapped_column(String(11), nullable=False)
    target_video_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    target_video_channel_id: Mapped[str] = mapped_column(String(180), nullable=False)
    target_video_title: Mapped[str] = mapped_column(String(1000), nullable=False)
    target_video_description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    target_video_thumbnail_url: Mapped[str | None] = mapped_column(String(1500), nullable=True)

    tone: Mapped[str] = mapped_column(String(24), nullable=False, default="helpful", server_default="helpful")
    language: Mapped[str] = mapped_column(String(40), nullable=False, default="match_comment", server_default="match_comment")
    custom_instructions: Mapped[str | None] = mapped_column(String(600), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False, default="top-level-video-comment-v1", server_default="top-level-video-comment-v1")
    draft_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Existing Phase 9 records are conservatively classified as legacy_ai by the
    # additive migration. New queue items must explicitly set this to human.
    draft_source: Mapped[str] = mapped_column(String(24), nullable=False, default="legacy_ai", server_default="legacy_ai")
    text_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_warnings: Mapped[list | dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_comment_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    approved_by_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class YouTubeCommentQueueState(Base):
    """Global posting pause used when YouTube reports project quota exhaustion."""

    __tablename__ = "youtube_comment_queue_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    updated_by_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
