from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CommentSyncRun(Base):
    """Durable status and counters for one requested background sync job."""

    __tablename__ = "comment_sync_runs"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'success', 'partial', 'error')", name="ck_comment_sync_runs_status"),
        CheckConstraint("trigger IN ('manual', 'scheduled', 'webhook')", name="ck_comment_sync_runs_trigger"),
        Index("ix_comment_sync_runs_account_queued", "social_account_id", "queued_at"),
        Index("ix_comment_sync_runs_workspace_queued", "workspace_id", "queued_at"),
        Index("ix_comment_sync_runs_status_queued", "status", "queued_at"),
        Index(
            "uq_comment_sync_runs_one_active_per_account",
            "social_account_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False
    )
    job_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False, default="manual", server_default="manual")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    media_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    comments_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    comments_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    comments_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Set for a targeted YouTube video import; null means a normal account sync.
    target_platform_media_id: Mapped[str | None] = mapped_column(String(220), nullable=True)
