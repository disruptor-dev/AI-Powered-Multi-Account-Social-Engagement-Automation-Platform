"""Add workspace team members, invitations, RBAC, and audit events.

Revision ID: 20261027_0007
Revises: 20261027_0006
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261027_0007"
down_revision = "20261027_0006"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "workspace_users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("full_name", sa.String(160), server_default="Team member", nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=True),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_workspace_users_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_workspace_users_email"),
    )
    op.create_index("ix_workspace_users_status_created", "workspace_users", ["status", "created_at"])

    op.create_table(
        "workspace_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(20), server_default="viewer", nullable=False),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("invited_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('owner', 'admin', 'editor', 'viewer')", name="ck_workspace_memberships_role"),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="ck_workspace_memberships_status"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["workspace_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by_user_id"], ["workspace_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership_workspace_user"),
    )
    op.create_index("ix_workspace_memberships_workspace_role_status", "workspace_memberships", ["workspace_id", "role", "status"])

    op.create_table(
        "workspace_invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(20), server_default="viewer", nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("invited_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'editor', 'viewer')", name="ck_workspace_invitations_role"),
        sa.CheckConstraint("status IN ('pending', 'accepted', 'revoked', 'expired')", name="ck_workspace_invitations_status"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by_user_id"], ["workspace_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_workspace_invitations_token_digest"),
    )
    op.create_index("ix_workspace_invitations_workspace_status_expiry", "workspace_invitations", ["workspace_id", "status", "expires_at"])
    op.create_index("ix_workspace_invitations_email_status", "workspace_invitations", ["email", "status"])

    op.create_table(
        "workspace_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_email", sa.String(320), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(80), nullable=False),
        sa.Column("entity_id", sa.String(80), nullable=True),
        sa.Column("details", _json_type(), nullable=False),
        sa.Column("request_id", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["workspace_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workspace_audit_workspace_created", "workspace_audit_events", ["workspace_id", "created_at"])
    op.create_index("ix_workspace_audit_actor_created", "workspace_audit_events", ["actor_user_id", "created_at"])
    op.create_index("ix_workspace_audit_action_created", "workspace_audit_events", ["action", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_workspace_audit_action_created", table_name="workspace_audit_events")
    op.drop_index("ix_workspace_audit_actor_created", table_name="workspace_audit_events")
    op.drop_index("ix_workspace_audit_workspace_created", table_name="workspace_audit_events")
    op.drop_table("workspace_audit_events")
    op.drop_index("ix_workspace_invitations_email_status", table_name="workspace_invitations")
    op.drop_index("ix_workspace_invitations_workspace_status_expiry", table_name="workspace_invitations")
    op.drop_table("workspace_invitations")
    op.drop_index("ix_workspace_memberships_workspace_role_status", table_name="workspace_memberships")
    op.drop_table("workspace_memberships")
    op.drop_index("ix_workspace_users_status_created", table_name="workspace_users")
    op.drop_table("workspace_users")
