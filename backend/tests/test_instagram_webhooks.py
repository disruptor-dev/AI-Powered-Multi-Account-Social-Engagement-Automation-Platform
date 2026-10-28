from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.routes import webhooks
from app.main import app


def test_instagram_webhook_verification_echoes_plain_challenge(monkeypatch) -> None:
    monkeypatch.setattr(
        webhooks,
        "get_settings",
        lambda: SimpleNamespace(meta_webhook_verify_token="unit-test-verify-token", instagram_app_secret="unit-test-secret"),
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/webhooks/instagram",
            params={"hub.mode": "subscribe", "hub.verify_token": "unit-test-verify-token", "hub.challenge": "12345"},
        )
    assert response.status_code == 200
    assert response.text == "12345"
    assert "application/json" not in response.headers.get("content-type", "")


def test_instagram_webhook_verification_rejects_wrong_token(monkeypatch) -> None:
    monkeypatch.setattr(
        webhooks,
        "get_settings",
        lambda: SimpleNamespace(meta_webhook_verify_token="correct-token", instagram_app_secret="unit-test-secret"),
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/webhooks/instagram",
            params={"hub.mode": "subscribe", "hub.verify_token": "wrong-token", "hub.challenge": "12345"},
        )
    assert response.status_code == 403


def test_instagram_webhook_accepts_valid_signature(monkeypatch) -> None:
    app_secret = "test-meta-app-secret"
    monkeypatch.setattr(
        webhooks,
        "get_settings",
        lambda: SimpleNamespace(meta_webhook_verify_token="unit-test-token", instagram_app_secret=app_secret),
    )
    payload = b'{"object":"instagram","entry":[]}'
    signature = "sha256=" + hmac.new(app_secret.encode(), payload, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/webhooks/instagram",
            content=payload,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
        )
    assert response.status_code == 200


def test_instagram_webhook_rejects_invalid_signature(monkeypatch) -> None:
    monkeypatch.setattr(
        webhooks,
        "get_settings",
        lambda: SimpleNamespace(meta_webhook_verify_token="unit-test-token", instagram_app_secret="test-meta-app-secret"),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/webhooks/instagram",
            json={"object": "instagram", "entry": []},
            headers={"X-Hub-Signature-256": "sha256=wrong"},
        )
    assert response.status_code == 401
