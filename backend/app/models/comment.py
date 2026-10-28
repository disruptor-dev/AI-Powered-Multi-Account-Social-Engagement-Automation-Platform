from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SocialComment(Base):
    """A deduplicated provider comment or reply; workflow state is local to this app."""

    __tablename__ = "social_comments"
    __table_args__ = (
        UniqueConstraint("social_account_id", "platform_comment_id", name="uq_comment_account_platform_id"),
        CheckConstraint("platform IN ('youtube', 'instagram')", name="ck_social_comments_platform"),
        CheckConstraint("workflow_status IN ('open', 'resolved')", name="ck_social_comments_workflow_status"),
        Index("ix_social_comments_workspace_published", "workspace_id", "published_at"),
        Index("ix_social_comments_workspace_resolved", "workspace_id", "resolved_at"),
        Index("ix_social_comments_account_published", "social_account_id", "published_at"),
        Index("ix_social_comments_workspace_status", "workspace_id", "workflow_status", "published_at"),
        Index("ix_social_comments_media_parent", "media_item_id", "parent_comment_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False
    )
    media_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False
    )
    parent_comment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("social_comments.id", ondelete="CASCADE"), nullable=True
    )
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    platform_comment_id: Mapped[str] = mapped_column(String(240), nullable=False)
    parent_platform_comment_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    author_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    author_name: Mapped[str] = mapped_column(String(300), nullable=False, default="Unknown user", server_default="Unknown user")
    author_username: Mapped[str | None] = mapped_column(String(240), nullable=True)
    author_avatar_url: Mapped[str | None] = mapped_column(String(1500), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    like_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reply_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    platform_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_owner_comment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    has_owner_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    workflow_status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", server_default="open")
    # Null for historical resolved comments whose actual resolution time was never tracked.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
