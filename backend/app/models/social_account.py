import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SocialAccount(Base):
    """A connected YouTube channel or Instagram professional account.

    Access/refresh credentials are encrypted before being placed in these columns.
    """

    __tablename__ = "social_accounts"
    __table_args__ = (
        CheckConstraint("platform IN ('youtube', 'instagram')", name="ck_social_accounts_platform"),
        CheckConstraint(
            "status IN ('connected', 'reauthorization_required', 'disconnected')",
            name="ck_social_accounts_status",
        ),
        UniqueConstraint("workspace_id", "platform", "platform_account_id", name="uq_social_account_workspace_platform_id"),
        Index("ix_social_accounts_workspace_status", "workspace_id", "status"),
        Index("ix_social_accounts_platform_expiry", "platform", "access_token_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    platform_account_id: Mapped[str] = mapped_column(String(180), nullable=False)
    display_name: Mapped[str] = mapped_column(String(240), nullable=False)
    username: Mapped[str | None] = mapped_column(String(180), nullable=True)
    account_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="connected", server_default="connected")
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Phase 3: comment synchronization is a backend-owned background operation.
    last_sync_status: Mapped[str] = mapped_column(String(20), nullable=False, default="never", server_default="never")
    last_sync_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_sync_media_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_sync_comment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
