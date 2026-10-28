"""Add media, comments and durable synchronization state for Phase 3.

Revision ID: 20261026_0003
Revises: 20261023_0002
Create Date: 2026-10-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261026_0003"
down_revision: Union[str, None] = "20261023_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("social_accounts", sa.Column("last_sync_status", sa.String(length=20), server_default="never", nullable=False))
    op.add_column("social_accounts", sa.Column("last_sync_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("social_accounts", sa.Column("last_sync_completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("social_accounts", sa.Column("last_sync_error_code", sa.String(length=100), nullable=True))
    op.add_column("social_accounts", sa.Column("last_sync_media_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("social_accounts", sa.Column("last_sync_comment_count", sa.Integer(), server_default="0", nullable=False))

    op.create_table(
        "media_items",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("social_account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("platform_media_id", sa.String(length=220), nullable=False),
        sa.Column("media_type", sa.String(length=40), server_default="unknown", nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("permalink", sa.String(length=1500), nullable=True),
        sa.Column("thumbnail_url", sa.String(length=1500), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_metadata", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["social_account_id"], ["social_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("social_account_id", "platform_media_id", name="uq_media_account_platform_id"),
    )
    op.create_index("ix_media_items_workspace_published", "media_items", ["workspace_id", "published_at"])
    op.create_index("ix_media_items_account_published", "media_items", ["social_account_id", "published_at"])

    op.create_table(
        "social_comments",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("social_account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("media_item_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("parent_comment_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("platform_comment_id", sa.String(length=240), nullable=False),
        sa.Column("parent_platform_comment_id", sa.String(length=240), nullable=True),
        sa.Column("author_id", sa.String(length=240), nullable=True),
        sa.Column("author_name", sa.String(length=300), server_default="Unknown user", nullable=False),
        sa.Column("author_username", sa.String(length=240), nullable=True),
        sa.Column("author_avatar_url", sa.String(length=1500), nullable=True),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.Column("like_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reply_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("platform_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_reply", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_owner_comment", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("has_owner_reply", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("workflow_status", sa.String(length=20), server_default="open", nullable=False),
        sa.Column("raw_metadata", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("platform IN ('youtube', 'instagram')", name="ck_social_comments_platform"),
        sa.CheckConstraint("workflow_status IN ('open', 'resolved')", name="ck_social_comments_workflow_status"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["social_account_id"], ["social_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["media_item_id"], ["media_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_comment_id"], ["social_comments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("social_account_id", "platform_comment_id", name="uq_comment_account_platform_id"),
    )
    op.create_index("ix_social_comments_workspace_published", "social_comments", ["workspace_id", "published_at"])
    op.create_index("ix_social_comments_account_published", "social_comments", ["social_account_id", "published_at"])
    op.create_index("ix_social_comments_workspace_status", "social_comments", ["workspace_id", "workflow_status", "published_at"])
    op.create_index("ix_social_comments_media_parent", "social_comments", ["media_item_id", "parent_comment_id"])

    op.create_table(
        "comment_sync_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("social_account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("job_key", sa.String(length=80), nullable=False),
        sa.Column("trigger", sa.String(length=20), server_default="manual", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("media_processed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("comments_inserted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("comments_updated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("comments_skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.CheckConstraint("status IN ('queued', 'running', 'success', 'partial', 'error')", name="ck_comment_sync_runs_status"),
        sa.CheckConstraint("trigger IN ('manual', 'scheduled', 'webhook')", name="ck_comment_sync_runs_trigger"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["social_account_id"], ["social_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_key"),
    )
    op.create_index("ix_comment_sync_runs_account_queued", "comment_sync_runs", ["social_account_id", "queued_at"])
    op.create_index("ix_comment_sync_runs_workspace_queued", "comment_sync_runs", ["workspace_id", "queued_at"])
    op.create_index("ix_comment_sync_runs_status_queued", "comment_sync_runs", ["status", "queued_at"])
    op.create_index(
        "uq_comment_sync_runs_one_active_per_account",
        "comment_sync_runs",
        ["social_account_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
        sqlite_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_comment_sync_runs_one_active_per_account", table_name="comment_sync_runs")
    op.drop_index("ix_comment_sync_runs_status_queued", table_name="comment_sync_runs")
    op.drop_index("ix_comment_sync_runs_workspace_queued", table_name="comment_sync_runs")
    op.drop_index("ix_comment_sync_runs_account_queued", table_name="comment_sync_runs")
    op.drop_table("comment_sync_runs")
    op.drop_index("ix_social_comments_media_parent", table_name="social_comments")
    op.drop_index("ix_social_comments_workspace_status", table_name="social_comments")
    op.drop_index("ix_social_comments_account_published", table_name="social_comments")
    op.drop_index("ix_social_comments_workspace_published", table_name="social_comments")
    op.drop_table("social_comments")
    op.drop_index("ix_media_items_account_published", table_name="media_items")
    op.drop_index("ix_media_items_workspace_published", table_name="media_items")
    op.drop_table("media_items")
    op.drop_column("social_accounts", "last_sync_comment_count")
    op.drop_column("social_accounts", "last_sync_media_count")
    op.drop_column("social_accounts", "last_sync_error_code")
    op.drop_column("social_accounts", "last_sync_completed_at")
    op.drop_column("social_accounts", "last_sync_started_at")
    op.drop_column("social_accounts", "last_sync_status")
