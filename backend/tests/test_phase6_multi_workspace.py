from __future__ import annotations

from collections.abc import Generator
from urllib.parse import parse_qs, urlparse

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

OWNER_PASSWORD = "Phase6-Owner-Testing-Password!"
MEMBER_PASSWORD = "Phase6-Strong-Member-Password!"


@pytest.fixture
def phase6_client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    monkeypatch.setenv("APP_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("APP_ADMIN_PASSWORD_HASH", PasswordHasher().hash(OWNER_PASSWORD))
    monkeypatch.setenv("SESSION_SECRET_KEY", "phase-six-test-session-secret-longer-than-32")
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
    return client.get("/api/v1/auth/csrf").json()["csrf_token"]


def _login_owner(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": _csrf(client)},
        json={"email": "owner@example.com", "password": OWNER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _mutate(client: TestClient, url: str, method: str, payload: dict) -> TestClient:
    response = client.request(method, url, headers={"X-CSRF-Token": _csrf(client)}, json=payload)
    assert response.status_code < 400, response.text
    return client


def _invite(client: TestClient, email: str, role: str) -> str:
    response = client.post(
        "/api/v1/team/invitations",
        headers={"X-CSRF-Token": _csrf(client)},
        json={"email": email, "role": role},
    )
    assert response.status_code == 201, response.text
    return parse_qs(urlparse(response.json()["accept_url"]).query)["invite"][0]


def test_owner_can_create_switch_and_rename_workspace(phase6_client: TestClient) -> None:
    original = _login_owner(phase6_client)
    original_id = original["workspace_id"]

    created = phase6_client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"name": "Client Alpha"},
    )
    assert created.status_code == 201, created.text
    new_workspace = created.json()["workspace"]
    assert new_workspace["name"] == "Client Alpha"
    assert new_workspace["slug"] == "client-alpha"
    assert new_workspace["role"] == "owner"
    assert phase6_client.get("/api/v1/auth/me").json()["workspace_id"] == new_workspace["id"]
    assert phase6_client.get("/api/v1/accounts").json()["workspace_id"] == new_workspace["id"]

    listing = phase6_client.get("/api/v1/workspaces")
    assert listing.status_code == 200
    assert listing.json()["count"] == 2
    assert {item["id"] for item in listing.json()["items"]} == {original_id, new_workspace["id"]}
    assert next(item for item in listing.json()["items"] if item["current"])["id"] == new_workspace["id"]
    default_item = next(item for item in listing.json()["items"] if item["id"] == original_id)
    active_item = next(item for item in listing.json()["items"] if item["id"] == new_workspace["id"])
    assert default_item["is_default"] is True
    assert active_item["is_default"] is False

    renamed = phase6_client.patch(
        f"/api/v1/workspaces/{new_workspace['id']}",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"name": "Client Alpha North"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["workspace"]["name"] == "Client Alpha North"
    assert renamed.json()["workspace"]["slug"] == "client-alpha"

    switched = phase6_client.post(
        "/api/v1/workspaces/switch",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"workspace_id": original_id},
    )
    assert switched.status_code == 200, switched.text
    current = phase6_client.get("/api/v1/auth/me")
    assert current.status_code == 200
    assert current.json()["workspace_id"] == original_id
    assert phase6_client.get("/api/v1/accounts").json()["workspace_id"] == original_id


def test_team_invitations_are_isolated_between_workspaces(phase6_client: TestClient) -> None:
    default = _login_owner(phase6_client)
    default_id = default["workspace_id"]
    created = phase6_client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"name": "Agency Client"},
    )
    assert created.status_code == 201, created.text
    other_id = created.json()["workspace"]["id"]
    token = _invite(phase6_client, "client-editor@example.com", "editor")
    assert token
    assert phase6_client.get("/api/v1/team/invitations").json()["count"] == 1

    switched = phase6_client.post(
        "/api/v1/workspaces/switch",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"workspace_id": default_id},
    )
    assert switched.status_code == 200, switched.text
    assert phase6_client.get("/api/v1/team/invitations").json()["count"] == 0
    assert phase6_client.get("/api/v1/team/members").json()["count"] == 1

    switched_back = phase6_client.post(
        "/api/v1/workspaces/switch",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"workspace_id": other_id},
    )
    assert switched_back.status_code == 200, switched_back.text
    assert phase6_client.get("/api/v1/team/invitations").json()["count"] == 1


def test_member_cannot_switch_to_workspace_without_membership(phase6_client: TestClient) -> None:
    owner = _login_owner(phase6_client)
    default_id = owner["workspace_id"]
    created = phase6_client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"name": "Private Client"},
    )
    assert created.status_code == 201, created.text
    private_id = created.json()["workspace"]["id"]
    assert phase6_client.post(
        "/api/v1/workspaces/switch",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"workspace_id": default_id},
    ).status_code == 200

    token = _invite(phase6_client, "viewer@example.com", "viewer")
    accepted = phase6_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"token": token, "full_name": "Workspace Viewer", "password": MEMBER_PASSWORD},
    )
    assert accepted.status_code == 200, accepted.text
    listing = phase6_client.get("/api/v1/workspaces").json()
    assert [item["id"] for item in listing["items"]] == [default_id]

    forbidden = phase6_client.post(
        "/api/v1/workspaces/switch",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"workspace_id": private_id},
    )
    assert forbidden.status_code == 403
    assert phase6_client.get("/api/v1/auth/me").json()["workspace_id"] == default_id


