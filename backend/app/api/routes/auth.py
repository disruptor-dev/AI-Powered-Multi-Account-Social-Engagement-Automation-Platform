from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import (
    create_csrf_token,
    create_member_session_token,
    create_session_token,
    get_current_admin,
    hash_password,
    require_csrf,
    verify_admin_password,
    verify_password_hash,
)
from app.models.workspace_membership import WorkspaceMembership
from app.models.workspace import Workspace
from app.models.workspace_user import WorkspaceUser
from app.models.account_security_token import AccountSecurityToken
from app.services.email_delivery import queue_transactional_email
from app.services.rate_limiting import rate_limit
from app.services.account_connections import ensure_default_workspace
from app.services.workspace_context import active_workspace
from app.services.team_management import audit_event, ensure_owner_membership, normalize_email, utcnow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class PasswordResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=20, max_length=512)
    password: str = Field(min_length=12, max_length=256)


def _secure_cookie() -> bool:
    settings = get_settings()
    return settings.app_env in ("staging", "production") or settings.session_cookie_samesite == "none"


def _set_session(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        secure=_secure_cookie(),
        samesite=settings.session_cookie_samesite,
        max_age=max(1, settings.session_ttl_hours) * 60 * 60,
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


@router.get("/csrf", summary="Create or return the browser CSRF token")
def get_csrf(request: Request, response: Response) -> dict[str, str]:
    settings = get_settings()
    csrf = request.cookies.get(settings.csrf_cookie_name)
    if not csrf or len(csrf) < 32:
        csrf = create_csrf_token()
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf,
        httponly=False,
        secure=_secure_cookie(),
        samesite=settings.session_cookie_samesite,
        max_age=60 * 60 * 24,
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"csrf_token": csrf}


@router.post("/login", summary="Start an owner or invited-member session")
def login(
    payload: LoginRequest,
    response: Response,
    _: None = Depends(require_csrf),
    __rate: None = Depends(rate_limit("auth-login", limit_attr="rate_limit_login_limit", window_attr="rate_limit_login_window_seconds")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    settings = get_settings()
    normalized_email = normalize_email(payload.email)

    if settings.admin_login_configured and verify_admin_password(normalized_email, payload.password, settings):
        workspace = ensure_default_workspace(db, settings)
        owner = ensure_owner_membership(db, workspace, settings.app_admin_email)
        _set_session(response, create_session_token(settings.app_admin_email, settings, workspace_id=workspace.id))
        logger.info("Configured owner session created")
        return {
            "email": settings.app_admin_email,
            "workspace_id": str(workspace.id),
            "workspace_name": workspace.name,
            "role": "owner",
            "user_id": str(owner.id),
            "full_name": owner.full_name,
        }

    user = db.scalar(select(WorkspaceUser).where(func.lower(WorkspaceUser.email) == normalized_email))
    if user is not None and user.status == "active" and verify_password_hash(user.password_hash, payload.password):
        default_workspace = ensure_default_workspace(db, settings)
        workspace = default_workspace
        membership = db.scalar(select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == default_workspace.id,
            WorkspaceMembership.user_id == user.id,
            WorkspaceMembership.status == "active",
        ))
        if membership is None:
            row = db.execute(
                select(WorkspaceMembership, Workspace)
                .join(Workspace, WorkspaceMembership.workspace_id == Workspace.id)
                .where(WorkspaceMembership.user_id == user.id, WorkspaceMembership.status == "active")
                .order_by(WorkspaceMembership.joined_at.asc(), WorkspaceMembership.created_at.asc())
                .limit(1)
            ).first()
            if row:
                membership, workspace = row
        if membership is not None:
            user.last_login_at = datetime.now(timezone.utc)
            db.commit()
            _set_session(response, create_member_session_token(str(user.id), user.email, settings, workspace_id=workspace.id, session_version=int(user.session_version or 1)))
            logger.info("Workspace member session created")
            return {
                "email": user.email,
                "workspace_id": str(workspace.id),
                "workspace_name": workspace.name,
                "role": membership.role,
                "user_id": str(user.id),
                "full_name": user.full_name,
            }

    if not settings.admin_login_configured and user is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Owner login is not configured. Set APP_ADMIN_EMAIL, APP_ADMIN_PASSWORD_HASH and SESSION_SECRET_KEY.",
        )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED, summary="Request a password reset email")
