from __future__ import annotations

from collections.abc import Generator

import pytest
from argon2 import PasswordHasher
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.core.database import get_db
from app.main import app
from app.models.base import Base


TEST_PASSWORD = "Phase2-Test-Password-2026!"


@pytest.fixture
def owner_client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    password = TEST_PASSWORD
    monkeypatch.setenv("APP_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("APP_ADMIN_PASSWORD_HASH", PasswordHasher().hash(password))
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret-that-is-longer-than-32-characters")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "")
    monkeypatch.setenv("INSTAGRAM_APP_ID", "")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "")
    get_settings.cache_clear()

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, class_=Session, autoflush=False, autocommit=False)

    def override_get_db() -> Generator[Session, None, None]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    engine.dispose()
    get_settings.cache_clear()


def test_account_routes_require_login(owner_client: TestClient) -> None:
    response = owner_client.get("/api/v1/accounts")
    assert response.status_code == 401


def test_login_requires_csrf_and_creates_session(owner_client: TestClient) -> None:
    csrf_response = owner_client.get("/api/v1/auth/csrf")
    assert csrf_response.status_code == 200
    csrf = csrf_response.json()["csrf_token"]

    blocked = owner_client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": "wrong-token"},
        json={"email": "owner@example.com", "password": TEST_PASSWORD},
    )
    assert blocked.status_code == 403

    response = owner_client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": csrf},
        json={"email": "owner@example.com", "password": TEST_PASSWORD},
    )
    assert response.status_code == 200
    assert "socialdesk_session" in owner_client.cookies
    assert owner_client.get("/api/v1/auth/me").status_code == 200


def test_account_list_never_returns_tokens(owner_client: TestClient) -> None:
    csrf = owner_client.get("/api/v1/auth/csrf").json()["csrf_token"]
    login = owner_client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": csrf},
        json={"email": "owner@example.com", "password": TEST_PASSWORD},
    )
    assert login.status_code == 200
    body = owner_client.get("/api/v1/accounts").json()
    assert body["items"] == []
    assert "access_token" not in str(body).lower()
    assert body["token_encryption_ready"] is True


def test_oauth_start_fails_closed_without_provider_credentials(owner_client: TestClient) -> None:
    csrf = owner_client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = owner_client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": csrf},
        json={"email": "owner@example.com", "password": TEST_PASSWORD},
    )
    assert response.status_code == 200
    connect = owner_client.post("/api/v1/accounts/youtube/connect", headers={"X-CSRF-Token": csrf})
    assert connect.status_code == 503
