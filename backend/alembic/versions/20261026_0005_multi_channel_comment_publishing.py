"""Durable multi-channel YouTube comment publishing batches.

Revision ID: 20261026_0005
Revises: 20261026_0004
Create Date: 2026-10-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261026_0005"
down_revision: Union[str, None] = "20261026_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "comment_publish_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("target_video_id", sa.String(length=11), nullable=False),
        sa.Column("target_video_url", sa.String(length=2000), nullable=False),
        sa.Column("target_video_channel_id", sa.String(length=180), nullable=True),
        sa.Column("target_video_title", sa.String(length=1000), nullable=True),
        sa.Column("target_video_thumbnail_url", sa.String(length=1500), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("published_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unknown_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('queued', 'running', 'success', 'partial', 'error')", name="ck_comment_publish_batches_status"),
        sa.CheckConstraint("total_count BETWEEN 1 AND 10", name="ck_comment_publish_batches_total_count"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name="uq_comment_publish_batches_idempotency"),
    )
    op.create_index("ix_comment_publish_batches_status_created", "comment_publish_batches", ["status", "created_at"])
    op.create_index("ix_comment_publish_batches_workspace_created", "comment_publish_batches", ["workspace_id", "created_at"])
    op.create_index("ix_comment_publish_batches_video_created", "comment_publish_batches", ["target_video_id", "created_at"])

    op.create_table(
        "comment_publish_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("social_account_id", sa.Uuid(), nullable=False),
        sa.Column("target_video_id", sa.String(length=11), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("provider_comment_id", sa.String(length=240), nullable=True),
        sa.Column("provider_result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('queued', 'publishing', 'published', 'failed', 'unknown')", name="ck_comment_publish_items_status"),
        sa.ForeignKeyConstraint(["batch_id"], ["comment_publish_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["social_account_id"], ["social_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "social_account_id", name="uq_comment_publish_items_batch_account"),
    )
    op.create_index("ix_comment_publish_items_batch_position", "comment_publish_items", ["batch_id", "position"])
    op.create_index("ix_comment_publish_items_account_created", "comment_publish_items", ["social_account_id", "created_at"])
    op.create_index("ix_comment_publish_items_account_video_hash", "comment_publish_items", ["social_account_id", "target_video_id", "text_fingerprint", "status"])
    op.create_index(
        "uq_comment_publish_items_active_text_per_video",
        "comment_publish_items",
        ["social_account_id", "target_video_id", "text_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status IN ('publishing', 'published', 'unknown')"),
        sqlite_where=sa.text("status IN ('publishing', 'published', 'unknown')"),
    )


def downgrade() -> None:
    op.drop_index("uq_comment_publish_items_active_text_per_video", table_name="comment_publish_items")
    op.drop_index("ix_comment_publish_items_account_video_hash", table_name="comment_publish_items")
    op.drop_index("ix_comment_publish_items_account_created", table_name="comment_publish_items")
    op.drop_index("ix_comment_publish_items_batch_position", table_name="comment_publish_items")
    op.drop_table("comment_publish_items")
    op.drop_index("ix_comment_publish_batches_video_created", table_name="comment_publish_batches")
    op.drop_index("ix_comment_publish_batches_workspace_created", table_name="comment_publish_batches")
    op.drop_index("ix_comment_publish_batches_status_created", table_name="comment_publish_batches")
    op.drop_table("comment_publish_batches")
