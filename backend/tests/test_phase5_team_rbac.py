from __future__ import annotations

from collections.abc import Generator
from urllib.parse import urlparse, parse_qs

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


OWNER_PASSWORD = "Phase5-Owner-Testing-Password!"
MEMBER_PASSWORD = "Phase5-Strong-Member-Password!"


@pytest.fixture
def team_client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    monkeypatch.setenv("APP_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("APP_ADMIN_PASSWORD_HASH", PasswordHasher().hash(OWNER_PASSWORD))
    monkeypatch.setenv("SESSION_SECRET_KEY", "phase-five-test-session-secret-longer-than-32")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FRONTEND_APP_URL", "http://localhost:3000")
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "")
    get_settings.cache_clear()

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, class_=Session, autoflush=False, autocommit=False)

    def override_get_db() -> Generator[Session, None, None]:
        db = test_session()
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


def _csrf(client: TestClient) -> str:
    body = client.get("/api/v1/auth/csrf").json()
    return body["csrf_token"]


def _login_owner(client: TestClient) -> None:
    csrf = _csrf(client)
    response = client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": csrf},
        json={"email": "owner@example.com", "password": OWNER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "owner"


def _invite(client: TestClient, email: str, role: str) -> str:
    csrf = _csrf(client)
    response = client.post(
        "/api/v1/team/invitations",
        headers={"X-CSRF-Token": csrf},
        json={"email": email, "role": role},
    )
    assert response.status_code == 201, response.text
    url = response.json()["accept_url"]
    return parse_qs(urlparse(url).query)["invite"][0]


def test_owner_can_invite_and_accept_invitation_is_single_use(team_client: TestClient) -> None:
    _login_owner(team_client)
    token = _invite(team_client, "editor@example.com", "editor")

    pending = team_client.get("/api/v1/team/invitations")
    assert pending.status_code == 200
    assert pending.json()["items"][0]["email"] == "editor@example.com"
    assert token not in pending.text

    csrf = _csrf(team_client)
    accepted = team_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": csrf},
        json={"token": token, "full_name": "Team Editor", "password": MEMBER_PASSWORD},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["role"] == "editor"
    assert "socialdesk_session" in team_client.cookies
    assert team_client.get("/api/v1/auth/me").json()["role"] == "editor"

    duplicate = team_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"token": token, "full_name": "Team Editor", "password": MEMBER_PASSWORD},
    )
    assert duplicate.status_code == 409


def test_editor_can_read_but_cannot_manage_accounts_or_invite_admin(team_client: TestClient) -> None:
    _login_owner(team_client)
    token = _invite(team_client, "editor2@example.com", "editor")
    accepted = team_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"token": token, "full_name": "Editor Two", "password": MEMBER_PASSWORD},
    )
    assert accepted.status_code == 200
    assert team_client.get("/api/v1/accounts").status_code == 200

    forbidden_connect = team_client.post(
        "/api/v1/accounts/youtube/connect",
        headers={"X-CSRF-Token": _csrf(team_client)},
    )
    assert forbidden_connect.status_code == 403

    forbidden_invite = team_client.post(
        "/api/v1/team/invitations",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"email": "another@example.com", "role": "admin"},
    )
    assert forbidden_invite.status_code == 403


def test_suspended_member_sessions_are_invalidated_immediately(team_client: TestClient) -> None:
    _login_owner(team_client)
    token = _invite(team_client, "suspend@example.com", "viewer")
    accepted = team_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"token": token, "full_name": "Viewer", "password": MEMBER_PASSWORD},
    )
    assert accepted.status_code == 200
    user_id = accepted.json()["user_id"]
    member_token = team_client.cookies.get("socialdesk_session")
    assert member_token

    # Re-authenticate as owner to suspend the member, then restore the old member cookie.
    assert team_client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"email": "owner@example.com", "password": OWNER_PASSWORD},
    ).status_code == 200
    suspended = team_client.patch(
        f"/api/v1/team/members/{user_id}",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"status": "suspended"},
    )
    assert suspended.status_code == 200

    team_client.cookies.set("socialdesk_session", member_token, path="/")
    stale_session = team_client.get("/api/v1/auth/me")
    assert stale_session.status_code == 403
    # A suspended member must still be able to clear the invalid browser cookie.
    logout = team_client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": _csrf(team_client)})
    assert logout.status_code == 200
    assert "socialdesk_session" in logout.headers.get("set-cookie", "")
    assert "Max-Age=0" in logout.headers.get("set-cookie", "")


