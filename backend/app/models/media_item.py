from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MediaItem(Base):
    """A provider-owned post/video that has been discovered for comment sync."""

    __tablename__ = "media_items"
    __table_args__ = (
        UniqueConstraint("social_account_id", "platform_media_id", name="uq_media_account_platform_id"),
        Index("ix_media_items_workspace_published", "workspace_id", "published_at"),
        Index("ix_media_items_account_published", "social_account_id", "published_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    platform_media_id: Mapped[str] = mapped_column(String(220), nullable=False)
    media_type: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown", server_default="unknown")
    source_type: Mapped[str] = mapped_column(String(30), nullable=False, default="account_video", server_default="account_video")
    title: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    permalink: Mapped[str | None] = mapped_column(String(1500), nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(String(1500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
