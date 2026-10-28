from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.security import (
    create_session_token,
    decrypt_secret,
    encrypt_secret,
    hash_password,
    read_session_token,
    verify_admin_password,
)
from app.models.base import Base
from app.models.workspace import Workspace
from app.services.account_connections import (
    build_authorization_url,
    consume_oauth_state,
    create_oauth_state,
)


def test_token_encryption_round_trip_and_no_plaintext_storage() -> None:
    settings = Settings(token_encryption_key=Fernet.generate_key().decode())
    encrypted = encrypt_secret("private-oauth-token", settings)
    assert encrypted is not None
    assert encrypted != "private-oauth-token"
    assert decrypt_secret(encrypted, settings) == "private-oauth-token"


def test_session_token_is_bound_to_configured_owner() -> None:
    password_hash = hash_password("Strong-password-2026!")
    settings = Settings(
        app_admin_email="owner@example.com",
        app_admin_password_hash=password_hash,
        session_secret_key="s" * 48,
    )
    assert verify_admin_password("OWNER@example.com", "Strong-password-2026!", settings)
    assert not verify_admin_password("other@example.com", "Strong-password-2026!", settings)
    token = create_session_token("owner@example.com", settings)
    assert read_session_token(token, settings) == "owner@example.com"
    other_owner_settings = Settings(
        app_admin_email="other@example.com",
        app_admin_password_hash=password_hash,
        session_secret_key="s" * 48,
    )
    assert read_session_token(token, other_owner_settings) is None


def test_youtube_authorization_url_uses_state_offline_access_and_expected_scope() -> None:
    settings = Settings(
        youtube_client_id="google-client-id",
        youtube_client_secret="google-client-secret",
        youtube_redirect_uri="http://localhost:8000/api/v1/accounts/youtube/callback",
        token_encryption_key=Fernet.generate_key().decode(),
    )
    url = build_authorization_url("youtube", "one-time-state", settings)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert query["state"] == ["one-time-state"]
    assert query["access_type"] == ["offline"]
    assert "https://www.googleapis.com/auth/youtube.force-ssl" in query["scope"][0]


def test_instagram_authorization_url_uses_instagram_login_and_comment_scope() -> None:
    settings = Settings(
        instagram_app_id="meta-app-id",
        instagram_app_secret="meta-app-secret",
        instagram_redirect_uri="http://localhost:8000/api/v1/accounts/instagram/callback",
        token_encryption_key=Fernet.generate_key().decode(),
    )
    url = build_authorization_url("instagram", "state-value", settings)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "www.instagram.com"
    assert query["state"] == ["state-value"]
    assert "instagram_business_manage_comments" in query["scope"][0]
    assert query["enable_fb_login"] == ["0"]


def test_oauth_state_is_one_time_and_owner_bound() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Unit test workspace")
        db.add(workspace)
        db.commit()
        db.refresh(workspace)
        state = create_oauth_state(db, "youtube", "owner@example.com", workspace.id)
        assert consume_oauth_state(db, state, "youtube", "different@example.com") is None
        # Wrong owner attempt consumes/invalidates the state intentionally.
        assert consume_oauth_state(db, state, "youtube", "owner@example.com") is None
        state2 = create_oauth_state(db, "youtube", "owner@example.com", workspace.id)
        record = consume_oauth_state(db, state2, "youtube", "owner@example.com")
        assert record is not None
        assert record.workspace_id == workspace.id
        assert consume_oauth_state(db, state2, "youtube", "owner@example.com") is None
    engine.dispose()