def test_viewer_cannot_create_automation_rule(team_client: TestClient) -> None:
    _login_owner(team_client)
    token = _invite(team_client, "viewer@example.com", "viewer")
    accepted = team_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"token": token, "full_name": "Read Only", "password": MEMBER_PASSWORD},
    )
    assert accepted.status_code == 200
    response = team_client.post(
        "/api/v1/automation/rules",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"name": "Blocked rule", "conditions": {}, "draft_settings": {}},
    )
    assert response.status_code == 403


def test_removed_member_can_rejoin_only_with_fresh_invitation_and_existing_password(team_client: TestClient) -> None:
    _login_owner(team_client)
    first_token = _invite(team_client, "returning@example.com", "editor")
    accepted = team_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"token": first_token, "full_name": "Returning Editor", "password": MEMBER_PASSWORD},
    )
    assert accepted.status_code == 200
    user_id = accepted.json()["user_id"]

    # Restore owner access to remove the member, then issue a fresh invitation.
    _login_owner(team_client)
    removed = team_client.delete(
        f"/api/v1/team/members/{user_id}",
        headers={"X-CSRF-Token": _csrf(team_client)},
    )
    assert removed.status_code == 200
    fresh_token = _invite(team_client, "returning@example.com", "viewer")

    wrong_password = team_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"token": fresh_token, "full_name": "Returning Editor", "password": "NotTheExistingPassword!"},
    )
    assert wrong_password.status_code == 401

    rejoined = team_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"token": fresh_token, "full_name": "Returning Editor", "password": MEMBER_PASSWORD},
    )
    assert rejoined.status_code == 200, rejoined.text
    assert rejoined.json()["role"] == "viewer"
    assert team_client.get("/api/v1/auth/me").json()["role"] == "viewer"


def test_admin_cannot_promote_a_member_to_admin(team_client: TestClient) -> None:
    _login_owner(team_client)
    admin_token = _invite(team_client, "admin@example.com", "admin")
    admin_accept = team_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"token": admin_token, "full_name": "Workspace Admin", "password": MEMBER_PASSWORD},
    )
    assert admin_accept.status_code == 200
    admin_cookie = team_client.cookies.get("socialdesk_session")
    assert admin_cookie

    editor_token = _invite(team_client, "editor3@example.com", "editor")
    editor_accept = team_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"token": editor_token, "full_name": "Workspace Editor", "password": MEMBER_PASSWORD},
    )
    assert editor_accept.status_code == 200
    editor_id = editor_accept.json()["user_id"]
    team_client.cookies.set("socialdesk_session", admin_cookie)

    forbidden = team_client.patch(
        f"/api/v1/team/members/{editor_id}",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"role": "admin"},
    )
    assert forbidden.status_code == 403


def test_replacing_pending_invitation_reuses_its_capacity_slot(team_client: TestClient) -> None:
    _login_owner(team_client)
    first_token = _invite(team_client, "replace-me@example.com", "viewer")
    for index in range(23):
        token = _invite(team_client, f"teammate-{index}@example.com", "viewer")
        assert token

    # Owner + 24 pending invites fills the workspace capacity guard (25).
    csrf = _csrf(team_client)
    replacement = team_client.post(
        "/api/v1/team/invitations",
        headers={"X-CSRF-Token": csrf},
        json={"email": "replace-me@example.com", "role": "editor"},
    )
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["invitation"]["role"] == "editor"
    assert replacement.json()["accept_url"]
    invitation_rows = team_client.get("/api/v1/team/invitations")
    same_email = [item for item in invitation_rows.json()["items"] if item["email"] == "replace-me@example.com"]
    assert sorted(item["status"] for item in same_email) == ["pending", "revoked"]


def test_viewer_can_read_member_directory_but_not_invitations_or_audit(team_client: TestClient) -> None:
    _login_owner(team_client)
    token = _invite(team_client, "directory-viewer@example.com", "viewer")
    accepted = team_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(team_client)},
        json={"token": token, "full_name": "Directory Viewer", "password": MEMBER_PASSWORD},
    )
    assert accepted.status_code == 200
    capabilities = team_client.get("/api/v1/team/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["role"] == "viewer"
    assert capabilities.json()["can_manage_team"] is False
    assert team_client.get("/api/v1/team/members").status_code == 200
    assert team_client.get("/api/v1/team/invitations").status_code == 403
    assert team_client.get("/api/v1/team/audit-log").status_code == 403
