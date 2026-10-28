from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import decrypt_secret, encrypt_secret, require_token_fernet
from app.models.oauth_state import OAuthState
from app.models.social_account import SocialAccount
from app.models.workspace import Workspace

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl", "https://www.googleapis.com/auth/yt-analytics.readonly"]
INSTAGRAM_SCOPES = ["instagram_business_basic", "instagram_business_manage_comments", "instagram_business_manage_insights"]
OAUTH_STATE_TTL_MINUTES = 10
HTTP_TIMEOUT_SECONDS = 20.0
MAX_CONNECTED_ACCOUNTS = 50


class OAuthFlowError(Exception):
    """Safe internal failure code; raw provider errors/tokens must not reach the browser."""

    def __init__(self, code: str = "provider_error") -> None:
        self.code = code
        super().__init__(code)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_default_workspace(db: Session, settings: Settings | None = None) -> Workspace:
    settings = settings or get_settings()
    workspace = db.scalar(select(Workspace).where(Workspace.is_default.is_(True)).limit(1))
    if workspace is None:
        workspace = db.scalar(select(Workspace).order_by(Workspace.created_at.asc(), Workspace.id.asc()).limit(1))
        if workspace is not None:
            workspace.is_default = True
            db.commit()
            db.refresh(workspace)
    if workspace is None:
        import re
        base_slug = re.sub(r"[^a-z0-9]+", "-", settings.default_workspace_name.strip().lower()).strip("-") or "workspace"
        slug = base_slug[:140]
        suffix = 2
        while db.scalar(select(Workspace.id).where(Workspace.slug == slug)) is not None:
            slug = f"{base_slug[:140-len(str(suffix))-1]}-{suffix}"
            suffix += 1
        workspace = Workspace(name=settings.default_workspace_name[:120], slug=slug, is_default=True)
        db.add(workspace)
        db.commit()
        db.refresh(workspace)
    return workspace


def requested_scopes(provider: str) -> list[str]:
    if provider == "youtube":
        return YOUTUBE_SCOPES.copy()
    if provider == "instagram":
        return INSTAGRAM_SCOPES.copy()
    raise HTTPException(status_code=404, detail="Unsupported platform")


