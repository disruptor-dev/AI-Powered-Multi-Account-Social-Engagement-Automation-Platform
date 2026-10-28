from __future__ import annotations

import json
import sys
import types
from collections.abc import Generator
from urllib.parse import parse_qs, urlparse

import pytest
from argon2 import PasswordHasher
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.security import decrypt_secret
from app.main import app
from app.models.account_security_token import AccountSecurityToken
from app.models.base import Base
from app.models.email_outbox import EmailOutbox
from app.models.workspace_user import WorkspaceUser
from app.services import email_delivery, rate_limiting

OWNER_PASSWORD = "Phase7-Owner-Testing-Password!"
MEMBER_PASSWORD = "Phase7-Strong-Member-Password!"
NEW_PASSWORD = "Phase7-New-Strong-Password!"


@pytest.fixture
def phase7_context(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, sessionmaker], None, None]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("APP_ADMIN_PASSWORD_HASH", PasswordHasher().hash(OWNER_PASSWORD))
    monkeypatch.setenv("SESSION_SECRET_KEY", "phase-seven-test-session-secret-at-least-32")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FRONTEND_APP_URL", "http://localhost:3000")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "noreply@example.test")
    monkeypatch.setenv("SMTP_STARTTLS", "true")
    monkeypatch.setenv("SMTP_USE_SSL", "false")
    get_settings.cache_clear()

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, autoflush=False, autocommit=False)

    def override_get_db() -> Generator[Session, None, None]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client, factory
    app.dependency_overrides.clear()
    engine.dispose()
    get_settings.cache_clear()


def csrf(client: TestClient) -> str:
    return client.get("/api/v1/auth/csrf").json()["csrf_token"]


def owner_login(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": csrf(client)},
        json={"email": "owner@example.com", "password": OWNER_PASSWORD},
    )
    assert response.status_code == 200, response.text


def invite_member(client: TestClient, email: str = "member@example.com") -> str:
    response = client.post(
        "/api/v1/team/invitations",
        headers={"X-CSRF-Token": csrf(client)},
        json={"email": email, "role": "editor"},
    )
    assert response.status_code == 201, response.text
    return parse_qs(urlparse(response.json()["accept_url"]).query)["invite"][0]


