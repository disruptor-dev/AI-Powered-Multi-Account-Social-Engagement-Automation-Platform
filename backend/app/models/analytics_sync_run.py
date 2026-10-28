from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AnalyticsSyncRun(Base):
    """Durable record for one account analytics refresh."""

    __tablename__ = "analytics_sync_runs"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'success', 'partial', 'error')", name="ck_analytics_sync_runs_status"),
        CheckConstraint("trigger IN ('manual', 'scheduled')", name="ck_analytics_sync_runs_trigger"),
        Index("ix_analytics_sync_runs_workspace_queued", "workspace_id", "queued_at"),
        Index("ix_analytics_sync_runs_account_queued", "social_account_id", "queued_at"),
        Index("uq_analytics_sync_runs_one_active_per_account", "social_account_id", unique=True,
              postgresql_where=text("status IN ('queued', 'running')"), sqlite_where=text("status IN ('queued', 'running')")),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    social_account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False, default="manual", server_default="manual")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    rows_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    rows_stored: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
