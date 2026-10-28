from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import (
    create_member_session_token,
    get_current_admin,
    hash_password,
    require_csrf,
    require_workspace_role,
    verify_password_hash,
)
from app.models.audit_event import AuditEvent
from app.services.email_delivery import queue_transactional_email
from app.services.rate_limiting import rate_limit
from app.models.workspace import Workspace
from app.models.workspace_invitation import WorkspaceInvitation
from app.models.workspace_membership import WorkspaceMembership
from app.models.workspace_user import WorkspaceUser
from app.services.workspace_context import active_workspace
from app.services.team_management import (
    INVITABLE_ROLES,
    MAX_WORKSPACE_MEMBERS,
    active_member_count,
    audit_event,
    normalize_email,
    utcnow,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/team", tags=["workspace team"])
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class InvitationCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(default="viewer")

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = normalize_email(value)
        if not EMAIL_RE.match(value):
            raise ValueError("Enter a valid email address")
        return value

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in INVITABLE_ROLES:
            raise ValueError("Role must be admin, editor, or viewer")
        return value


class InvitationAccept(BaseModel):
    token: str = Field(min_length=20, max_length=512)
    full_name: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=12, max_length=256)

    @field_validator("full_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Name is required")
        return value


class MemberUpdate(BaseModel):
    role: str | None = None
    status: str | None = None

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: str | None) -> str | None:
        if value is not None and value not in INVITABLE_ROLES:
            raise ValueError("Role must be admin, editor, or viewer")
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str | None) -> str | None:
        if value is not None and value not in {"active", "suspended"}:
            raise ValueError("Status must be active or suspended")
        return value


def _workspace(db: Session) -> Workspace:
    return active_workspace(db)


def _actor_user_id(request: Request) -> uuid.UUID | None:
    raw = getattr(request.state, "current_user_id", None)
    try:
        return uuid.UUID(str(raw)) if raw else None
    except (ValueError, TypeError, AttributeError):
        return None


def _actor_role(request: Request) -> str:
    return str(getattr(request.state, "workspace_role", "viewer"))


def _secure_cookie() -> bool:
    settings = get_settings()
    return settings.app_env in ("staging", "production") or settings.session_cookie_samesite == "none"


