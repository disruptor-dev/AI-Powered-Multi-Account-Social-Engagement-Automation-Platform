"""Add workspace slugs for multi-workspace management.

Revision ID: 20261027_0008
Revises: 20261027_0007
Create Date: 2026-10-27
"""
from __future__ import annotations

import re
import uuid

from alembic import op
import sqlalchemy as sa

revision = "20261027_0008"
down_revision = "20261027_0007"
branch_labels = None
depends_on = None


def _base_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "workspace").strip().lower()).strip("-")
    return (slug or "workspace")[:140]


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("slug", sa.String(length=160), nullable=True))
    op.add_column("workspaces", sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False))
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, name FROM workspaces ORDER BY created_at, id")).mappings().all()
    seen: set[str] = set()
    for index, row in enumerate(rows):
        base = _base_slug(str(row["name"] or "workspace"))
        slug = base
        if slug in seen:
            suffix = str(row["id"]).replace("-", "")[:8]
            slug = f"{base[:140]}-{suffix}"
        n = 2
        while slug in seen:
            slug = f"{base[:135]}-{n}"
            n += 1
        seen.add(slug)
        bind.execute(
            sa.text("UPDATE workspaces SET slug = :slug, is_default = :is_default WHERE id = :workspace_id"),
            {"slug": slug, "is_default": index == 0, "workspace_id": row["id"]},
        )
    with op.batch_alter_table("workspaces") as batch:
        batch.alter_column("slug", existing_type=sa.String(length=160), nullable=False)
        batch.create_unique_constraint("uq_workspaces_slug", ["slug"])
    op.create_index(
        "uq_workspaces_default_only", "workspaces", ["is_default"], unique=True,
        sqlite_where=sa.text("is_default = 1"),
        postgresql_where=sa.text("is_default = true"),
    )


def downgrade() -> None:
    op.drop_index("uq_workspaces_default_only", table_name="workspaces")
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_constraint("uq_workspaces_slug", type_="unique")
        batch.drop_column("is_default")
        batch.drop_column("slug")
