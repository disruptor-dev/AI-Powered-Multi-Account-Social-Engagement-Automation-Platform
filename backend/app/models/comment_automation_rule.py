from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CommentAutomationRule(Base):
    """Rule filters plus AI draft settings. Public actions are draft-only, never auto-publish."""

    __tablename__ = "comment_automation_rules"
    __table_args__ = (
        CheckConstraint("priority BETWEEN 1 AND 1000", name="ck_comment_automation_rules_priority"),
        UniqueConstraint("workspace_id", "name", name="uq_comment_automation_rules_workspace_name"),
        Index("ix_comment_automation_rules_workspace_enabled_priority", "workspace_id", "enabled", "priority"),
        Index("ix_comment_automation_rules_enabled_scheduled", "enabled", "last_scheduled_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default="100")
    conditions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    draft_settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CommentAutomationRun(Base):
    __tablename__ = "comment_automation_runs"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'success', 'partial', 'error')", name="ck_comment_automation_runs_status"),
        CheckConstraint("trigger IN ('manual', 'scheduled')", name="ck_comment_automation_runs_trigger"),
        Index("ix_comment_automation_runs_workspace_queued", "workspace_id", "queued_at"),
        Index("ix_comment_automation_runs_rule_queued", "rule_id", "queued_at"),
        Index("ix_comment_automation_runs_status_queued", "status", "queued_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("comment_automation_rules.id", ondelete="SET NULL"), nullable=True)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False, default="manual", server_default="manual")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    scanned_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    drafts_queued_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CommentAutomationExecution(Base):
    __tablename__ = "comment_automation_executions"
    __table_args__ = (
        CheckConstraint("status IN ('draft_queued', 'skipped', 'failed')", name="ck_comment_automation_executions_status"),
        UniqueConstraint("rule_id", "comment_id", name="uq_comment_automation_executions_rule_comment"),
        Index("ix_comment_automation_executions_rule_created", "rule_id", "created_at"),
        Index("ix_comment_automation_executions_comment_created", "comment_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    rule_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("comment_automation_rules.id", ondelete="CASCADE"), nullable=False)
    comment_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("social_comments.id", ondelete="CASCADE"), nullable=False)
    ai_draft_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("comment_ai_drafts.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
