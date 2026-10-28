"""Session, CSRF, and token-encryption helpers for the multi-workspace app.

Session workspace claims are signed hints only: each request must resolve the
workspace membership, active status, and role from the database before access.
"""

from __future__ import annotations

import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.workspace_membership import WorkspaceMembership
from app.models.workspace import Workspace
from app.models.workspace_user import WorkspaceUser

from app.core.config import Settings, get_settings

_password_hasher = PasswordHasher()


def create_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def require_csrf(request: Request) -> None:
    """Require the same unpredictable token in cookie and custom request header."""
    settings = get_settings()
    cookie_token = request.cookies.get(settings.csrf_cookie_name, "")
    header_token = request.headers.get("X-CSRF-Token", "")
    if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")


def verify_admin_password(email: str, password: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not settings.admin_login_configured:
        return False
    if not hmac.compare_digest(email.strip().lower(), settings.app_admin_email.strip().lower()):
        return False
    try:
        return bool(_password_hasher.verify(settings.app_admin_password_hash, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
        return False


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Use a password with at least 12 characters.")
    return _password_hasher.hash(password)


def create_session_token(email: str, settings: Settings | None = None, workspace_id: str | uuid.UUID | None = None) -> str:
    settings = settings or get_settings()
    if len(settings.session_secret_key) < 32:
        raise HTTPException(status_code=503, detail="SESSION_SECRET_KEY is not configured (minimum 32 characters)")
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": email.strip().lower(),
        "iat": now,
        "exp": now + timedelta(hours=max(1, settings.session_ttl_hours)),
        "kind": "owner_session",
    }
    if workspace_id is not None:
        payload["wid"] = str(workspace_id)
    return jwt.encode(payload, settings.session_secret_key, algorithm="HS256")


def decode_session_payload(token: str, settings: Settings | None = None) -> dict[str, Any] | None:
    settings = settings or get_settings()
    if len(settings.session_secret_key) < 32:
        return None
    try:
        payload = jwt.decode(
            token,
            settings.session_secret_key,
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "sub", "kind"]},
        )
    except jwt.PyJWTError:
        return None
    if not isinstance(payload.get("sub"), str):
        return None
    return payload


def read_session_token(token: str, settings: Settings | None = None) -> str | None:
    """Read a legacy configured-owner token; member tokens are handled by the DB-backed dependency."""
    settings = settings or get_settings()
    payload = decode_session_payload(token, settings)
    if payload is None or payload.get("kind") != "owner_session":
        return None
    subject = payload.get("sub")
    if not settings.app_admin_email or not hmac.compare_digest(
        subject.lower(), settings.app_admin_email.strip().lower()
    ):
        return None
    return subject


def create_member_session_token(user_id: str, email: str, settings: Settings | None = None, workspace_id: str | uuid.UUID | None = None, session_version: int = 1) -> str:
    settings = settings or get_settings()
    if len(settings.session_secret_key) < 32:
        raise HTTPException(status_code=503, detail="SESSION_SECRET_KEY is not configured (minimum 32 characters)")
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": email.strip().lower(),
        "uid": user_id,
        "iat": now,
        "exp": now + timedelta(hours=max(1, settings.session_ttl_hours)),
        "kind": "member_session",
        "sv": max(1, int(session_version)),
    }
    if workspace_id is not None:
        payload["wid"] = str(workspace_id)
    return jwt.encode(payload, settings.session_secret_key, algorithm="HS256")


