"""Require human-authored text in the Human YouTube Comment Queue.

Revision ID: 20261027_0013
Revises: 20261027_0012
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa

revision = "20261027_0013"
down_revision = "20261027_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy rows are conservatively classified as legacy_ai. New application code
    # must explicitly write draft_source='human'.
    with op.batch_alter_table("youtube_comment_queue_items", recreate="auto") as batch_op:
        batch_op.add_column(
            sa.Column("draft_source", sa.String(length=24), nullable=False, server_default="legacy_ai"),
        )
        batch_op.create_check_constraint(
            "ck_youtube_comment_queue_draft_source",
            "draft_source IN ('human', 'legacy_ai')",
        )

    # Existing AI-generated drafts and scheduled writes must not be silently posted
    # after this migration. Publishing rows are ambiguous and require manual review.
    op.execute("""
        UPDATE youtube_comment_queue_items
        SET status = 'cancelled',
            error_code = 'legacy_ai_draft_disabled',
            cancelled_at = CURRENT_TIMESTAMP,
            finished_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE status IN ('queued_generation', 'generating', 'pending_approval', 'scheduled', 'blocked_quota')
    """)
    op.execute("""
        UPDATE youtube_comment_queue_items
        SET status = 'outcome_unknown',
            error_code = 'legacy_ai_write_outcome_review_required',
            finished_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE status = 'publishing'
    """)


def downgrade() -> None:
    with op.batch_alter_table("youtube_comment_queue_items", recreate="auto") as batch_op:
        batch_op.drop_constraint("ck_youtube_comment_queue_draft_source", type_="check")
        batch_op.drop_column("draft_source")
