"""Add durable provider analytics snapshots and sync-run tracking."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261027_0010"
down_revision = "20261027_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analytics_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("social_account_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("metric_scope", sa.String(length=20), server_default="account", nullable=False),
        sa.Column("entity_key", sa.String(length=240), server_default="account", nullable=False),
        sa.Column("platform_media_id", sa.String(length=220), nullable=True),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["social_account_id"], ["social_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("social_account_id", "metric_date", "metric_scope", "entity_key", "source", name="uq_analytics_snapshot_account_date_scope_entity_source"),
    )
    op.create_index("ix_analytics_snapshots_workspace_date", "analytics_snapshots", ["workspace_id", "metric_date"])
    op.create_index("ix_analytics_snapshots_account_date", "analytics_snapshots", ["social_account_id", "metric_date"])
    op.create_index("ix_analytics_snapshots_workspace_scope_date", "analytics_snapshots", ["workspace_id", "metric_scope", "metric_date"])

    op.create_table(
        "analytics_sync_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("social_account_id", sa.Uuid(), nullable=False),
        sa.Column("trigger", sa.String(length=20), server_default="manual", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("rows_received", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_stored", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('queued', 'running', 'success', 'partial', 'error')", name="ck_analytics_sync_runs_status"),
        sa.CheckConstraint("trigger IN ('manual', 'scheduled')", name="ck_analytics_sync_runs_trigger"),
        sa.ForeignKeyConstraint(["social_account_id"], ["social_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analytics_sync_runs_workspace_queued", "analytics_sync_runs", ["workspace_id", "queued_at"])
    op.create_index("ix_analytics_sync_runs_account_queued", "analytics_sync_runs", ["social_account_id", "queued_at"])
    op.create_index("uq_analytics_sync_runs_one_active_per_account", "analytics_sync_runs", ["social_account_id"], unique=True,
                    postgresql_where=sa.text("status IN ('queued', 'running')"), sqlite_where=sa.text("status IN ('queued', 'running')"))


def downgrade() -> None:
    op.drop_index("uq_analytics_sync_runs_one_active_per_account", table_name="analytics_sync_runs")
    op.drop_index("ix_analytics_sync_runs_account_queued", table_name="analytics_sync_runs")
    op.drop_index("ix_analytics_sync_runs_workspace_queued", table_name="analytics_sync_runs")
    op.drop_table("analytics_sync_runs")
    op.drop_index("ix_analytics_snapshots_workspace_scope_date", table_name="analytics_snapshots")
    op.drop_index("ix_analytics_snapshots_account_date", table_name="analytics_snapshots")
    op.drop_index("ix_analytics_snapshots_workspace_date", table_name="analytics_snapshots")
    op.drop_table("analytics_snapshots")
