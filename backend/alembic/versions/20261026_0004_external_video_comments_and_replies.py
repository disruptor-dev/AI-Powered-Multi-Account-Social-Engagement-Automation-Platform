"""Support targeted external YouTube videos and reply publishing.

Revision ID: 20261026_0004
Revises: 20261026_0003
Create Date: 2026-10-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261026_0004"
down_revision: Union[str, None] = "20261026_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "media_items",
        sa.Column("source_type", sa.String(length=30), server_default="account_video", nullable=False),
    )
    op.add_column(
        "comment_sync_runs",
        sa.Column("target_platform_media_id", sa.String(length=220), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("comment_sync_runs", "target_platform_media_id")
    op.drop_column("media_items", "source_type")