def accept_member(client: TestClient, token: str, email: str = "member@example.com") -> None:
    response = client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": csrf(client)},
        json={"token": token, "full_name": "Phase 7 Member", "password": MEMBER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert response.json()["email"] == email


def latest_reset_token(factory: sessionmaker) -> str:
    db = factory()
    try:
        row = db.scalar(select(EmailOutbox).where(EmailOutbox.kind == "password_reset").order_by(EmailOutbox.created_at.desc()))
        assert row is not None
        payload = json.loads(decrypt_secret(row.encrypted_payload, get_settings()) or "{}")
        reset_url = next(part for part in payload["text_body"].splitlines() if "?reset=" in part)
        return parse_qs(urlparse(reset_url).query)["reset"][0]
    finally:
        db.close()


def test_invitation_is_queued_as_encrypted_email_when_smtp_is_configured(phase7_context) -> None:
    client, factory = phase7_context
    owner_login(client)
    response = client.post(
        "/api/v1/team/invitations",
        headers={"X-CSRF-Token": csrf(client)},
        json={"email": "queued@example.test", "role": "viewer"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["delivery"] == "queued"
    db = factory()
    try:
        item = db.scalar(select(EmailOutbox).where(EmailOutbox.kind == "workspace_invitation"))
        assert item is not None
        assert "invite=" not in item.encrypted_payload
        payload = json.loads(decrypt_secret(item.encrypted_payload, get_settings()) or "{}")
        assert "queued@example.test" not in payload["text_body"]
        assert "?invite=" in payload["text_body"]
    finally:
        db.close()


def test_member_password_reset_changes_password_revokes_sessions_and_is_single_use(phase7_context) -> None:
    client, factory = phase7_context
    owner_login(client)
    invitation_token = invite_member(client)
    accept_member(client, invitation_token)
    assert client.get("/api/v1/auth/me").status_code == 200

    requested = client.post(
        "/api/v1/auth/password-reset/request",
        headers={"X-CSRF-Token": csrf(client)},
        json={"email": "member@example.com"},
    )
    assert requested.status_code == 202
    assert requested.json()["status"] == "accepted"
    reset_token = latest_reset_token(factory)

    confirmed = client.post(
        "/api/v1/auth/password-reset/confirm",
        headers={"X-CSRF-Token": csrf(client)},
        json={"token": reset_token, "password": NEW_PASSWORD},
    )
    assert confirmed.status_code == 200, confirmed.text
    # The cookie belongs to the pre-reset session and the session version invalidates it.
    assert client.get("/api/v1/auth/me").status_code == 401

    reused = client.post(
        "/api/v1/auth/password-reset/confirm",
        headers={"X-CSRF-Token": csrf(client)},
        json={"token": reset_token, "password": "Another-Strong-Password!"},
    )
    assert reused.status_code == 400

    old_login = client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": csrf(client)},
        json={"email": "member@example.com", "password": MEMBER_PASSWORD},
    )
    assert old_login.status_code == 401
    new_login = client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": csrf(client)},
        json={"email": "member@example.com", "password": NEW_PASSWORD},
    )
    assert new_login.status_code == 200, new_login.text

    db = factory()
    try:
        tokens = db.scalars(select(AccountSecurityToken).where(AccountSecurityToken.token_type == "password_reset")).all()
        assert len(tokens) == 1 and tokens[0].consumed_at is not None
        user = db.scalar(select(WorkspaceUser).where(WorkspaceUser.email == "member@example.com"))
        assert user is not None and user.session_version == 2
    finally:
        db.close()


def test_password_reset_request_does_not_disclose_unknown_accounts(phase7_context) -> None:
    client, factory = phase7_context
    owner_login(client)
    response = client.post(
        "/api/v1/auth/password-reset/request",
        headers={"X-CSRF-Token": csrf(client)},
        json={"email": "not-found@example.test"},
    )
    assert response.status_code == 202
    assert response.json()["message"] == "If an eligible account exists, password-reset instructions will be emailed shortly."
    db = factory()
    try:
        assert db.scalar(select(EmailOutbox.id).where(EmailOutbox.kind == "password_reset")) is None
    finally:
        db.close()


def test_outbox_retries_transient_smtp_failure_then_marks_sent(phase7_context, monkeypatch) -> None:
    client, factory = phase7_context
    owner_login(client)
    # Use the existing workspace ID from the authenticated session.
    workspace_id = client.get("/api/v1/auth/me").json()["workspace_id"]
    db = factory()
    try:
        from app.services.email_delivery import queue_transactional_email
        from uuid import UUID
        row = queue_transactional_email(
            db, kind="test_delivery", workspace_id=UUID(workspace_id), recipient="test@example.test",
            subject="Test subject", text_body="Test message",
        )
        db.commit()
        row_id = row.id
    finally:
        db.close()

    calls = {"count": 0}
    def fake_send(recipient: str, subject: str, text_body: str) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError()
        assert recipient == "test@example.test" and subject == "Test subject" and text_body == "Test message"

    monkeypatch.setattr(email_delivery, "_send_smtp", fake_send)
    db = factory()
    try:
        assert email_delivery.process_one_email_outbox(db) is True
        row = db.get(EmailOutbox, row_id)
        assert row is not None and row.status == "pending" and row.attempts == 1
        row.next_attempt_at = email_delivery.utcnow()
        db.commit()
        assert email_delivery.process_one_email_outbox(db) is True
        row = db.get(EmailOutbox, row_id)
        assert row is not None and row.status == "sent" and row.attempts == 2
    finally:
        db.close()


def _request() -> Request:
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/api/v1/auth/login", "raw_path": b"/api/v1/auth/login", "query_string": b"",
        "headers": [], "client": ("192.0.2.10", 12345), "server": ("testserver", 80),
    })


def test_rate_limiter_returns_429_and_retry_after(monkeypatch) -> None:
    settings = Settings(app_env="production", session_secret_key="x" * 40, rate_limit_enabled=True, rate_limit_fail_closed=True)
    monkeypatch.setattr(rate_limiting, "get_settings", lambda: settings)
    fake_module = types.ModuleType("app.core.redis_client")
    class FakeRedis:
        def eval(self, script, key_count, key, window):
            return [11, 42]
    fake_module.get_redis_client = lambda: FakeRedis()
    monkeypatch.setitem(sys.modules, "app.core.redis_client", fake_module)
    with pytest.raises(HTTPException) as caught:
        rate_limiting.enforce_rate_limit(_request(), bucket="test", limit=10, window_seconds=60)
    assert caught.value.status_code == 429
    assert caught.value.headers["Retry-After"] == "42"


def test_rate_limiter_fails_closed_in_production_when_redis_is_unavailable(monkeypatch) -> None:
    settings = Settings(app_env="production", session_secret_key="x" * 40, rate_limit_enabled=True, rate_limit_fail_closed=True)
    monkeypatch.setattr(rate_limiting, "get_settings", lambda: settings)
    fake_module = types.ModuleType("app.core.redis_client")
    def fail():
        raise OSError("do not disclose this text")
    fake_module.get_redis_client = fail
    monkeypatch.setitem(sys.modules, "app.core.redis_client", fake_module)
    with pytest.raises(HTTPException) as caught:
        rate_limiting.enforce_rate_limit(_request(), bucket="test", limit=10, window_seconds=60)
    assert caught.value.status_code == 503
    assert "do not disclose" not in str(caught.value.detail)


def test_security_headers_and_request_id_are_attached(phase7_context) -> None:
    client, _ = phase7_context
    response = client.get("/health", headers={"X-Request-ID": "phase7.test-123"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "phase7.test-123"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"

    unsafe = client.get("/health", headers={"X-Request-ID": "bad\r\nInjected: header"})
    assert unsafe.status_code == 200
    assert unsafe.headers["X-Request-ID"] != "bad\r\nInjected: header"