def verify_password_hash(encoded_hash: str | None, password: str) -> bool:
    if not encoded_hash or not password:
        return False
    try:
        return bool(_password_hasher.verify(encoded_hash, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
        return False


def get_current_admin(request: Request, db: Session = Depends(get_db)) -> str:
    """Authenticate the user and bind this request to a membership-authorized workspace.

    The signed session's ``wid`` claim selects the active workspace. Old Phase 2/5
    sessions without that claim continue to resolve to the default workspace (or the
    member's first active workspace if they are not a member of the default one).
    """
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name, "")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")

    payload = decode_session_payload(token, settings)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    kind = payload.get("kind")
    requested_workspace: uuid.UUID | None = None
    if payload.get("wid") is not None:
        try:
            requested_workspace = uuid.UUID(str(payload.get("wid")))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid workspace session") from None

    from app.services.account_connections import ensure_default_workspace
    from app.services.team_management import ensure_owner_membership

    session_version = 1
    if kind == "owner_session":
        owner_email = read_session_token(token, settings)
        if not owner_email:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
        default_workspace = ensure_default_workspace(db, settings)
        owner = ensure_owner_membership(db, default_workspace, owner_email)
        if requested_workspace is None or requested_workspace == default_workspace.id:
            workspace = default_workspace
            membership = db.scalar(select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace.id,
                WorkspaceMembership.user_id == owner.id,
                WorkspaceMembership.status == "active",
            ))
        else:
            workspace = db.get(Workspace, requested_workspace)
            membership = db.scalar(select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == requested_workspace,
                WorkspaceMembership.user_id == owner.id,
                WorkspaceMembership.status == "active",
            )) if workspace is not None else None
        if workspace is None or membership is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have active access to this workspace")
        role = membership.role
        email = owner_email.strip().lower()
        current_user_id = owner.id
    elif kind == "member_session":
        try:
            user_id = uuid.UUID(str(payload.get("uid", "")))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required") from None
        email = str(payload.get("sub", "")).strip().lower()
        user = db.scalar(select(WorkspaceUser).where(WorkspaceUser.id == user_id))
        if user is None or user.status != "active" or not hmac.compare_digest(user.email.strip().lower(), email):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session is no longer active")
        session_version = payload.get("sv", 1)  # legacy Phase 5/6 cookies default to version 1
        if type(session_version) is not int or session_version != int(user.session_version or 1):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session was revoked after an account security change")
        session_version = int(user.session_version or 1)
        if requested_workspace is not None:
            workspace = db.get(Workspace, requested_workspace)
            membership = db.scalar(select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == requested_workspace,
                WorkspaceMembership.user_id == user.id,
                WorkspaceMembership.status == "active",
            )) if workspace is not None else None
        else:
            default_workspace = ensure_default_workspace(db, settings)
            workspace = default_workspace
            membership = db.scalar(select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == default_workspace.id,
                WorkspaceMembership.user_id == user.id,
                WorkspaceMembership.status == "active",
            ))
            if membership is None:
                row = db.execute(
                    select(Workspace, WorkspaceMembership)
                    .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
                    .where(WorkspaceMembership.user_id == user.id, WorkspaceMembership.status == "active")
                    .order_by(WorkspaceMembership.joined_at.asc(), WorkspaceMembership.created_at.asc())
                    .limit(1)
                ).first()
                if row:
                    workspace, membership = row
        if workspace is None or membership is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace membership is inactive")
        role = membership.role
        current_user_id = user.id
        email = user.email
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")

    request.state.workspace_role = role
    request.state.session_version = session_version
    request.state.current_user_id = str(current_user_id)
    request.state.current_email = email
    request.state.workspace_id = str(workspace.id)
    db.info["active_workspace_id"] = str(workspace.id)
    return email


def require_workspace_role(minimum_role: str):
    """Dependency factory for hierarchical owner/admin/editor/viewer access."""
    hierarchy = {"viewer": 0, "editor": 1, "admin": 2, "owner": 3}
    if minimum_role not in hierarchy:
        raise ValueError(f"Unknown workspace role: {minimum_role}")

    def dependency(request: Request, _: str = Depends(get_current_admin)) -> None:
        actual = getattr(request.state, "workspace_role", None)
        if actual not in hierarchy or hierarchy[actual] < hierarchy[minimum_role]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"{minimum_role.title()} role or higher required")

    return dependency

def require_token_fernet(settings: Settings | None = None) -> Fernet:
    settings = settings or get_settings()
    key = settings.token_encryption_key.strip().encode("ascii", errors="ignore")
    if not key:
        raise HTTPException(status_code=503, detail="TOKEN_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        raise HTTPException(status_code=503, detail="TOKEN_ENCRYPTION_KEY is invalid; generate a Fernet key") from None


def encrypt_secret(value: str | None, settings: Settings | None = None) -> str | None:
    if value is None or value == "":
        return None
    token = require_token_fernet(settings).encrypt(value.encode("utf-8"))
    return token.decode("ascii")


def decrypt_secret(value: str | None, settings: Settings | None = None) -> str | None:
    if value is None or value == "":
        return None
    try:
        return require_token_fernet(settings).decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise HTTPException(status_code=500, detail="Stored credential could not be decrypted; check TOKEN_ENCRYPTION_KEY") from None
