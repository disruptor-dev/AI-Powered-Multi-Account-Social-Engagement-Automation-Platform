"""Add human-approved, scheduled YouTube top-level comment queue.

Revision ID: 20261027_0012
Revises: 20261027_0011
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa

revision = "20261027_0012"
down_revision = "20261027_0011"
branch_labels = None
depends_on = None

ACTIVE_VIDEO_STATUSES = "'queued_generation', 'generating', 'pending_approval', 'scheduled', 'publishing', 'posted', 'blocked_quota', 'outcome_unknown'"


def upgrade() -> None:
    op.create_table(
        "youtube_comment_queue_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("social_account_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="queued_generation", nullable=False),
        sa.Column("target_video_id", sa.String(length=11), nullable=False),
        sa.Column("target_video_url", sa.String(length=2000), nullable=False),
        sa.Column("target_video_channel_id", sa.String(length=180), nullable=False),
        sa.Column("target_video_title", sa.String(length=1000), nullable=False),
        sa.Column("target_video_description", sa.Text(), server_default="", nullable=False),
        sa.Column("target_video_thumbnail_url", sa.String(length=1500), nullable=True),
        sa.Column("tone", sa.String(length=24), server_default="helpful", nullable=False),
        sa.Column("language", sa.String(length=40), server_default="match_comment", nullable=False),
        sa.Column("custom_instructions", sa.String(length=600), nullable=True),
        sa.Column("prompt_version", sa.String(length=40), server_default="top-level-video-comment-v1", nullable=False),
        sa.Column("draft_text", sa.Text(), nullable=True),
        sa.Column("text_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("approved_text", sa.Text(), nullable=True),
        sa.Column("quality_warnings", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("provider_comment_id", sa.String(length=240), nullable=True),
        sa.Column("approved_by_email", sa.String(length=320), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('queued_generation', 'generating', 'pending_approval', 'scheduled', 'publishing', 'posted', 'failed', 'blocked_quota', 'outcome_unknown', 'cancelled')", name="ck_youtube_comment_queue_status"),
        sa.CheckConstraint("length(target_video_id) = 11", name="ck_youtube_comment_queue_video_id_length"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["social_account_id"], ["social_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name="uq_youtube_comment_queue_workspace_idempotency"),
    )
    op.create_index("ix_youtube_comment_queue_due", "youtube_comment_queue_items", ["status", "scheduled_at"])
    op.create_index("ix_youtube_comment_queue_workspace_created", "youtube_comment_queue_items", ["workspace_id", "created_at"])
    op.create_index("ix_youtube_comment_queue_account_created", "youtube_comment_queue_items", ["social_account_id", "created_at"])
    op.create_index(
        "uq_youtube_comment_queue_workspace_text_once", "youtube_comment_queue_items",
        ["workspace_id", "text_fingerprint"], unique=True,
        postgresql_where=sa.text("text_fingerprint IS NOT NULL AND status IN ('scheduled', 'publishing', 'posted', 'blocked_quota', 'outcome_unknown')"),
        sqlite_where=sa.text("text_fingerprint IS NOT NULL AND status IN ('scheduled', 'publishing', 'posted', 'blocked_quota', 'outcome_unknown')"),
    )
    op.create_index(
        "uq_youtube_comment_queue_workspace_video_once", "youtube_comment_queue_items",
        ["workspace_id", "target_video_id"], unique=True,
        postgresql_where=sa.text(f"status IN ({ACTIVE_VIDEO_STATUSES})"),
        sqlite_where=sa.text(f"status IN ({ACTIVE_VIDEO_STATUSES})"),
    )
    op.create_index(
        "uq_youtube_comment_queue_workspace_active_channel_once", "youtube_comment_queue_items",
        ["workspace_id", "social_account_id"], unique=True,
        postgresql_where=sa.text("status IN ('queued_generation', 'generating', 'pending_approval', 'scheduled', 'publishing', 'blocked_quota', 'outcome_unknown')"),
        sqlite_where=sa.text("status IN ('queued_generation', 'generating', 'pending_approval', 'scheduled', 'publishing', 'blocked_quota', 'outcome_unknown')"),
    )
    op.create_table(
        "youtube_comment_queue_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pause_reason", sa.String(length=100), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by_email", sa.String(length=320), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO youtube_comment_queue_state (id, paused_at, pause_reason, updated_by_email) VALUES (1, NULL, NULL, NULL)")


def downgrade() -> None:
    op.drop_table("youtube_comment_queue_state")
    op.drop_index("uq_youtube_comment_queue_workspace_text_once", table_name="youtube_comment_queue_items")
    op.drop_index("uq_youtube_comment_queue_workspace_active_channel_once", table_name="youtube_comment_queue_items")
    op.drop_index("uq_youtube_comment_queue_workspace_video_once", table_name="youtube_comment_queue_items")
    op.drop_index("ix_youtube_comment_queue_account_created", table_name="youtube_comment_queue_items")
    op.drop_index("ix_youtube_comment_queue_workspace_created", table_name="youtube_comment_queue_items")
    op.drop_index("ix_youtube_comment_queue_due", table_name="youtube_comment_queue_items")
    op.drop_table("youtube_comment_queue_items")
