"""Add connected social accounts and one-time OAuth states.

Revision ID: 20261023_0002
Revises: 20261023_0001
Create Date: 2026-10-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261023_0002"
down_revision: Union[str, None] = "20261023_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "social_accounts",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("platform_account_id", sa.String(length=180), nullable=False),
        sa.Column("display_name", sa.String(length=240), nullable=False),
        sa.Column("username", sa.String(length=180), nullable=True),
        sa.Column("account_type", sa.String(length=80), nullable=True),
        sa.Column("avatar_url", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="connected", nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.CheckConstraint("platform IN ('youtube', 'instagram')", name="ck_social_accounts_platform"),
        sa.CheckConstraint("status IN ('connected', 'reauthorization_required', 'disconnected')", name="ck_social_accounts_status"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "platform", "platform_account_id", name="uq_social_account_workspace_platform_id"),
    )
    op.create_index("ix_social_accounts_workspace_status", "social_accounts", ["workspace_id", "status"], unique=False)
    op.create_index("ix_social_accounts_platform_expiry", "social_accounts", ["platform", "access_token_expires_at"], unique=False)

    op.create_table(
        "oauth_states",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("state_digest", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_email", sa.String(length=320), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("provider IN ('youtube', 'instagram')", name="ck_oauth_states_provider"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_digest"),
    )
    op.create_index("ix_oauth_states_expiry", "oauth_states", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_oauth_states_expiry", table_name="oauth_states")
    op.drop_table("oauth_states")
    op.drop_index("ix_social_accounts_platform_expiry", table_name="social_accounts")
    op.drop_index("ix_social_accounts_workspace_status", table_name="social_accounts")
    op.drop_table("social_accounts")
