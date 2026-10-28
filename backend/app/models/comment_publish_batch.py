from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CommentPublishBatch(Base):
    """Durable, user-confirmed multi-channel top-level comment publishing batch."""

    __tablename__ = "comment_publish_batches"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'success', 'partial', 'error')", name="ck_comment_publish_batches_status"),
        CheckConstraint("total_count BETWEEN 1 AND 10", name="ck_comment_publish_batches_total_count"),
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_comment_publish_batches_idempotency"),
        Index("ix_comment_publish_batches_status_created", "status", "created_at"),
        Index("ix_comment_publish_batches_workspace_created", "workspace_id", "created_at"),
        Index("ix_comment_publish_batches_video_created", "target_video_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    target_video_id: Mapped[str] = mapped_column(String(11), nullable=False)
    target_video_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    target_video_channel_id: Mapped[str | None] = mapped_column(String(180), nullable=True)
    target_video_title: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    target_video_thumbnail_url: Mapped[str | None] = mapped_column(String(1500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    published_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    unknown_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CommentPublishItem(Base):
    """One independent channel publish result inside a batch."""

    __tablename__ = "comment_publish_items"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'publishing', 'published', 'failed', 'unknown')", name="ck_comment_publish_items_status"),
        UniqueConstraint("batch_id", "social_account_id", name="uq_comment_publish_items_batch_account"),
        Index("ix_comment_publish_items_batch_position", "batch_id", "position"),
        Index("ix_comment_publish_items_account_created", "social_account_id", "created_at"),
        Index("ix_comment_publish_items_account_video_hash", "social_account_id", "target_video_id", "text_fingerprint", "status"),
        Index("uq_comment_publish_items_active_text_per_video", "social_account_id", "target_video_id", "text_fingerprint", unique=True, postgresql_where=text("status IN ('publishing', 'published', 'unknown')"), sqlite_where=text("status IN ('publishing', 'published', 'unknown')")),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("comment_publish_batches.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False
    )
    target_video_id: Mapped[str] = mapped_column(String(11), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_comment_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    provider_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
