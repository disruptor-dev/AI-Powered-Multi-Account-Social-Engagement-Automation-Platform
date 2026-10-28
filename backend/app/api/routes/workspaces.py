from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import (
    create_member_session_token,
    create_session_token,
    decode_session_payload,
    get_current_admin,
    require_csrf,
    require_workspace_role,
)
from app.models.workspace import Workspace
from app.models.workspace_membership import WorkspaceMembership
from app.models.workspace_user import WorkspaceUser
from app.services.workspace_management import (
    MAX_WORKSPACES_PER_USER,
    ensure_target_access,
    list_user_workspaces,
    record_workspace_audit,
    unique_slug,
    workspace_count,
)

router = APIRouter(prefix="/api/v1/workspaces", tags=["multi-workspace management"])


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 2:
            raise ValueError("Workspace name must contain at least 2 characters")
        return value


class WorkspaceUpdate(BaseModel):
    name: str = Field(min_length=2, max_length=120)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 2:
            raise ValueError("Workspace name must contain at least 2 characters")
        return value


class WorkspaceSwitch(BaseModel):
    workspace_id: uuid.UUID


def _actor_id(request: Request) -> uuid.UUID:
    try:
        return uuid.UUID(str(request.state.current_user_id))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=401, detail="Session identity is unavailable") from None


def _workspace_item(workspace: Workspace, membership: WorkspaceMembership, current_id: str) -> dict:
    return {
        "id": str(workspace.id),
        "name": workspace.name,
        "slug": workspace.slug,
        "is_default": workspace.is_default,
        "role": membership.role,
        "status": membership.status,
        "current": str(workspace.id) == current_id,
        "created_at": workspace.created_at.isoformat() if workspace.created_at else None,
        "updated_at": workspace.updated_at.isoformat() if workspace.updated_at else None,
    }


def _set_workspace_session(response: Response, request: Request, email: str, workspace_id: uuid.UUID) -> None:
    settings = get_settings()
    raw_token = request.cookies.get(settings.session_cookie_name, "")
    payload = decode_session_payload(raw_token, settings)
    if payload is None:
        raise HTTPException(status_code=401, detail="Login required")
    if payload.get("kind") == "owner_session":
        token = create_session_token(email, settings, workspace_id=workspace_id)
    else:
        token = create_member_session_token(str(_actor_id(request)), email, settings, workspace_id=workspace_id, session_version=int(getattr(request.state, "session_version", 1)))
    secure = settings.app_env in ("staging", "production") or settings.session_cookie_samesite == "none"
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        secure=secure,
        samesite=settings.session_cookie_samesite,
        max_age=max(1, settings.session_ttl_hours) * 60 * 60,
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


@router.get("", summary="List all active workspaces available to the current user")
def list_workspaces(
    request: Request,
    response: Response,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    user_id = _actor_id(request)
    current_id = str(request.state.workspace_id)
    rows = list_user_workspaces(db, user_id)
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [_workspace_item(workspace, membership, current_id) for workspace, membership in rows],
        "current_workspace_id": current_id,
        "count": len(rows),
        "workspace_limit": MAX_WORKSPACES_PER_USER,
        "can_create": request.state.workspace_role in {"owner", "admin"} and len(rows) < MAX_WORKSPACES_PER_USER,
    }


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create an independently isolated workspace")
def create_workspace(
    payload: WorkspaceCreate,
    request: Request,
    response: Response,
    _: None = Depends(require_csrf),
    __: None = Depends(require_workspace_role("admin")),
    email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    actor_id = _actor_id(request)
    # Lock the user row on PostgreSQL so concurrent workspace creation cannot
    # exceed the per-user limit. SQLite safely executes this without row locks.
    actor = db.scalar(select(WorkspaceUser).where(WorkspaceUser.id == actor_id).with_for_update())
    if actor is None or actor.status != "active":
        raise HTTPException(status_code=401, detail="Session identity is no longer active")
    if workspace_count(db, actor_id) >= MAX_WORKSPACES_PER_USER:
        raise HTTPException(status_code=409, detail=f"Workspace limit ({MAX_WORKSPACES_PER_USER}) reached")

    workspace = Workspace(name=payload.name, slug=unique_slug(db, payload.name))
    try:
        db.add(workspace)
        db.flush()
        membership = WorkspaceMembership(
            workspace_id=workspace.id,
            user_id=actor_id,
            role="owner",
            status="active",
            joined_at=datetime.now(timezone.utc),
        )
        db.add(membership)
        record_workspace_audit(
            db, workspace.id, actor_id, email, "workspace.created",
            {"name": workspace.name, "slug": workspace.slug},
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Workspace could not be created due to a name/slug conflict. Please try again.") from None
    db.refresh(workspace)
    _set_workspace_session(response, request, email, workspace.id)
    return {"workspace": _workspace_item(workspace, membership, str(workspace.id)), "active": True}


@router.post("/switch", summary="Switch the signed-in session to another authorized workspace")
def switch_workspace(
    payload: WorkspaceSwitch,
    request: Request,
    response: Response,
    _: None = Depends(require_csrf),
    email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    actor_id = _actor_id(request)
    workspace, membership = ensure_target_access(db, payload.workspace_id, actor_id)
    previous_workspace_id = str(request.state.workspace_id)
    record_workspace_audit(
        db, workspace.id, actor_id, email, "workspace.switched",
        {"previous_workspace_id": previous_workspace_id},
    )
    db.commit()
    _set_workspace_session(response, request, email, workspace.id)
    return {"workspace": _workspace_item(workspace, membership, str(workspace.id)), "active": True}


@router.patch("/{workspace_id}", summary="Rename a workspace the user owns or administers")
def rename_workspace(
    workspace_id: uuid.UUID,
    payload: WorkspaceUpdate,
    request: Request,
    response: Response,
    _: None = Depends(require_csrf),
    email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    actor_id = _actor_id(request)
    workspace, membership = ensure_target_access(db, workspace_id, actor_id)
    if membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Workspace owner or admin role required")
    old_name = workspace.name
    workspace.name = payload.name
    record_workspace_audit(
        db, workspace.id, actor_id, email, "workspace.renamed",
        {"old_name": old_name, "new_name": payload.name},
    )
    db.commit()
    db.refresh(workspace)
    response.headers["Cache-Control"] = "no-store"
    return {"workspace": _workspace_item(workspace, membership, str(request.state.workspace_id))}