def _member_item(user: WorkspaceUser, membership: WorkspaceMembership) -> dict:
    return {
        "user_id": str(user.id),
        "membership_id": str(membership.id),
        "email": user.email,
        "full_name": user.full_name,
        "role": membership.role,
        "status": membership.status,
        "joined_at": membership.joined_at.isoformat() if membership.joined_at else None,
        "created_at": membership.created_at.isoformat() if membership.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


def _invitation_item(invitation: WorkspaceInvitation) -> dict:
    expires = invitation.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return {
        "id": str(invitation.id),
        "email": invitation.email,
        "role": invitation.role,
        "status": invitation.status,
        "expires_at": expires.isoformat(),
        "created_at": invitation.created_at.isoformat() if invitation.created_at else None,
        "accepted_at": invitation.accepted_at.isoformat() if invitation.accepted_at else None,
    }


@router.get("/capabilities", summary="Return safe current-member capabilities")
def team_capabilities(
    request: Request,
    response: Response,
    email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    workspace = _workspace(db)
    role = _actor_role(request)
    role_rank = {"viewer": 0, "editor": 1, "admin": 2, "owner": 3}
    rank = role_rank.get(role, 0)
    count = int(db.scalar(select(func.count(WorkspaceMembership.id)).where(
        WorkspaceMembership.workspace_id == workspace.id,
        WorkspaceMembership.status == "active",
    )) or 0)
    return {
        "workspace_id": str(workspace.id),
        "role": role,
        "can_edit_comments": rank >= 1,
        "can_publish_comments": rank >= 1,
        "can_manage_accounts": rank >= 2,
        "can_manage_team": rank >= 2,
        "can_view_audit_log": rank >= 2,
        "active_member_count": count,
        "member_limit": MAX_WORKSPACE_MEMBERS,
    }


@router.get("/members", summary="List workspace members")
def list_members(
    response: Response,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    workspace = _workspace(db)
    rows = db.execute(
        select(WorkspaceUser, WorkspaceMembership)
        .join(WorkspaceMembership, WorkspaceMembership.user_id == WorkspaceUser.id)
        .where(WorkspaceMembership.workspace_id == workspace.id)
        .order_by(WorkspaceMembership.role.asc(), WorkspaceUser.full_name.asc(), WorkspaceUser.email.asc())
    ).all()
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [_member_item(user, membership) for user, membership in rows],
        "count": len(rows),
        "limit": MAX_WORKSPACE_MEMBERS,
    }


@router.get("/invitations", summary="List invitations without exposing token values")
def list_invitations(
    response: Response,
    _: str = Depends(get_current_admin),
    __: None = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
) -> dict:
    workspace = _workspace(db)
    now = utcnow()
    pending = db.scalars(select(WorkspaceInvitation).where(
        WorkspaceInvitation.workspace_id == workspace.id,
        WorkspaceInvitation.status == "pending",
    )).all()
    for item in pending:
        expires = item.expires_at if item.expires_at.tzinfo else item.expires_at.replace(tzinfo=timezone.utc)
        if expires <= now:
            item.status = "expired"
    db.commit()
    rows = db.scalars(select(WorkspaceInvitation).where(
        WorkspaceInvitation.workspace_id == workspace.id,
        WorkspaceInvitation.status.in_(["pending", "expired", "accepted", "revoked"]),
    ).order_by(WorkspaceInvitation.created_at.desc()).limit(100)).all()
    response.headers["Cache-Control"] = "no-store"
    return {"items": [_invitation_item(item) for item in rows], "count": len(rows)}


@router.post("/invitations", summary="Create a single-use workspace invitation", status_code=201)
def create_invitation(
    payload: InvitationCreate,
    request: Request,
    _: None = Depends(require_csrf),
    __: None = Depends(require_workspace_role("admin")),
    actor_email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
    __rate: None = Depends(rate_limit("invitation-create", limit_attr="rate_limit_invitation_limit", window_attr="rate_limit_invitation_window_seconds")),
) -> dict:
    workspace = _workspace(db)
    # Serialize capacity decisions for a workspace on PostgreSQL. All invite creation
    # and acceptance flows take this lock before counting memberships/invitations.
    workspace = db.scalar(select(Workspace).where(Workspace.id == workspace.id).with_for_update())
    if workspace is None:
        raise HTTPException(status_code=503, detail="Workspace is not available")
    role = _actor_role(request)
    email = normalize_email(payload.email)
    if get_settings().app_admin_email and email == normalize_email(get_settings().app_admin_email):
        raise HTTPException(status_code=409, detail="The configured workspace owner cannot be invited as a member")
    if role == "admin" and payload.role == "admin":
        raise HTTPException(status_code=403, detail="Only the workspace owner can invite another administrator")

    existing_user = db.scalar(select(WorkspaceUser).where(func.lower(WorkspaceUser.email) == email))
    if existing_user is not None:
        existing_membership = db.scalar(select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace.id,
            WorkspaceMembership.user_id == existing_user.id,
        ))
        if existing_membership is not None:
            raise HTTPException(status_code=409, detail="This email already has a workspace membership")

    now = utcnow()
    prior = db.scalars(select(WorkspaceInvitation).where(
        WorkspaceInvitation.workspace_id == workspace.id,
        func.lower(WorkspaceInvitation.email) == email,
        WorkspaceInvitation.status == "pending",
    )).all()
    for pending in prior:
        pending.status = "revoked"
    # Session factories may disable autoflush; persist revocations before counting
    # available invitation slots so replacement invites do not consume two slots.
    db.flush()

    open_invites = int(db.scalar(select(func.count(WorkspaceInvitation.id)).where(
        WorkspaceInvitation.workspace_id == workspace.id,
        WorkspaceInvitation.status == "pending",
        WorkspaceInvitation.expires_at > now,
    )) or 0)
    if active_member_count(db, workspace.id) + open_invites >= MAX_WORKSPACE_MEMBERS:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Workspace member limit ({MAX_WORKSPACE_MEMBERS}) reached")

    from app.services.team_management import create_invitation as create_invite_record
    invitation, raw_token = create_invite_record(
        db,
        workspace_id=workspace.id,
        email=email,
        role=payload.role,
        actor_user_id=_actor_user_id(request),
        actor_email=actor_email,
    )
    accept_url = f"{get_settings().frontend_app_url.rstrip('/')}/?invite={quote(raw_token, safe='')}"
    settings = get_settings()
    delivery = "copy_link"
    if settings.smtp_configured:
        try:
            queue_transactional_email(
                db,
                kind="workspace_invitation",
                workspace_id=workspace.id,
                recipient=email,
                subject=f"Invitation to {workspace.name} on Socialdesk",
                text_body=(
                    f"You have been invited to join {workspace.name} on Socialdesk as {payload.role}.\n\n"
                    f"Accept the invitation using this one-time link (expires in 7 days):\n{accept_url}\n\n"
                    "If you were not expecting this invitation, you can ignore this email."
                ),
            )
            db.commit()
            delivery = "queued"
        except Exception as exc:
            db.rollback()
            # Invitation remains valid and the one-time URL is returned only to the privileged inviter.
            logger.warning("invitation_email_queue_failed workspace=%s error_type=%s", workspace.id, type(exc).__name__)
            delivery = "copy_link_fallback"
    logger.info("Workspace invitation created: workspace=%s role=%s delivery=%s", workspace.id, payload.role, delivery)
    return {"invitation": _invitation_item(invitation), "accept_url": accept_url, "delivery": delivery}


@router.delete("/invitations/{invitation_id}", summary="Revoke a pending invitation")
def revoke_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    _: None = Depends(require_csrf),
    __: None = Depends(require_workspace_role("admin")),
    actor_email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    workspace = _workspace(db)
    invitation = db.scalar(select(WorkspaceInvitation).where(
        WorkspaceInvitation.id == invitation_id,
        WorkspaceInvitation.workspace_id == workspace.id,
    ))
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if invitation.status != "pending":
        raise HTTPException(status_code=409, detail="Only pending invitations can be revoked")
    invitation.status = "revoked"
    audit_event(db, workspace_id=workspace.id, actor_email=actor_email, actor_user_id=_actor_user_id(request),
                action="team.invitation_revoked", entity_type="invitation", entity_id=invitation.id,
                details={"invitee_email": invitation.email})
    db.commit()
    return {"status": "revoked", "invitation_id": str(invitation.id)}


@router.patch("/members/{user_id}", summary="Update a member role or active status")
def update_member(
    user_id: uuid.UUID,
    payload: MemberUpdate,
    request: Request,
    _: None = Depends(require_csrf),
    __: None = Depends(require_workspace_role("admin")),
    actor_email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    workspace = _workspace(db)
    if payload.role is None and payload.status is None:
        raise HTTPException(status_code=422, detail="Provide a role or status update")
    if str(user_id) == str(_actor_user_id(request)):
        raise HTTPException(status_code=409, detail="You cannot change your own role or status")

    target = db.execute(select(WorkspaceUser, WorkspaceMembership)
        .join(WorkspaceMembership, WorkspaceMembership.user_id == WorkspaceUser.id)
        .where(WorkspaceUser.id == user_id, WorkspaceMembership.workspace_id == workspace.id)
    ).first()
    if target is None:
        raise HTTPException(status_code=404, detail="Workspace member not found")
    user, membership = target
    actor_role = _actor_role(request)
    if membership.role == "owner":
        raise HTTPException(status_code=403, detail="The workspace owner cannot be changed or suspended")
    if actor_role != "owner" and membership.role == "admin":
        raise HTTPException(status_code=403, detail="Only the workspace owner can modify an administrator")
    if actor_role != "owner" and payload.role == "admin":
        raise HTTPException(status_code=403, detail="Only the workspace owner can promote a member to administrator")

    if payload.role is not None:
        membership.role = payload.role
    if payload.status is not None:
        if payload.status == "suspended" and membership.role == "owner":
            raise HTTPException(status_code=403, detail="The workspace owner cannot be suspended")
        membership.status = payload.status
    audit_event(db, workspace_id=workspace.id, actor_email=actor_email, actor_user_id=_actor_user_id(request),
                action="team.member_updated", entity_type="membership", entity_id=membership.id,
                details={"member_email": user.email, "role": membership.role, "status": membership.status})
    db.commit()
    return {"item": _member_item(user, membership)}


@router.delete("/members/{user_id}", summary="Remove a member from the workspace")
def remove_member(
    user_id: uuid.UUID,
    request: Request,
    _: None = Depends(require_csrf),
    __: None = Depends(require_workspace_role("admin")),
    actor_email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    workspace = _workspace(db)
    if str(user_id) == str(_actor_user_id(request)):
        raise HTTPException(status_code=409, detail="You cannot remove your own membership")
    membership = db.scalar(select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == workspace.id,
        WorkspaceMembership.user_id == user_id,
    ))
    user = db.get(WorkspaceUser, user_id)
    if membership is None or user is None:
        raise HTTPException(status_code=404, detail="Workspace member not found")
    if membership.role == "owner":
        raise HTTPException(status_code=403, detail="The workspace owner cannot be removed")
    if _actor_role(request) != "owner" and membership.role == "admin":
        raise HTTPException(status_code=403, detail="Only the workspace owner can remove an administrator")
    audit_event(db, workspace_id=workspace.id, actor_email=actor_email, actor_user_id=_actor_user_id(request),
                action="team.member_removed", entity_type="membership", entity_id=membership.id,
                details={"member_email": user.email, "role": membership.role})
    db.delete(membership)
    # A membership removal is tenant-scoped. Do not disable the identity globally
    # when it still belongs to another workspace; a session pinned to this removed
    # workspace will fail authorization immediately because its membership is gone.
    remaining_active = int(db.scalar(select(func.count(WorkspaceMembership.id)).where(
        WorkspaceMembership.user_id == user.id,
        WorkspaceMembership.status == "active",
        WorkspaceMembership.workspace_id != workspace.id,
    )) or 0)
    if remaining_active == 0:
        user.status = "disabled"
    db.commit()
    return {"status": "removed", "user_id": str(user_id)}


@router.get("/audit-log", summary="Read workspace security and team audit events")
def list_audit_events(
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000000),
    action: str | None = Query(default=None, max_length=100),
    _: str = Depends(get_current_admin),
    __: None = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
) -> dict:
    workspace = _workspace(db)
    statement = select(AuditEvent).where(AuditEvent.workspace_id == workspace.id)
    count_statement = select(func.count(AuditEvent.id)).where(AuditEvent.workspace_id == workspace.id)
    if action:
        statement = statement.where(AuditEvent.action == action)
        count_statement = count_statement.where(AuditEvent.action == action)
    total = int(db.scalar(count_statement) or 0)
    rows = db.scalars(statement.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).offset(offset).limit(limit)).all()
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [{
            "id": str(row.id), "actor_email": row.actor_email, "action": row.action,
            "entity_type": row.entity_type, "entity_id": row.entity_id,
            "details": row.details, "created_at": row.created_at.isoformat() if row.created_at else None,
        } for row in rows],
        "pagination": {"offset": offset, "limit": limit, "total": total, "has_more": offset + len(rows) < total},
    }


