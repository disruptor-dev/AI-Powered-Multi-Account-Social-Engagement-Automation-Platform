from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import get_current_admin, require_csrf, require_token_fernet, require_workspace_role
from app.models.social_account import SocialAccount
from app.models.workspace_membership import WorkspaceMembership
from app.services.workspace_context import active_workspace
from app.services.rate_limiting import rate_limit
from app.services.account_connections import (
    OAuthFlowError,
    assert_oauth_ready,
    build_authorization_url,
    consume_oauth_state,
    create_oauth_state,
    fetch_instagram_tokens_and_profile,
    fetch_youtube_tokens_and_channels,
    provider_is_configured,
    refresh_social_account,
    save_connected_accounts,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/accounts", tags=["social accounts"])


class Platform(str, Enum):
    youtube = "youtube"
    instagram = "instagram"


class AccountSummary(BaseModel):
    id: str
    platform: str
    platform_account_id: str
    display_name: str
    username: str | None
    account_type: str | None
    avatar_url: str | None
    status: str
    access_token_expires_at: datetime | None
    connected_at: datetime
    last_error_code: str | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _connection_redirect(*, success: bool, platform: Platform, code: str | None = None, count: int | None = None) -> RedirectResponse:
    settings = get_settings()
    params: dict[str, str] = {
        "connection": "success" if success else "error",
        "platform": platform.value,
    }
    if code:
        params["code"] = code
    if count is not None:
        params["count"] = str(count)
    return RedirectResponse(f"{settings.frontend_app_url.rstrip('/')}/?{urlencode(params)}", status_code=303)


def _serialize_account(account: SocialAccount) -> AccountSummary:
    return AccountSummary(
        id=str(account.id),
        platform=account.platform,
        platform_account_id=account.platform_account_id,
        display_name=account.display_name,
        username=account.username,
        account_type=account.account_type,
        avatar_url=account.avatar_url,
        status=account.status,
        access_token_expires_at=account.access_token_expires_at,
        connected_at=account.connected_at,
        last_error_code=account.last_error_code,
    )


@router.get("", summary="List connected social accounts")
def list_accounts(
    response: Response,
    email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    workspace = active_workspace(db)
    accounts = db.scalars(
        select(SocialAccount)
        .where(SocialAccount.workspace_id == workspace.id)
        .order_by(SocialAccount.platform.asc(), SocialAccount.display_name.asc())
    ).all()
    # Tokens and OAuth secrets are intentionally never returned to the frontend.
    response.headers["Cache-Control"] = "no-store"
    try:
        require_token_fernet(settings)
        encryption_ready = True
    except HTTPException:
        encryption_ready = False
    return {
        "items": [_serialize_account(account).model_dump(mode="json") for account in accounts],
        "workspace_id": str(workspace.id),
        "platform_setup": {
            "youtube": provider_is_configured("youtube", settings),
            "instagram": provider_is_configured("instagram", settings),
        },
        "token_encryption_ready": encryption_ready,
        "account_limit": 50,
    }


@router.post("/{platform}/connect", summary="Start official platform OAuth")
def start_connect(
    platform: Platform,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("admin")),
    email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
    __rate: None = Depends(rate_limit("oauth-start", limit_attr="rate_limit_social_action_limit", window_attr="rate_limit_social_action_window_seconds")),
) -> dict[str, str | int]:
    settings = get_settings()
    assert_oauth_ready(platform.value, settings)
    workspace = active_workspace(db)
    state = create_oauth_state(db, platform.value, email, workspace.id)
    url = build_authorization_url(platform.value, state, settings)
    return {"authorization_url": url, "expires_in_seconds": 600}


@router.get("/{platform}/callback", summary="Complete official platform OAuth", include_in_schema=False)
async def oauth_callback(
    platform: Platform,
    request: Request,
    state: str | None = Query(default=None, max_length=512),
    code: str | None = Query(default=None, max_length=4096),
    error: str | None = Query(default=None, max_length=200),
    __role: None = Depends(require_workspace_role("admin")),
    email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    state_record = consume_oauth_state(db, state or "", platform.value, email)
    if state_record is None:
        return _connection_redirect(success=False, platform=platform, code="invalid_or_expired_state")
    try:
        actor_id = uuid.UUID(str(request.state.current_user_id))
    except (ValueError, TypeError, AttributeError):
        return _connection_redirect(success=False, platform=platform, code="workspace_access_changed")
    if db.scalar(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == state_record.workspace_id,
        WorkspaceMembership.user_id == actor_id,
        WorkspaceMembership.status == "active",
        WorkspaceMembership.role.in_(["owner", "admin"]),
    )) is None:
        return _connection_redirect(success=False, platform=platform, code="workspace_access_changed")
    if error:
        return _connection_redirect(success=False, platform=platform, code="authorization_cancelled")
    if not code:
        return _connection_redirect(success=False, platform=platform, code="authorization_code_missing")

    try:
        settings = get_settings()
        assert_oauth_ready(platform.value, settings)
        if platform == Platform.youtube:
            tokens, profiles = await fetch_youtube_tokens_and_channels(code, settings)
        else:
            tokens, profile = await fetch_instagram_tokens_and_profile(code, settings)
            profiles = [profile]
        count = save_connected_accounts(
            db,
            provider=platform.value,
            workspace_id=state_record.workspace_id,
            tokens=tokens,
            profiles=profiles,
        )
        return _connection_redirect(success=True, platform=platform, count=count)
    except OAuthFlowError as exc:
        db.rollback()
        logger.warning("OAuth connection failed: platform=%s code=%s", platform.value, exc.code)
        return _connection_redirect(success=False, platform=platform, code=exc.code)
    except HTTPException as exc:
        db.rollback()
        safe_code = "configuration_missing" if exc.status_code == 503 else "connection_failed"
        logger.warning("OAuth connection failed: platform=%s status=%s", platform.value, exc.status_code)
        return _connection_redirect(success=False, platform=platform, code=safe_code)
    except Exception:
        db.rollback()
        logger.exception("Unexpected OAuth callback failure for platform=%s", platform.value)
        return _connection_redirect(success=False, platform=platform, code="connection_failed")


@router.post("/{account_id}/refresh", summary="Refresh a connected account token")
async def refresh_account(
    account_id: uuid.UUID,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("admin")),
    email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
    __rate: None = Depends(rate_limit("account-refresh", limit_attr="rate_limit_social_action_limit", window_attr="rate_limit_social_action_window_seconds")),
) -> dict[str, str | None]:
    workspace = active_workspace(db)
    account = db.scalar(select(SocialAccount).where(
        SocialAccount.id == account_id,
        SocialAccount.workspace_id == workspace.id,
    ))
    if account is None or account.status == "disconnected":
        raise HTTPException(status_code=404, detail="Connected account not found")
    try:
        await refresh_social_account(account)
        db.commit()
    except OAuthFlowError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"Token refresh failed ({exc.code})") from None
    except HTTPException:
        # Persist a reauthorization_required marker if the refresh helper set one.
        db.commit()
        raise
    return {"status": account.status, "access_token_expires_at": account.access_token_expires_at.isoformat() if account.access_token_expires_at else None}


@router.delete("/{account_id}", summary="Disconnect a social account")
def disconnect_account(
    account_id: uuid.UUID,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("admin")),
    email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
    __rate: None = Depends(rate_limit("account-disconnect", limit_attr="rate_limit_social_action_limit", window_attr="rate_limit_social_action_window_seconds")),
) -> dict[str, str]:
    workspace = active_workspace(db)
    account = db.scalar(select(SocialAccount).where(
        SocialAccount.id == account_id,
        SocialAccount.workspace_id == workspace.id,
    ))
    if account is None:
        raise HTTPException(status_code=404, detail="Connected account not found")
    # Local disconnect removes our ability to use credentials. It does not revoke the
    # app's grant at Google/Instagram; users can revoke that grant in provider settings.
    account.status = "disconnected"
    account.access_token_encrypted = None
    account.refresh_token_encrypted = None
    account.access_token_expires_at = None
    account.refresh_token_expires_at = None
    account.scopes = []
    account.last_error_code = None
    account.updated_at = _utcnow()
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"status": "disconnected", "account_id": str(account.id)}
