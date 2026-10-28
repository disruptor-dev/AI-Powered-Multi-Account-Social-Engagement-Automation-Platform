from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.audit_event import AuditEvent
from app.models.workspace import Workspace
from app.models.workspace_invitation import WorkspaceInvitation
from app.models.workspace_membership import WorkspaceMembership
from app.models.workspace_user import WorkspaceUser
from app.services.account_connections import ensure_default_workspace

ROLES = ("owner", "admin", "editor", "viewer")
INVITABLE_ROLES = ("admin", "editor", "viewer")
MAX_WORKSPACE_MEMBERS = 25
INVITATION_TTL_HOURS = 168


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def audit_event(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    actor_email: str,
    actor_user_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        actor_email=normalize_email(actor_email),
        action=action[:100],
        entity_type=entity_type[:80],
        entity_id=(str(entity_id)[:80] if entity_id else None),
        details=details or {},
        request_id=(request_id[:100] if request_id else None),
    )
    db.add(event)
    return event


def ensure_owner_membership(db: Session, workspace: Workspace, email: str, full_name: str = "Workspace owner") -> WorkspaceUser:
    """Provision the env-configured owner into the membership system without changing auth."""
    normalized = normalize_email(email)
    user = db.scalar(select(WorkspaceUser).where(func.lower(WorkspaceUser.email) == normalized))
    if user is None:
        user = WorkspaceUser(email=normalized, full_name=full_name[:160], password_hash=None, status="active")
        db.add(user)
        db.flush()
    membership = db.scalar(select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == workspace.id,
        WorkspaceMembership.user_id == user.id,
    ))
    if membership is None:
        membership = WorkspaceMembership(
            workspace_id=workspace.id, user_id=user.id, role="owner", status="active", joined_at=utcnow()
        )
        db.add(membership)
    else:
        membership.role = "owner"
        membership.status = "active"
        if membership.joined_at is None:
            membership.joined_at = utcnow()
    db.commit()
    db.refresh(user)
    return user


def default_workspace_and_owner(db: Session, settings: Settings | None = None) -> tuple[Workspace, WorkspaceUser | None]:
    settings = settings or get_settings()
    workspace = ensure_default_workspace(db, settings)
    if settings.app_admin_email.strip():
        owner = ensure_owner_membership(db, workspace, settings.app_admin_email)
        return workspace, owner
    return workspace, None


def create_invitation(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    email: str,
    role: str,
    actor_user_id: uuid.UUID | None,
    actor_email: str,
) -> tuple[WorkspaceInvitation, str]:
    raw_token = secrets.token_urlsafe(36)
    invitation = WorkspaceInvitation(
        workspace_id=workspace_id,
        email=normalize_email(email),
        role=role,
        token_digest=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        status="pending",
        invited_by_user_id=actor_user_id,
        expires_at=utcnow() + timedelta(hours=INVITATION_TTL_HOURS),
    )
    db.add(invitation)
    db.flush()
    audit_event(
        db, workspace_id=workspace_id, actor_email=actor_email, actor_user_id=actor_user_id,
        action="team.invitation_created", entity_type="invitation", entity_id=invitation.id,
        details={"invitee_email": invitation.email, "role": role},
    )
    db.commit()
    db.refresh(invitation)
    return invitation, raw_token


def get_active_membership(db: Session, workspace_id: uuid.UUID, user_id: uuid.UUID) -> WorkspaceMembership | None:
    return db.scalar(select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.user_id == user_id,
        WorkspaceMembership.status == "active",
    ))


def active_member_count(db: Session, workspace_id: uuid.UUID) -> int:
    return int(db.scalar(select(func.count(WorkspaceMembership.id)).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.status == "active",
    )) or 0)
