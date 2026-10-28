"""add AI reply drafts and draft-only automation rules

Revision ID: 20261027_0006
Revises: 20261026_0005
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa

revision = "20261027_0006"
down_revision = "20261026_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comment_automation_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("draft_settings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("priority BETWEEN 1 AND 1000", name="ck_comment_automation_rules_priority"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_comment_automation_rules_workspace_name"),
    )
    op.create_index("ix_comment_automation_rules_workspace_enabled_priority", "comment_automation_rules", ["workspace_id", "enabled", "priority"])
    op.create_index("ix_comment_automation_rules_enabled_scheduled", "comment_automation_rules", ["enabled", "last_scheduled_at"])

    op.create_table(
        "comment_ai_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("comment_id", sa.Uuid(), nullable=False),
        sa.Column("target_account_id", sa.Uuid(), nullable=True),
        sa.Column("automation_rule_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("tone", sa.String(length=24), server_default="helpful", nullable=False),
        sa.Column("language", sa.String(length=40), server_default="match_comment", nullable=False),
        sa.Column("custom_instructions", sa.String(length=600), nullable=True),
        sa.Column("provider", sa.String(length=80), server_default="openai_compatible", nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), server_default="reply-v1", nullable=False),
        sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("draft_text", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('queued', 'generating', 'draft', 'approved', 'rejected', 'published', 'failed')", name="ck_comment_ai_drafts_status"),
        sa.CheckConstraint("tone IN ('helpful', 'friendly', 'professional', 'empathetic', 'concise')", name="ck_comment_ai_drafts_tone"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["comment_id"], ["social_comments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_account_id"], ["social_accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["automation_rule_id"], ["comment_automation_rules.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name="uq_comment_ai_drafts_workspace_idempotency"),
    )
    op.create_index("ix_comment_ai_drafts_workspace_created", "comment_ai_drafts", ["workspace_id", "created_at"])
    op.create_index("ix_comment_ai_drafts_comment_created", "comment_ai_drafts", ["comment_id", "created_at"])
    op.create_index("ix_comment_ai_drafts_status_enqueued", "comment_ai_drafts", ["status", "last_enqueued_at"])

    op.create_table(
        "comment_automation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=True),
        sa.Column("trigger", sa.String(length=20), server_default="manual", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("scanned_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("matched_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("drafts_queued_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('queued', 'running', 'success', 'partial', 'error')", name="ck_comment_automation_runs_status"),
        sa.CheckConstraint("trigger IN ('manual', 'scheduled')", name="ck_comment_automation_runs_trigger"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rule_id"], ["comment_automation_rules.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_comment_automation_runs_workspace_queued", "comment_automation_runs", ["workspace_id", "queued_at"])
    op.create_index("ix_comment_automation_runs_rule_queued", "comment_automation_runs", ["rule_id", "queued_at"])
    op.create_index("ix_comment_automation_runs_status_queued", "comment_automation_runs", ["status", "queued_at"])

    op.create_table(
        "comment_automation_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("comment_id", sa.Uuid(), nullable=False),
        sa.Column("ai_draft_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('draft_queued', 'skipped', 'failed')", name="ck_comment_automation_executions_status"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rule_id"], ["comment_automation_rules.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["comment_id"], ["social_comments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ai_draft_id"], ["comment_ai_drafts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "comment_id", name="uq_comment_automation_executions_rule_comment"),
    )
    op.create_index("ix_comment_automation_executions_rule_created", "comment_automation_executions", ["rule_id", "created_at"])
    op.create_index("ix_comment_automation_executions_comment_created", "comment_automation_executions", ["comment_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_comment_automation_executions_comment_created", table_name="comment_automation_executions")
    op.drop_index("ix_comment_automation_executions_rule_created", table_name="comment_automation_executions")
    op.drop_table("comment_automation_executions")
    op.drop_index("ix_comment_automation_runs_status_queued", table_name="comment_automation_runs")
    op.drop_index("ix_comment_automation_runs_rule_queued", table_name="comment_automation_runs")
    op.drop_index("ix_comment_automation_runs_workspace_queued", table_name="comment_automation_runs")
    op.drop_table("comment_automation_runs")
    op.drop_index("ix_comment_ai_drafts_status_enqueued", table_name="comment_ai_drafts")
    op.drop_index("ix_comment_ai_drafts_comment_created", table_name="comment_ai_drafts")
    op.drop_index("ix_comment_ai_drafts_workspace_created", table_name="comment_ai_drafts")
    op.drop_table("comment_ai_drafts")
    op.drop_index("ix_comment_automation_rules_enabled_scheduled", table_name="comment_automation_rules")
    op.drop_index("ix_comment_automation_rules_workspace_enabled_priority", table_name="comment_automation_rules")
    op.drop_table("comment_automation_rules")