def test_workspace_rename_requires_target_membership_and_admin_role(phase6_client: TestClient) -> None:
    owner = _login_owner(phase6_client)
    default_id = owner["workspace_id"]
    created = phase6_client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"name": "Rename Guard"},
    )
    other_id = created.json()["workspace"]["id"]
    phase6_client.post(
        "/api/v1/workspaces/switch",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"workspace_id": default_id},
    )
    token = _invite(phase6_client, "read-only@example.com", "viewer")
    accepted = phase6_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"token": token, "full_name": "Read Only", "password": MEMBER_PASSWORD},
    )
    assert accepted.status_code == 200
    response = phase6_client.patch(
        f"/api/v1/workspaces/{other_id}",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"name": "Should Not Work"},
    )
    assert response.status_code == 403


def test_same_user_can_have_different_roles_in_two_workspaces(phase6_client: TestClient) -> None:
    default = _login_owner(phase6_client)
    default_id = default["workspace_id"]

    created = phase6_client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"name": "Brand Workspace"},
    )
    assert created.status_code == 201, created.text
    brand_id = created.json()["workspace"]["id"]

    editor_token = _invite(phase6_client, "multi-space@example.com", "editor")
    accepted_editor = phase6_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"token": editor_token, "full_name": "Multi Space User", "password": MEMBER_PASSWORD},
    )
    assert accepted_editor.status_code == 200, accepted_editor.text
    assert accepted_editor.json()["role"] == "editor"

    # The same identity can be added to a second workspace, but must prove its
    # existing password and gets a separate per-workspace role.
    owner_login = phase6_client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"email": "owner@example.com", "password": OWNER_PASSWORD},
    )
    assert owner_login.status_code == 200, owner_login.text
    default_invite = _invite(phase6_client, "multi-space@example.com", "viewer")
    accepted_viewer = phase6_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"token": default_invite, "full_name": "Multi Space User", "password": MEMBER_PASSWORD},
    )
    assert accepted_viewer.status_code == 200, accepted_viewer.text
    assert accepted_viewer.json()["role"] == "viewer"

    workspaces = phase6_client.get("/api/v1/workspaces").json()["items"]
    roles = {item["id"]: item["role"] for item in workspaces}
    assert roles == {default_id: "viewer", brand_id: "editor"}

    switched_brand = phase6_client.post(
        "/api/v1/workspaces/switch",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"workspace_id": brand_id},
    )
    assert switched_brand.status_code == 200, switched_brand.text
    assert phase6_client.get("/api/v1/team/capabilities").json()["role"] == "editor"

    switched_default = phase6_client.post(
        "/api/v1/workspaces/switch",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"workspace_id": default_id},
    )
    assert switched_default.status_code == 200, switched_default.text
    assert phase6_client.get("/api/v1/team/capabilities").json()["role"] == "viewer"


def test_removing_user_from_one_workspace_preserves_other_workspace_access(phase6_client: TestClient) -> None:
    default = _login_owner(phase6_client)
    default_id = default["workspace_id"]
    created = phase6_client.post(
        "/api/v1/workspaces",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"name": "Retained Membership"},
    )
    assert created.status_code == 201, created.text
    other_id = created.json()["workspace"]["id"]

    invite_other = _invite(phase6_client, "multi-membership@example.com", "editor")
    accepted_other = phase6_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"token": invite_other, "full_name": "Multi Membership", "password": MEMBER_PASSWORD},
    )
    assert accepted_other.status_code == 200
    user_id = accepted_other.json()["user_id"]

    # Add the same identity to the default workspace with a lower role.
    owner_login = phase6_client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"email": "owner@example.com", "password": OWNER_PASSWORD},
    )
    assert owner_login.status_code == 200
    invite_default = _invite(phase6_client, "multi-membership@example.com", "viewer")
    accepted_default = phase6_client.post(
        "/api/v1/team/invitations/accept",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"token": invite_default, "full_name": "Multi Membership", "password": MEMBER_PASSWORD},
    )
    assert accepted_default.status_code == 200

    owner_login = phase6_client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"email": "owner@example.com", "password": OWNER_PASSWORD},
    )
    assert owner_login.status_code == 200
    removed = phase6_client.delete(
        f"/api/v1/team/members/{user_id}",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
    )
    assert removed.status_code == 200, removed.text

    member_login = phase6_client.post(
        "/api/v1/auth/login",
        headers={"X-CSRF-Token": _csrf(phase6_client)},
        json={"email": "multi-membership@example.com", "password": MEMBER_PASSWORD},
    )
    assert member_login.status_code == 200, member_login.text
    current = phase6_client.get("/api/v1/auth/me")
    assert current.status_code == 200, current.text
    assert current.json()["workspace_id"] == other_id
    assert current.json()["role"] == "editor"
    assert current.json()["workspace_id"] != default_id
