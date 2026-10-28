"""Add security tokens, session versioning, and durable email outbox.

Revision ID: 20261027_0009
Revises: 20261027_0008
Create Date: 2026-10-27
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261027_0009"
down_revision = "20261027_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workspace_users") as batch:
        batch.add_column(sa.Column("session_version", sa.Integer(), server_default="1", nullable=False))

    op.create_table(
        "account_security_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("token_type", sa.String(length=32), server_default="password_reset", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("token_type IN ('password_reset')", name="ck_account_security_tokens_type"),
        sa.ForeignKeyConstraint(["user_id"], ["workspace_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index("ix_account_security_tokens_user_type_expiry", "account_security_tokens", ["user_id", "token_type", "expires_at"])
    op.create_index("ix_account_security_tokens_expiry", "account_security_tokens", ["expires_at"])

    op.create_table(
        "email_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=60), nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("encrypted_payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'sending', 'sent', 'failed')", name="ck_email_outbox_status"),
        sa.CheckConstraint("attempts >= 0", name="ck_email_outbox_attempts_nonnegative"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_outbox_status_next_attempt", "email_outbox", ["status", "next_attempt_at"])
    op.create_index("ix_email_outbox_workspace_created", "email_outbox", ["workspace_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_email_outbox_workspace_created", table_name="email_outbox")
    op.drop_index("ix_email_outbox_status_next_attempt", table_name="email_outbox")
    op.drop_table("email_outbox")
    op.drop_index("ix_account_security_tokens_expiry", table_name="account_security_tokens")
    op.drop_index("ix_account_security_tokens_user_type_expiry", table_name="account_security_tokens")
    op.drop_table("account_security_tokens")
    with op.batch_alter_table("workspace_users") as batch:
        batch.drop_column("session_version")