@router.post("/invitations/accept", summary="Accept an invitation and establish a member session")
def accept_invitation(
    payload: InvitationAccept,
    request: Request,
    response: Response,
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    __rate: None = Depends(rate_limit("invitation-accept", limit_attr="rate_limit_invitation_limit", window_attr="rate_limit_invitation_window_seconds")),
) -> dict[str, str]:
    digest = hashlib.sha256(payload.token.encode("utf-8")).hexdigest()
    invitation = db.scalar(select(WorkspaceInvitation).where(
        WorkspaceInvitation.token_digest == digest,
    ))
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation is invalid or no longer available")
    # Lock workspace first, consistently with invitation creation, so concurrent
    # accepts cannot race past the workspace member limit on PostgreSQL.
    workspace = db.scalar(select(Workspace).where(Workspace.id == invitation.workspace_id).with_for_update())
    if workspace is None:
        raise HTTPException(status_code=404, detail="Invitation workspace is no longer available")
    invitation = db.scalar(select(WorkspaceInvitation).where(
        WorkspaceInvitation.id == invitation.id,
        WorkspaceInvitation.token_digest == digest,
    ).with_for_update())
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation is invalid or no longer available")
    if invitation.status != "pending":
        raise HTTPException(status_code=409, detail="Invitation has already been accepted, revoked, or expired")
    expires = invitation.expires_at if invitation.expires_at.tzinfo else invitation.expires_at.replace(tzinfo=timezone.utc)
    if expires <= utcnow():
        invitation.status = "expired"
        db.commit()
        raise HTTPException(status_code=410, detail="Invitation has expired. Ask a workspace administrator for a new link.")

    settings = get_settings()
    if settings.app_admin_email and invitation.email == normalize_email(settings.app_admin_email):
        raise HTTPException(status_code=409, detail="The configured workspace owner cannot accept a member invitation")

    user = db.scalar(select(WorkspaceUser).where(func.lower(WorkspaceUser.email) == invitation.email).with_for_update())
    if user is None:
        user = WorkspaceUser(
            email=invitation.email,
            full_name=payload.full_name,
            password_hash=hash_password(payload.password),
            status="active",
        )
        db.add(user)
        db.flush()
    else:
        # A removed identity can rejoin only after a fresh, email-bound invitation
        # and successful verification of its existing password. The invitation
        # alone must not silently reactivate an old account.
        if user.password_hash and not verify_password_hash(user.password_hash, payload.password):
            raise HTTPException(status_code=401, detail="This email already has an account. Enter its existing password to accept the invitation.")
        if not user.password_hash:
            user.password_hash = hash_password(payload.password)
        user.full_name = payload.full_name
        user.status = "active"

    existing_membership = db.scalar(select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == invitation.workspace_id,
        WorkspaceMembership.user_id == user.id,
    ))
    if existing_membership is not None:
        raise HTTPException(status_code=409, detail="This user already has a membership in the workspace")
    if active_member_count(db, invitation.workspace_id) >= MAX_WORKSPACE_MEMBERS:
        raise HTTPException(status_code=409, detail=f"Workspace member limit ({MAX_WORKSPACE_MEMBERS}) reached")

    membership = WorkspaceMembership(
        workspace_id=invitation.workspace_id,
        user_id=user.id,
        role=invitation.role,
        status="active",
        invited_by_user_id=invitation.invited_by_user_id,
        joined_at=utcnow(),
    )
    db.add(membership)
    invitation.status = "accepted"
    invitation.accepted_at = utcnow()
    audit_event(db, workspace_id=invitation.workspace_id, actor_email=user.email, actor_user_id=user.id,
                action="team.invitation_accepted", entity_type="membership", entity_id=user.id,
                details={"role": invitation.role})
    db.commit()
    db.refresh(user)

    token = create_member_session_token(str(user.id), user.email, settings, workspace_id=invitation.workspace_id, session_version=int(user.session_version or 1))
    response.set_cookie(
        key=settings.session_cookie_name, value=token, httponly=True,
        secure=_secure_cookie(), samesite=settings.session_cookie_samesite,
        max_age=max(1, settings.session_ttl_hours) * 60 * 60, path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    workspace = db.get(Workspace, invitation.workspace_id)
    return {
        "email": user.email,
        "workspace_id": str(invitation.workspace_id),
        "workspace_name": workspace.name if workspace else get_settings().default_workspace_name,
        "role": invitation.role,
        "user_id": str(user.id),
        "full_name": user.full_name,
    }
