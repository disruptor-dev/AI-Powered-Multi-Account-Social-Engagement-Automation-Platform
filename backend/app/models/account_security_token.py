from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AccountSecurityToken(Base):
    """One-time, expiring account recovery token; only a digest is persisted."""

    __tablename__ = "account_security_tokens"
    __table_args__ = (
        CheckConstraint("token_type IN ('password_reset')", name="ck_account_security_tokens_type"),
        Index("ix_account_security_tokens_user_type_expiry", "user_id", "token_type", "expires_at"),
        Index("ix_account_security_tokens_expiry", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace_users.id", ondelete="CASCADE"), nullable=False
    )
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_type: Mapped[str] = mapped_column(String(32), nullable=False, default="password_reset", server_default="password_reset")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
