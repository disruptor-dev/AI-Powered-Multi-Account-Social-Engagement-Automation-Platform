from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AIReplyDraft(Base):
    """Persisted AI suggestion; it never publishes without an explicit owner action."""

    __tablename__ = "comment_ai_drafts"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'generating', 'draft', 'approved', 'rejected', 'published', 'failed')", name="ck_comment_ai_drafts_status"),
        CheckConstraint("tone IN ('helpful', 'friendly', 'professional', 'empathetic', 'concise')", name="ck_comment_ai_drafts_tone"),
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_comment_ai_drafts_workspace_idempotency"),
        Index("ix_comment_ai_drafts_workspace_created", "workspace_id", "created_at"),
        Index("ix_comment_ai_drafts_comment_created", "comment_id", "created_at"),
        Index("ix_comment_ai_drafts_status_enqueued", "status", "last_enqueued_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    comment_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("social_comments.id", ondelete="CASCADE"), nullable=False)
    target_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("social_accounts.id", ondelete="SET NULL"), nullable=True)
    automation_rule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("comment_automation_rules.id", ondelete="SET NULL"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    tone: Mapped[str] = mapped_column(String(24), nullable=False, default="helpful", server_default="helpful")
    language: Mapped[str] = mapped_column(String(40), nullable=False, default="match_comment", server_default="match_comment")
    custom_instructions: Mapped[str | None] = mapped_column(String(600), nullable=True)
    provider: Mapped[str] = mapped_column(String(80), nullable=False, default="openai_compatible", server_default="openai_compatible")
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False, default="reply-v1", server_default="reply-v1")
    context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
