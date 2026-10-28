"""Track when inbox comments are resolved for analytics."""
from alembic import op
import sqlalchemy as sa

revision = "20261027_0011"
down_revision = "20261027_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("social_comments", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_social_comments_workspace_resolved", "social_comments", ["workspace_id", "resolved_at"])


def downgrade() -> None:
    op.drop_index("ix_social_comments_workspace_resolved", table_name="social_comments")
    op.drop_column("social_comments", "resolved_at")
