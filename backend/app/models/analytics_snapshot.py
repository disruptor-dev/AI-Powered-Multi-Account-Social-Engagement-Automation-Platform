from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, JSON, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AnalyticsSnapshot(Base):
    """Workspace-isolated provider analytics values captured for a calendar day."""

    __tablename__ = "analytics_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "social_account_id", "metric_date", "metric_scope", "entity_key", "source",
            name="uq_analytics_snapshot_account_date_scope_entity_source",
        ),
        Index("ix_analytics_snapshots_workspace_date", "workspace_id", "metric_date"),
        Index("ix_analytics_snapshots_account_date", "social_account_id", "metric_date"),
        Index("ix_analytics_snapshots_workspace_scope_date", "workspace_id", "metric_scope", "metric_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    social_account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False)
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    metric_scope: Mapped[str] = mapped_column(String(20), nullable=False, default="account", server_default="account")
    entity_key: Mapped[str] = mapped_column(String(240), nullable=False, default="account", server_default="account")
    platform_media_id: Mapped[str | None] = mapped_column(String(220), nullable=True)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
