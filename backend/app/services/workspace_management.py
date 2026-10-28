from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.workspace import Workspace
from app.models.workspace_membership import WorkspaceMembership
from app.services.team_management import audit_event, normalize_email

MAX_WORKSPACES_PER_USER = 25


def slug_base(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return (slug or "workspace")[:140]


def unique_slug(db: Session, name: str) -> str:
    base = slug_base(name)
    candidate = base
    suffix = 2
    while db.scalar(select(Workspace.id).where(Workspace.slug == candidate)) is not None:
        candidate = f"{base[:140-len(str(suffix))-1]}-{suffix}"
        suffix += 1
        if suffix > 10000:
            candidate = f"{base[:125]}-{uuid.uuid4().hex[:12]}"
            break
    return candidate


def list_user_workspaces(db: Session, user_id: uuid.UUID) -> list[tuple[Workspace, WorkspaceMembership]]:
    return db.execute(
        select(Workspace, WorkspaceMembership)
        .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
        .where(WorkspaceMembership.user_id == user_id, WorkspaceMembership.status == "active")
        .order_by(Workspace.created_at.asc(), Workspace.name.asc())
    ).all()


def membership_for(db: Session, workspace_id: uuid.UUID, user_id: uuid.UUID) -> WorkspaceMembership | None:
    return db.scalar(select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.user_id == user_id,
    ))


def ensure_target_access(db: Session, workspace_id: uuid.UUID, user_id: uuid.UUID) -> tuple[Workspace, WorkspaceMembership]:
    workspace = db.get(Workspace, workspace_id)
    membership = membership_for(db, workspace_id, user_id)
    if workspace is None or membership is None or membership.status != "active":
        raise HTTPException(status_code=403, detail="You do not have active access to this workspace")
    return workspace, membership


def workspace_count(db: Session, user_id: uuid.UUID) -> int:
    return int(db.scalar(select(func.count(WorkspaceMembership.id)).where(
        WorkspaceMembership.user_id == user_id,
        WorkspaceMembership.status == "active",
    )) or 0)


def record_workspace_audit(
    db: Session, workspace_id: uuid.UUID, actor_id: uuid.UUID, actor_email: str,
    action: str, details: dict | None = None,
) -> None:
    audit_event(
        db,
        workspace_id=workspace_id,
        actor_user_id=actor_id,
        actor_email=normalize_email(actor_email),
        action=action,
        entity_type="workspace",
        entity_id=str(workspace_id),
        details=details or {},
    )