def request_password_reset(
    payload: PasswordResetRequest,
    _: None = Depends(require_csrf),
    __rate: None = Depends(rate_limit("password-reset-request", limit_attr="rate_limit_password_reset_limit", window_attr="rate_limit_password_reset_window_seconds")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Always return the same public result to avoid account enumeration."""
    settings = get_settings()
    email = normalize_email(payload.email)
    generic = {"status": "accepted", "message": "If an eligible account exists, password-reset instructions will be emailed shortly."}
    if not settings.smtp_configured or not settings.token_encryption_configured:
        return generic
    if settings.app_admin_email and email == normalize_email(settings.app_admin_email):
        # The bootstrap owner's hash lives in environment configuration and cannot be reset here.
        return generic
    user = db.scalar(select(WorkspaceUser).where(
        func.lower(WorkspaceUser.email) == email,
        WorkspaceUser.status == "active",
        WorkspaceUser.password_hash.is_not(None),
    ))
    if user is None:
        return generic

    now = utcnow()
    for old in db.scalars(select(AccountSecurityToken).where(
        AccountSecurityToken.user_id == user.id,
        AccountSecurityToken.token_type == "password_reset",
        AccountSecurityToken.consumed_at.is_(None),
    )).all():
        old.consumed_at = now

    raw_token = secrets.token_urlsafe(36)
    security_token = AccountSecurityToken(
        user_id=user.id,
        token_digest=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        token_type="password_reset",
        expires_at=now + timedelta(minutes=max(5, min(settings.password_reset_ttl_minutes, 120))),
    )
    db.add(security_token)
    reset_url = f"{settings.frontend_app_url.rstrip('/')}/?reset={quote(raw_token, safe='')}"
    memberships = db.scalars(select(WorkspaceMembership).where(
        WorkspaceMembership.user_id == user.id,
        WorkspaceMembership.status == "active",
    )).all()
    try:
        queue_transactional_email(
            db,
            kind="password_reset",
            recipient=user.email,
            subject="Reset your Socialdesk password",
            text_body=(
                f"Hello {user.full_name},\n\nA password reset was requested for your Socialdesk account."
                f"\n\nUse this one-time link within {max(5, min(settings.password_reset_ttl_minutes, 120))} minutes:\n{reset_url}"
                "\n\nIf you did not request this, ignore this email. Your password will not change unless the link is used."
            ),
        )
        for membership in memberships:
            audit_event(
                db, workspace_id=membership.workspace_id, actor_user_id=user.id, actor_email=user.email,
                action="account.password_reset_requested", entity_type="account", entity_id=user.id,
                details={"delivery_queued": True},
            )
        db.commit()
    except Exception as exc:
        db.rollback()
        # Deliberately don't disclose provider/configuration failure or account existence.
        logger.warning("password_reset_queue_failed error_type=%s", type(exc).__name__)
    return generic


@router.post("/password-reset/confirm", summary="Consume a password reset token and update the password")
def confirm_password_reset(
    payload: PasswordResetConfirm,
    _: None = Depends(require_csrf),
    __rate: None = Depends(rate_limit("password-reset-confirm", limit_attr="rate_limit_password_reset_limit", window_attr="rate_limit_password_reset_window_seconds")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    digest = hashlib.sha256(payload.token.encode("utf-8")).hexdigest()
    now = utcnow()
    token_row = db.scalar(select(AccountSecurityToken).where(
        AccountSecurityToken.token_digest == digest,
        AccountSecurityToken.token_type == "password_reset",
    ).with_for_update())
    if token_row is None or token_row.consumed_at is not None:
        raise HTTPException(status_code=400, detail="This password-reset link is invalid or has already been used")
    expiry = token_row.expires_at if token_row.expires_at.tzinfo else token_row.expires_at.replace(tzinfo=timezone.utc)
    if expiry <= now:
        token_row.consumed_at = now
        db.commit()
        raise HTTPException(status_code=400, detail="This password-reset link has expired. Request a new one.")
    user = db.scalar(select(WorkspaceUser).where(WorkspaceUser.id == token_row.user_id).with_for_update())
    if user is None or user.status != "active" or not user.password_hash:
        token_row.consumed_at = now
        db.commit()
        raise HTTPException(status_code=400, detail="This password-reset link is no longer valid")

    user.password_hash = hash_password(payload.password)
    user.session_version = int(user.session_version or 1) + 1
    token_row.consumed_at = now
    for pending in db.scalars(select(AccountSecurityToken).where(
        AccountSecurityToken.user_id == user.id,
        AccountSecurityToken.token_type == "password_reset",
        AccountSecurityToken.consumed_at.is_(None),
    )).all():
        pending.consumed_at = now
    memberships = db.scalars(select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)).all()
    for membership in memberships:
        audit_event(
            db, workspace_id=membership.workspace_id, actor_user_id=user.id, actor_email=user.email,
            action="account.password_reset_completed", entity_type="account", entity_id=user.id,
            details={"sessions_revoked": True},
        )
    db.commit()
    logger.info("password_reset_completed user_id=%s", user.id)
    return {"status": "password_updated", "message": "Password updated. Sign in again with your new password."}


@router.get("/me", summary="Get the current workspace session")
def me(request: Request, response: Response, email: str = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, str]:
    workspace = active_workspace(db)
    response.headers["Cache-Control"] = "no-store"
    role = getattr(request.state, "workspace_role", "owner")
    user_id = getattr(request.state, "current_user_id", "")
    user = None
    try:
        if user_id:
            user = db.get(WorkspaceUser, uuid.UUID(user_id))
    except (ValueError, TypeError):
        user = None
    return {
        "email": email,
        "workspace_id": str(workspace.id),
        "workspace_name": workspace.name,
        "role": role,
        "user_id": str(user.id) if user else user_id,
        "full_name": user.full_name if user else ("Workspace owner" if role == "owner" else email),
    }


@router.post("/logout", summary="End the current session")
def logout(
    response: Response,
    # Logout must remain available to a suspended member whose session has been
    # correctly invalidated by get_current_admin. CSRF still protects cookie clearing.
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    settings = get_settings()
    response.delete_cookie(settings.session_cookie_name, path="/", secure=_secure_cookie(), httponly=True, samesite=settings.session_cookie_samesite)
    response.headers["Cache-Control"] = "no-store"
    return {"status": "logged_out"}