def provider_is_configured(provider: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if provider == "youtube":
        return settings.youtube_oauth_configured
    if provider == "instagram":
        return settings.instagram_oauth_configured
    return False


def provider_redirect_uri(provider: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if provider == "youtube":
        return settings.youtube_redirect_uri
    if provider == "instagram":
        return settings.instagram_redirect_uri
    raise HTTPException(status_code=404, detail="Unsupported platform")


def assert_oauth_ready(provider: str, settings: Settings) -> None:
    if not provider_is_configured(provider, settings):
        env_vars = "YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET" if provider == "youtube" else "INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET"
        raise HTTPException(status_code=503, detail=f"Configure {env_vars} in backend environment")
    if not settings.token_encryption_configured:
        raise HTTPException(status_code=503, detail="Configure TOKEN_ENCRYPTION_KEY before connecting social accounts")
    require_token_fernet(settings)  # Fail before redirecting if the configured key is malformed.
    redirect_uri = provider_redirect_uri(provider, settings)
    if not redirect_uri.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise HTTPException(status_code=503, detail="OAuth callback must use HTTPS outside local development")


def create_oauth_state(db: Session, provider: str, owner_email: str, workspace_id: Any) -> str:
    raw_state = secrets.token_urlsafe(40)
    db.query(OAuthState).filter(
        (OAuthState.expires_at < utcnow()) | (OAuthState.consumed_at.is_not(None))
    ).delete(synchronize_session=False)
    db.add(OAuthState(
        state_digest=hashlib.sha256(raw_state.encode("utf-8")).hexdigest(),
        provider=provider,
        owner_email=owner_email.strip().lower(),
        workspace_id=workspace_id,
        expires_at=utcnow() + timedelta(minutes=OAUTH_STATE_TTL_MINUTES),
    ))
    db.commit()
    return raw_state


def consume_oauth_state(db: Session, raw_state: str, provider: str, owner_email: str) -> OAuthState | None:
    if not raw_state or len(raw_state) > 512:
        return None
    digest = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
    record = db.scalar(select(OAuthState).where(
        OAuthState.state_digest == digest, OAuthState.provider == provider
    ).with_for_update())
    if record is None or record.consumed_at is not None:
        return None
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= utcnow() or record.owner_email.lower() != owner_email.strip().lower():
        record.consumed_at = utcnow()
        db.commit()
        return None
    record.consumed_at = utcnow()
    db.commit()
    return record


def build_authorization_url(provider: str, state: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    assert_oauth_ready(provider, settings)
    redirect_uri = provider_redirect_uri(provider, settings)
    if provider == "youtube":
        params = {
            "client_id": settings.youtube_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(YOUTUBE_SCOPES),
            "state": state,
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
        }
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    params = {
        "client_id": settings.instagram_app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(INSTAGRAM_SCOPES),
        "state": state,
        "enable_fb_login": "0",
    }
    return "https://www.instagram.com/oauth/authorize?" + urlencode(params)


async def _post_form(url: str, data: dict[str, str]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.post(url, data=data, headers={"Accept": "application/json"})
        if response.status_code >= 400:
            raise OAuthFlowError("token_exchange_failed")
        result = response.json()
    except (httpx.HTTPError, ValueError):
        raise OAuthFlowError("provider_unavailable") from None
    if not isinstance(result, dict) or result.get("error") or not result.get("access_token"):
        raise OAuthFlowError("token_exchange_failed")
    return result


async def _get_json(url: str, *, params: dict[str, str], access_token: str | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.get(url, params=params, headers=headers)
        if response.status_code >= 400:
            raise OAuthFlowError("profile_fetch_failed")
        result = response.json()
    except (httpx.HTTPError, ValueError):
        raise OAuthFlowError("provider_unavailable") from None
    if not isinstance(result, dict) or result.get("error"):
        raise OAuthFlowError("profile_fetch_failed")
    return result


async def fetch_youtube_tokens_and_channels(code: str, settings: Settings) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tokens = await _post_form("https://oauth2.googleapis.com/token", {
        "client_id": settings.youtube_client_id,
        "client_secret": settings.youtube_client_secret,
        "code": code,
        "redirect_uri": settings.youtube_redirect_uri,
        "grant_type": "authorization_code",
    })
    channels_response = await _get_json(
        "https://youtube.googleapis.com/youtube/v3/channels",
        params={"part": "snippet", "mine": "true", "maxResults": "50"},
        access_token=str(tokens["access_token"]),
    )
    channels = channels_response.get("items")
    if not isinstance(channels, list):
        raise OAuthFlowError("no_youtube_channel_found")
    valid = [item for item in channels if isinstance(item, dict) and item.get("id")]
    if not valid:
        raise OAuthFlowError("no_youtube_channel_found")
    return tokens, valid


async def fetch_instagram_tokens_and_profile(code: str, settings: Settings) -> tuple[dict[str, Any], dict[str, Any]]:
    short = await _post_form("https://api.instagram.com/oauth/access_token", {
        "client_id": settings.instagram_app_id,
        "client_secret": settings.instagram_app_secret,
        "grant_type": "authorization_code",
        "redirect_uri": settings.instagram_redirect_uri,
        "code": code,
    })
    short_token = str(short["access_token"])
    long_lived = await _get_json("https://graph.instagram.com/access_token", params={
        "grant_type": "ig_exchange_token",
        "client_secret": settings.instagram_app_secret,
        "access_token": short_token,
    })
    long_token = long_lived.get("access_token")
    if not isinstance(long_token, str):
        raise OAuthFlowError("token_exchange_failed")
    profile = await _get_json(
        f"https://graph.instagram.com/{settings.meta_graph_api_version}/me",
        params={"fields": "id,user_id,username,account_type"},
        access_token=long_token,
    )
    long_lived.setdefault("expires_in", 5_184_000)
    return long_lived, profile


def _token_expiry(tokens: dict[str, Any]) -> datetime | None:
    try:
        seconds = int(tokens.get("expires_in"))
    except (TypeError, ValueError):
        return None
    return utcnow() + timedelta(seconds=seconds) if seconds > 0 else None


def _upsert_account(
    db: Session,
    *,
    workspace_id: Any,
    provider: str,
    account_id: str,
    display_name: str,
    username: str | None,
    account_type: str | None,
    avatar_url: str | None,
    tokens: dict[str, Any],
) -> None:
    account = db.scalar(select(SocialAccount).where(
        SocialAccount.workspace_id == workspace_id,
        SocialAccount.platform == provider,
        SocialAccount.platform_account_id == account_id,
    ))
    if account is None:
        account = SocialAccount(workspace_id=workspace_id, platform=provider, platform_account_id=account_id, display_name=display_name[:240])
        db.add(account)
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthFlowError("token_exchange_failed")
    account.display_name = display_name[:240]
    account.username = username[:180] if username else None
    account.account_type = account_type[:80] if account_type else None
    account.avatar_url = avatar_url[:1000] if avatar_url else None
    account.status = "connected"
    account.access_token_encrypted = encrypt_secret(access_token)
    refresh_token = tokens.get("refresh_token")
    # Google does not always return a fresh refresh token on repeated consent.
    if isinstance(refresh_token, str) and refresh_token:
        account.refresh_token_encrypted = encrypt_secret(refresh_token)
    elif account.refresh_token_encrypted is None:
        account.refresh_token_encrypted = None
    account.access_token_expires_at = _token_expiry(tokens)
    account.refresh_token_expires_at = account.access_token_expires_at if provider == "instagram" else None
    actual_scope = tokens.get("scope")
    account.scopes = actual_scope.split() if isinstance(actual_scope, str) else requested_scopes(provider)
    account.last_error_code = None
    account.updated_at = utcnow()
    db.flush()


def save_connected_accounts(db: Session, *, provider: str, workspace_id: Any, tokens: dict[str, Any], profiles: list[dict[str, Any]]) -> int:
    active_count = db.scalar(select(func.count()).select_from(SocialAccount).where(
        SocialAccount.workspace_id == workspace_id, SocialAccount.status != "disconnected"
    )) or 0
    count = 0
    for profile in profiles:
        account_id = profile.get("id") or profile.get("user_id")
        if not account_id:
            continue
        existing = db.scalar(select(SocialAccount).where(
            SocialAccount.workspace_id == workspace_id,
            SocialAccount.platform == provider,
            SocialAccount.platform_account_id == str(account_id),
        ))
        is_new_active_account = existing is None or existing.status == "disconnected"
        if is_new_active_account and active_count >= MAX_CONNECTED_ACCOUNTS:
            raise OAuthFlowError("account_limit_reached")
        if is_new_active_account:
            active_count += 1
        if provider == "youtube":
            snippet = profile.get("snippet") if isinstance(profile.get("snippet"), dict) else {}
            display_name = str(snippet.get("title") or f"YouTube channel {account_id}")
            username = snippet.get("customUrl")
            thumbs = snippet.get("thumbnails") if isinstance(snippet.get("thumbnails"), dict) else {}
            default_thumb = thumbs.get("default") if isinstance(thumbs.get("default"), dict) else {}
            avatar_url = default_thumb.get("url")
            account_type = "channel"
        else:
            display_name = str(profile.get("username") or f"Instagram account {account_id}")
            username = profile.get("username")
            avatar_url = None
            account_type = str(profile.get("account_type") or "professional")
        _upsert_account(
            db, workspace_id=workspace_id, provider=provider, account_id=str(account_id),
            display_name=display_name, username=str(username) if username else None,
            account_type=account_type, avatar_url=str(avatar_url) if avatar_url else None, tokens=tokens,
        )
        count += 1
    if not count:
        raise OAuthFlowError("no_account_found")
    db.commit()
    return count


async def refresh_social_account(account: SocialAccount, settings: Settings | None = None) -> None:
    """Refresh on explicit user action; scheduled refresh jobs can be added after Phase 2."""
    settings = settings or get_settings()
    if not settings.token_encryption_configured:
        raise HTTPException(status_code=503, detail="TOKEN_ENCRYPTION_KEY is not configured")
    if account.platform == "youtube":
        refresh_token = decrypt_secret(account.refresh_token_encrypted, settings)
        if not refresh_token:
            account.status = "reauthorization_required"
            account.last_error_code = "refresh_token_missing"
            raise HTTPException(status_code=409, detail="Reconnect this YouTube account to grant offline access")
        tokens = await _post_form("https://oauth2.googleapis.com/token", {
            "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
        account.access_token_encrypted = encrypt_secret(str(tokens["access_token"]), settings)
        account.access_token_expires_at = _token_expiry(tokens)
    elif account.platform == "instagram":
        access_token = decrypt_secret(account.access_token_encrypted, settings)
        if not access_token:
            account.status = "reauthorization_required"
            account.last_error_code = "access_token_missing"
            raise HTTPException(status_code=409, detail="Reconnect this Instagram account")
        tokens = await _get_json("https://graph.instagram.com/refresh_access_token", params={
            "grant_type": "ig_refresh_token", "access_token": access_token,
        })
        new_token = tokens.get("access_token")
        if not isinstance(new_token, str):
            raise OAuthFlowError("token_refresh_failed")
        account.access_token_encrypted = encrypt_secret(new_token, settings)
        account.access_token_expires_at = _token_expiry(tokens)
        account.refresh_token_expires_at = account.access_token_expires_at
    else:
        raise HTTPException(status_code=400, detail="Unsupported platform")
    account.status = "connected"
    account.last_error_code = None
    account.updated_at = utcnow()
