from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.core.database import get_db
from app.main import app
from app.models.base import Base
from app.models.comment import SocialComment
from app.models.media_item import MediaItem
from app.models.social_account import SocialAccount
from app.models.workspace import Workspace
from app.services import comment_jobs, comment_publish_jobs

TEST_PASSWORD = "Phase3-Test-Password-2026!"


class FakeRedis:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def lpush(self, key: str, value: str) -> int:
        self.items.insert(0, (key, value))
        return len(self.items)


@pytest.fixture
def phase3_client(monkeypatch: pytest.MonkeyPatch) -> Generator[dict, None, None]:
    monkeypatch.setenv("APP_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("APP_ADMIN_PASSWORD_HASH", PasswordHasher().hash(TEST_PASSWORD))
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret-that-is-longer-than-32-characters")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "")
    monkeypatch.setenv("INSTAGRAM_APP_ID", "")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "")
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
    fake_redis = FakeRedis()
    monkeypatch.setattr(comment_jobs, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(comment_publish_jobs, "get_redis_client", lambda: fake_redis)
    with TestClient(app) as client:
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        login = client.post(
            "/api/v1/auth/login",
            headers={"X-CSRF-Token": csrf},
            json={"email": "owner@example.com", "password": TEST_PASSWORD},
        )
        assert login.status_code == 200
        owner = test_session()
        try:
            workspace = owner.query(Workspace).first()
            assert workspace is not None
            account = SocialAccount(
                workspace_id=workspace.id,
                platform="youtube",
                platform_account_id="UC_phase3_test_channel",
                display_name="Phase 3 test channel",
                username="phase3channel",
                status="connected",
                scopes=["https://www.googleapis.com/auth/youtube.force-ssl"],
                access_token_encrypted="encrypted-test-token",
                last_sync_status="success",
                last_sync_completed_at=datetime.now(timezone.utc),
            )
            owner.add(account)
            owner.flush()
            media = MediaItem(
                workspace_id=workspace.id,
                social_account_id=account.id,
                platform="youtube",
                platform_media_id="video_phase3_test",
                media_type="video",
                title="Phase 3 testing walkthrough",
                permalink="https://www.youtube.com/watch?v=video_phase3_test",
                published_at=datetime.now(timezone.utc),
            )
            owner.add(media)
            owner.flush()
            open_comment = SocialComment(
                workspace_id=workspace.id,
                social_account_id=account.id,
                media_item_id=media.id,
                platform="youtube",
                platform_comment_id="comment_open",
                author_name="A viewer",
                author_username="viewer_a",
                text="How do I get started with this tutorial?",
                workflow_status="open",
                has_owner_reply=False,
                is_owner_comment=False,
                published_at=datetime.now(timezone.utc),
            )
            answered_comment = SocialComment(
                workspace_id=workspace.id,
                social_account_id=account.id,
                media_item_id=media.id,
                platform="youtube",
                platform_comment_id="comment_answered",
                author_name="Another viewer",
                author_username="viewer_b",
                text="Thanks for explaining this!",
                workflow_status="open",
                has_owner_reply=True,
                is_owner_comment=False,
                published_at=datetime.now(timezone.utc),
            )
            owner.add_all([open_comment, answered_comment])
            owner.commit()
            yield {
                "client": client,
                "Session": test_session,
                "workspace_id": workspace.id,
                "account_id": account.id,
                "media_id": media.id,
                "open_comment_id": open_comment.id,
                "answered_comment_id": answered_comment.id,
                "csrf": csrf,
                "redis": fake_redis,
            }
        finally:
            owner.close()
    app.dependency_overrides.clear()
    engine.dispose()
    get_settings.cache_clear()


def test_comment_endpoints_require_owner_session() -> None:
    with TestClient(app) as client:
        assert client.get("/api/v1/comments").status_code == 401
        assert client.get("/api/v1/comments/summary").status_code == 401
        assert client.get("/api/v1/comments/sync-status").status_code == 401


def test_unified_inbox_filters_pagination_and_summary(phase3_client: dict) -> None:
    client: TestClient = phase3_client["client"]
    response = client.get("/api/v1/comments", params={"response_status": "unanswered", "page_size": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["pagination"]["total"] == 1
    assert body["pagination"]["page_size"] == 1
    assert body["items"][0]["platform"] == "youtube"
    assert body["items"][0]["author_username"] == "viewer_a"
    assert "access_token" not in str(body).lower()

    search = client.get("/api/v1/comments", params={"query": "tutorial", "workflow_status": "all"})
    assert search.status_code == 200
    assert search.json()["pagination"]["total"] == 1

    summary = client.get("/api/v1/comments/summary")
    assert summary.status_code == 200
    assert summary.json()["open_comments"] == 2
    assert summary.json()["unanswered_comments"] == 1


def test_comment_workflow_status_needs_csrf_and_is_local(phase3_client: dict) -> None:
    client: TestClient = phase3_client["client"]
    comment_id = str(phase3_client["open_comment_id"])
    blocked = client.patch(f"/api/v1/comments/{comment_id}/status", json={"workflow_status": "resolved"})
    assert blocked.status_code == 403
    response = client.patch(
        f"/api/v1/comments/{comment_id}/status",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={"workflow_status": "resolved"},
    )
    assert response.status_code == 200
    assert response.json()["workflow_status"] == "resolved"
    assert client.get("/api/v1/comments", params={"workflow_status": "resolved"}).json()["pagination"]["total"] == 1


def test_sync_request_queues_job_without_returning_credentials(phase3_client: dict) -> None:
    client: TestClient = phase3_client["client"]
    response = client.post(
        "/api/v1/comments/sync",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={"account_id": str(phase3_client["account_id"])},
    )
    assert response.status_code == 202
    assert response.json()["queued"] == 1
    assert len(phase3_client["redis"].items) == 1
    key, payload = phase3_client["redis"].items[0]
    assert key == comment_jobs.QUEUE_NAME
    assert '"type":"comment_sync"' in payload
    assert "encrypted-test-token" not in payload
    assert "access_token" not in payload

    # An active run is reused rather than adding a second queue message.
    repeated = client.post(
        "/api/v1/comments/sync",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={"account_id": str(phase3_client["account_id"])},
    )
    assert repeated.status_code == 200
    assert repeated.json()["queued"] == 0
    assert len(phase3_client["redis"].items) == 1


def test_youtube_video_url_parser_accepts_watch_short_and_shorts_urls() -> None:
    from app.services.comment_sync import parse_youtube_video_url

    video_id = "dQw4w9WgXcQ"
    assert parse_youtube_video_url(f"https://www.youtube.com/watch?v={video_id}&feature=share") == video_id
    assert parse_youtube_video_url(f"https://youtu.be/{video_id}?si=abc") == video_id
    assert parse_youtube_video_url(f"https://youtube.com/shorts/{video_id}") == video_id
    assert parse_youtube_video_url(f"youtube.com/live/{video_id}") == video_id


def test_youtube_video_url_parser_rejects_non_youtube_urls_and_invalid_ids() -> None:
    from app.services.comment_sync import parse_youtube_video_url

    with pytest.raises(ValueError):
        parse_youtube_video_url("https://evil.example/watch?v=dQw4w9WgXcQ")
    with pytest.raises(ValueError):
        parse_youtube_video_url("https://youtube.com/watch?v=too-short")
    with pytest.raises(ValueError):
        parse_youtube_video_url("https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ")


def test_external_video_import_is_csrf_protected_and_persistently_queued(phase3_client: dict) -> None:
    client: TestClient = phase3_client["client"]
    account_id = str(phase3_client["account_id"])
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    blocked = client.post("/api/v1/comments/external-video", json={"account_id": account_id, "video_url": url})
    assert blocked.status_code == 403

    response = client.post(
        "/api/v1/comments/external-video",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={"account_id": account_id, "video_url": url},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["video_id"] == "dQw4w9WgXcQ"
    assert body["account_id"] == account_id
    assert body["queued"] is True
    assert len(phase3_client["redis"].items) == 1
    _, raw_job = phase3_client["redis"].items[0]
    assert '"target_platform_media_id":"dQw4w9WgXcQ"' in raw_job
    assert "encrypted-test-token" not in raw_job

    run_response = client.get(f"/api/v1/comments/runs/{body['run_id']}")
    assert run_response.status_code == 200
    assert run_response.json()["target_platform_media_id"] == "dQw4w9WgXcQ"


def test_external_video_import_rejects_invalid_url(phase3_client: dict) -> None:
    client: TestClient = phase3_client["client"]
    response = client.post(
        "/api/v1/comments/external-video",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={"account_id": str(phase3_client["account_id"]), "video_url": "https://example.com/video"},
    )
    assert response.status_code == 422
    assert "youtube" in response.json()["detail"].lower()
    assert phase3_client["redis"].items == []


def test_reply_can_publish_from_another_connected_youtube_channel(phase3_client: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import comments as comments_routes
    from app.models.comment import SocialComment
    from app.models.social_account import SocialAccount

    db_session = phase3_client["Session"]()
    try:
        source_comment = db_session.get(SocialComment, phase3_client["open_comment_id"])
        source_account = db_session.get(SocialAccount, phase3_client["account_id"])
        assert source_comment is not None and source_account is not None
        second_account = SocialAccount(
            workspace_id=source_account.workspace_id,
            platform="youtube",
            platform_account_id="UC_second_test_channel",
            display_name="Second connected channel",
            username="secondchannel",
            status="connected",
            scopes=["https://www.googleapis.com/auth/youtube.force-ssl"],
            access_token_encrypted="encrypted-second-test-token",
        )
        db_session.add(second_account)
        db_session.commit()
        second_account_id = str(second_account.id)
    finally:
        db_session.close()

    async def fake_publish(account, *, parent_comment_id: str, text: str, settings=None, db=None):
        assert str(account.id) == second_account_id
        assert parent_comment_id == "comment_open"
        assert text == "Thanks for your question!"
        return {
            "id": "youtube_reply_published_1",
            "snippet": {
                "textOriginal": text,
                "authorDisplayName": "Second connected channel",
                "authorChannelId": {"value": "UC_second_test_channel"},
                "publishedAt": "2026-10-26T10:00:00Z",
            },
        }

    monkeypatch.setattr(comments_routes, "publish_youtube_reply", fake_publish)
    response = phase3_client["client"].post(
        f"/api/v1/comments/{phase3_client['open_comment_id']}/reply",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={"account_id": second_account_id, "text": "Thanks for your question!"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["account_id"] == second_account_id
    assert body["account_name"] == "Second connected channel"
    assert body["status"] == "published"

    db_session = phase3_client["Session"]()
    try:
        source_comment = db_session.get(SocialComment, phase3_client["open_comment_id"])
        assert source_comment is not None
        assert source_comment.has_owner_reply is True
        assert source_comment.reply_count == 1
        published_reply = db_session.scalar(
            select(SocialComment).where(
                SocialComment.social_account_id == __import__("uuid").UUID(second_account_id),
                SocialComment.platform_comment_id == "youtube_reply_published_1",
            )
        )
        assert published_reply is not None
        assert published_reply.is_reply is True
        assert published_reply.is_owner_comment is True
        assert published_reply.text == "Thanks for your question!"
    finally:
        db_session.close()


def _make_bulk_publish_payload(account_ids: list[str], *, key: str = "batch-idempotency-key-0001") -> dict:
    return {
        "video_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "idempotency_key": key,
        "confirm_publish": True,
        "confirm_nonspam": True,
        "comments": [
            {"account_id": account_ids[0], "text": "The example at the end made the workflow much easier to understand."},
            *([{"account_id": account_ids[1], "text": "I appreciate the detailed walkthrough; the explanation was clear."}] if len(account_ids) > 1 else []),
        ],
    }


def test_legacy_multi_channel_comment_publish_is_retired_and_requires_csrf(phase3_client: dict) -> None:
    client: TestClient = phase3_client["client"]
    payload = _make_bulk_publish_payload([str(phase3_client["account_id"]), str(uuid4())])
    blocked = client.post("/api/v1/comments/publish-batches", json=payload)
    assert blocked.status_code == 403

    queued_before = len(phase3_client["redis"].items)
    response = client.post(
        "/api/v1/comments/publish-batches",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json=payload,
    )
    assert response.status_code == 410
    assert "human-approval queue" in response.json()["detail"]
    assert len(phase3_client["redis"].items) == queued_before


def test_legacy_multi_channel_publish_cannot_be_replayed(phase3_client: dict) -> None:
    client: TestClient = phase3_client["client"]
    payload = _make_bulk_publish_payload([str(phase3_client["account_id"]), str(uuid4())])
    headers = {"X-CSRF-Token": phase3_client["csrf"]}
    queued_before = len(phase3_client["redis"].items)

    first = client.post("/api/v1/comments/publish-batches", headers=headers, json=payload)
    repeated = client.post("/api/v1/comments/publish-batches", headers=headers, json=payload)
    assert first.status_code == repeated.status_code == 410
    assert len(phase3_client["redis"].items) == queued_before


def test_legacy_multi_channel_publish_rejects_all_write_attempts(phase3_client: dict) -> None:
    client: TestClient = phase3_client["client"]
    headers = {"X-CSRF-Token": phase3_client["csrf"]}
    payload = _make_bulk_publish_payload([str(phase3_client["account_id"]), str(uuid4())])

    duplicate_payload = {**payload, "comments": [dict(payload["comments"][0]), dict(payload["comments"][0])]}
    duplicate_payload["comments"][1]["account_id"] = str(uuid4())
    duplicate_response = client.post("/api/v1/comments/publish-batches", headers=headers, json=duplicate_payload)
    assert duplicate_response.status_code == 410

    missing_confirmation = _make_bulk_publish_payload([str(phase3_client["account_id"]), str(uuid4())], key="batch-idempotency-key-0002")
    missing_confirmation["confirm_nonspam"] = False
    confirmation_response = client.post("/api/v1/comments/publish-batches", headers=headers, json=missing_confirmation)
    assert confirmation_response.status_code == 410


def test_multi_channel_publish_status_is_workspace_scoped(phase3_client: dict) -> None:
    from app.models.comment_publish_batch import CommentPublishBatch, CommentPublishItem
    from app.services.comment_publishing import request_fingerprint, text_fingerprint

    Session = phase3_client["Session"]
    db = Session()
    try:
        account = db.get(SocialAccount, phase3_client["account_id"])
        assert account is not None
        text = "A unique reviewed comment for scope testing."
        draft = {"account_id": str(account.id), "text": text}
        batch = CommentPublishBatch(
            workspace_id=phase3_client["workspace_id"],
            idempotency_key="internal-status-test-key-0001",
            request_fingerprint=request_fingerprint("abcdefghijk", [draft]),
            target_video_id="abcdefghijk",
            target_video_url="https://www.youtube.com/watch?v=abcdefghijk",
            status="queued",
            total_count=1,
        )
        db.add(batch)
        db.flush()
        db.add(CommentPublishItem(
            batch_id=batch.id,
            workspace_id=phase3_client["workspace_id"],
            social_account_id=account.id,
            target_video_id="abcdefghijk",
            position=1,
            text=text,
            text_fingerprint=text_fingerprint(text),
            status="queued",
        ))
        db.commit()
        batch_id = str(batch.id)
    finally:
        db.close()

    response = phase3_client["client"].get(f"/api/v1/comments/publish-batches/{batch_id}")
    assert response.status_code == 200
    assert response.json()["total_count"] == 1
    assert response.json()["items"][0]["status"] == "queued"


def test_phase4_ai_status_is_authenticated_and_never_returns_provider_secret(phase3_client: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    client: TestClient = phase3_client["client"]
    assert client.get("/api/v1/comments/ai/status").status_code == 200
    monkeypatch.setenv("AI_REPLY_ENABLED", "true")
    monkeypatch.setenv("AI_API_KEY", "phase4-private-test-secret")
    monkeypatch.setenv("AI_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("AI_MODEL", "test-model")
    get_settings.cache_clear()
    response = client.get("/api/v1/comments/ai/status")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["manual_review_required"] is True
    assert body["auto_publish_enabled"] is False
    assert "phase4-private-test-secret" not in response.text
    assert client.get("/api/v1/automation/status").json()["automation_can_publish_automatically"] is False


def test_phase4_ai_draft_api_requires_csrf_and_queues_owner_scoped_draft(phase3_client: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings
    from app.services import ai_drafting

    client: TestClient = phase3_client["client"]
    monkeypatch.setenv("AI_REPLY_ENABLED", "true")
    monkeypatch.setenv("AI_API_KEY", "phase4-private-test-secret")
    monkeypatch.setenv("AI_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("AI_MODEL", "test-model")
    get_settings.cache_clear()
    monkeypatch.setattr(ai_drafting, "get_redis_client", lambda: phase3_client["redis"])
    comment_id = str(phase3_client["open_comment_id"])

    blocked = client.post(f"/api/v1/comments/{comment_id}/ai-drafts", json={"tone": "friendly", "language": "en"})
    assert blocked.status_code == 403
    response = client.post(
        f"/api/v1/comments/{comment_id}/ai-drafts",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={"tone": "friendly", "language": "en", "custom_instructions": "Use a helpful tone"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["comment_id"] == comment_id
    assert "phase4-private-test-secret" not in response.text
    queued = client.get("/api/v1/comments/ai-drafts", params={"comment_ids": comment_id})
    assert queued.status_code == 200
    assert [item["id"] for item in queued.json()["items"]] == [body["id"]]
    redis_payload = phase3_client["redis"].items[0][1]
    assert '"type":"comment_ai_draft"' in redis_payload
    assert "phase4-private-test-secret" not in redis_payload


def test_phase4_automation_rule_creation_is_csrf_protected_and_manual_run_needs_review_confirmation(phase3_client: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings
    from app.services import comment_automation

    client: TestClient = phase3_client["client"]
    rule_payload = {
        "name": "Customer questions",
        "description": "Draft helpful answers",
        "enabled": True,
        "priority": 50,
        "conditions": {
            "platform": "youtube",
            "account_ids": [str(phase3_client["account_id"])],
            "keywords_any": ["how", "pricing"],
            "only_unanswered": True,
            "only_open": True,
            "exclude_owner_comments": True,
        },
        "draft_settings": {"tone": "helpful", "language": "en", "custom_instructions": "Be concise"},
    }
    blocked = client.post("/api/v1/automation/rules", json=rule_payload)
    assert blocked.status_code == 403
    created = client.post("/api/v1/automation/rules", headers={"X-CSRF-Token": phase3_client["csrf"]}, json=rule_payload)
    assert created.status_code == 201
    rule = created.json()
    assert rule["action"] == "create_ai_draft_for_manual_review"
    assert rule["conditions"]["platform"] == "youtube"

    monkeypatch.setenv("AI_REPLY_ENABLED", "true")
    monkeypatch.setenv("AI_API_KEY", "phase4-private-test-secret")
    monkeypatch.setenv("AI_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("AI_MODEL", "test-model")
    get_settings.cache_clear()
    monkeypatch.setattr(comment_automation, "get_redis_client", lambda: phase3_client["redis"])

    unconfirmed = client.post(
        f"/api/v1/automation/rules/{rule['id']}/run",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={"confirm_draft_only": False},
    )
    assert unconfirmed.status_code == 422
    queued = client.post(
        f"/api/v1/automation/rules/{rule['id']}/run",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={"confirm_draft_only": True},
    )
    assert queued.status_code == 202
    assert queued.json()["status"] == "queued"
    job_json = phase3_client["redis"].items[0][1]
    assert '"type":"comment_automation_run"' in job_json
    assert "phase4-private-test-secret" not in job_json


def test_phase4_rule_account_filters_cannot_reference_another_workspace(phase3_client: dict) -> None:
    client: TestClient = phase3_client["client"]
    payload = {
        "name": "Invalid account filter",
        "conditions": {"account_ids": [str(uuid4())]},
        "draft_settings": {"tone": "helpful", "language": "en"},
    }
    response = client.post(
        "/api/v1/automation/rules",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json=payload,
    )
    assert response.status_code == 404
    assert "workspace" in response.json()["detail"].lower()


def _post_human_approved_youtube_draft(
    phase3_client: dict,
    monkeypatch: pytest.MonkeyPatch,
    *,
    video_id: str = "abcdefghijk",
    idempotency_key: str | None = None,
    comment_text: str = "The row-locking example makes the duplicate-job prevention mechanism concrete.",
    expected_status: int = 201,
) -> dict:
    from app.api.routes import youtube_comment_queue as queue_routes

    async def fake_video_details(account, *, video_id, settings, db):
        return {
            "channel_id": "UC_target_video_owner",
            "title": "PostgreSQL row locks and duplicate webhook jobs",
            "description": "This tutorial demonstrates how row locks keep concurrent webhook workers from creating duplicate jobs, with a short transaction example.",
            "thumbnail_url": None,
            "published_at": None,
        }

    monkeypatch.setattr(queue_routes, "get_youtube_video_details_for_comment_publish", fake_video_details)
    payload = {
        "account_id": str(phase3_client["account_id"]),
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "comment_text": comment_text,
        "idempotency_key": idempotency_key or f"human-comment-{uuid4()}",
        "confirm_one_channel_one_video": True,
    }
    response = phase3_client["client"].post(
        "/api/v1/comments/youtube-queue",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json=payload,
    )
    assert response.status_code == expected_status, response.text
    return response.json()


def _finish_contextual_draft_generation(
    phase3_client: dict,
    monkeypatch: pytest.MonkeyPatch,
    queue_item_id: str,
) -> None:
    """Compatibility helper: manual text is Pending Approval immediately; no AI worker runs."""
    from app.models.youtube_comment_queue import YouTubeCommentQueueItem
    with phase3_client["Session"]() as db:
        item = db.get(YouTubeCommentQueueItem, uuid4_from(queue_item_id))
        assert item is not None
        assert item.status == "pending_approval"
        assert item.draft_source == "human"
        assert item.generated_at is None


def uuid4_from(value: str):
    from uuid import UUID
    return UUID(value)


def test_human_queue_saves_only_user_written_text_and_reserves_one_video(phase3_client: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.models.youtube_comment_queue import YouTubeCommentQueueItem
    from app.services import youtube_comment_queue as queue_service

    published_calls: list[str] = []
    async def forbidden_publish(*args, **kwargs):
        published_calls.append("unexpected")
        raise AssertionError("saving human text must not call YouTube write APIs")

    monkeypatch.setattr(queue_service, "publish_youtube_top_level_comment", forbidden_publish)
    human_text = "The row-locking example makes the duplicate-job prevention mechanism concrete."
    first = _post_human_approved_youtube_draft(phase3_client, monkeypatch, comment_text=human_text)
    assert first["status"] == "pending_approval"
    assert first["draft_source"] == "human"
    assert first["human_authored"] is True
    assert first["draft_text"] == human_text
    assert first["video_title"] == "PostgreSQL row locks and duplicate webhook jobs"
    assert first["video_description"].startswith("This tutorial demonstrates")
    assert first["generated_at"] is None

    duplicate = _post_human_approved_youtube_draft(
        phase3_client, monkeypatch, idempotency_key=f"different-key-{uuid4()}", expected_status=409
    )
    assert "already assigned" in duplicate["detail"]
    with phase3_client["Session"]() as db:
        items = db.query(YouTubeCommentQueueItem).filter_by(workspace_id=phase3_client["workspace_id"], target_video_id="abcdefghijk").all()
        assert len(items) == 1
        item = db.get(YouTubeCommentQueueItem, uuid4_from(first["id"]))
        assert item is not None and item.status == "pending_approval"
        assert item.draft_source == "human"
        assert item.draft_text == human_text
        assert item.quality_warnings == []
    assert published_calls == []


def test_human_queue_works_when_ai_drafting_is_disabled(phase3_client: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("AI_REPLY_ENABLED", "false")
    monkeypatch.setenv("AI_API_KEY", "")
    get_settings.cache_clear()
    try:
        result = _post_human_approved_youtube_draft(
            phase3_client,
            monkeypatch,
            video_id="lmnopqrstuv",
            comment_text="The demo's transaction boundary helps clarify how duplicate jobs are prevented.",
        )
        assert result["status"] == "pending_approval"
        assert result["draft_source"] == "human"
        assert result["draft_text"] == "The demo's transaction boundary helps clarify how duplicate jobs are prevented."
    finally:
        get_settings.cache_clear()


def test_legacy_ai_generation_endpoint_is_permanently_disabled(phase3_client: dict) -> None:
    response = phase3_client["client"].post(
        "/api/v1/comments/youtube-queue/generate",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={},
    )
    assert response.status_code == 410
    assert "manual" in response.json()["detail"].lower() or "human" in response.json()["detail"].lower()


def test_legacy_ai_queue_item_cannot_be_approved(phase3_client: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timedelta, timezone
    from app.models.youtube_comment_queue import YouTubeCommentQueueItem

    item_response = _post_human_approved_youtube_draft(phase3_client, monkeypatch, video_id="abcdefghijk")
    with phase3_client["Session"]() as db:
        item = db.get(YouTubeCommentQueueItem, uuid4_from(item_response["id"]))
        assert item is not None
        item.draft_source = "legacy_ai"
        db.commit()

    response = phase3_client["client"].post(
        f"/api/v1/comments/youtube-queue/{item_response['id']}/approve",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={
            "approved_text": "The row-locking example makes the duplicate-job prevention mechanism concrete.",
            "scheduled_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "confirm_target_and_channel": True,
            "confirm_relevance_and_nonspam": True,
        },
    )
    assert response.status_code == 409
    assert "not authored manually" in response.json()["detail"]


def test_comment_text_fingerprint_ignores_cosmetic_punctuation_and_case() -> None:
    from app.api.routes.youtube_comment_queue import _normalized_text_fingerprint

    first = _normalized_text_fingerprint("This example explains row-locking clearly!")
    cosmetic_variant = _normalized_text_fingerprint(" THIS example explains row locking clearly ")
    assert first == cosmetic_variant


def test_human_approval_requires_explicit_confirmations_and_user_selected_time(phase3_client: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timedelta, timezone
    from app.models.youtube_comment_queue import YouTubeCommentQueueItem

    draft = _post_human_approved_youtube_draft(phase3_client, monkeypatch, video_id="lmnopqrstuv")
    _finish_contextual_draft_generation(phase3_client, monkeypatch, draft["id"])
    client = phase3_client["client"]
    headers = {"X-CSRF-Token": phase3_client["csrf"]}
    queue_item_id = draft["id"]
    too_generic = client.post(
        f"/api/v1/comments/youtube-queue/{queue_item_id}/approve",
        headers=headers,
        json={
            "approved_text": "Great video!",
            "scheduled_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "confirm_target_and_channel": True,
            "confirm_relevance_and_nonspam": True,
        },
    )
    assert too_generic.status_code == 422

    missing_confirmation = client.post(
        f"/api/v1/comments/youtube-queue/{queue_item_id}/approve",
        headers=headers,
        json={
            "approved_text": "The row-locking example makes the duplicate-job prevention mechanism concrete.",
            "scheduled_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "confirm_target_and_channel": True,
            "confirm_relevance_and_nonspam": False,
        },
    )
    assert missing_confirmation.status_code == 422

    schedule = datetime.now(timezone.utc) + timedelta(minutes=7)
    approved = client.post(
        f"/api/v1/comments/youtube-queue/{queue_item_id}/approve",
        headers=headers,
        json={
            "approved_text": "The row-locking example makes the duplicate-job prevention mechanism concrete.",
            "scheduled_at": schedule.isoformat(),
            "confirm_target_and_channel": True,
            "confirm_relevance_and_nonspam": True,
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "scheduled"
    assert approved.json()["approved_by_email"] == "owner@example.com"
    assert approved.json()["scheduled_at"] is not None
    with phase3_client["Session"]() as db:
        item = db.get(YouTubeCommentQueueItem, uuid4_from(queue_item_id))
        assert item is not None and item.status == "scheduled"
        assert item.approved_text == "The row-locking example makes the duplicate-job prevention mechanism concrete."
        assert item.scheduled_at is not None


def test_admin_can_resolve_unknown_provider_outcome_after_manual_verification(phase3_client: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.models.audit_event import AuditEvent
    from app.models.youtube_comment_queue import YouTubeCommentQueueItem

    draft = _post_human_approved_youtube_draft(phase3_client, monkeypatch, video_id="abcdefghijk")
    with phase3_client["Session"]() as db:
        item = db.get(YouTubeCommentQueueItem, uuid4_from(draft["id"]))
        assert item is not None
        item.status = "outcome_unknown"
        item.error_code = "provider_outcome_unknown"
        db.commit()

    client = phase3_client["client"]
    headers = {"X-CSRF-Token": phase3_client["csrf"]}
    unresolved = client.post(
        f"/api/v1/comments/youtube-queue/{draft['id']}/resolve-outcome",
        headers=headers,
        json={"outcome": "not_posted", "confirm_checked_youtube": False, "resolution_note": "Checked the YouTube page; comment was not present."},
    )
    assert unresolved.status_code == 422

    resolved = client.post(
        f"/api/v1/comments/youtube-queue/{draft['id']}/resolve-outcome",
        headers=headers,
        json={"outcome": "not_posted", "confirm_checked_youtube": True, "resolution_note": "Checked the YouTube page; comment was not present."},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "failed"
    assert resolved.json()["error_code"] == "manual_verified_not_posted"
    with phase3_client["Session"]() as db:
        events = db.query(AuditEvent).filter_by(workspace_id=phase3_client["workspace_id"], action="youtube_comment_outcome_resolved").all()
        assert len(events) == 1
        assert events[0].details["outcome"] == "not_posted"

    # The reservation is released only after the explicit admin resolution.
    second = _post_human_approved_youtube_draft(
        phase3_client, monkeypatch, video_id="lmnopqrstuv", idempotency_key=f"new-after-resolution-{uuid4()}"
    )
    assert second["status"] == "pending_approval"


def test_marking_unknown_outcome_posted_requires_comment_id(phase3_client: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = _post_human_approved_youtube_draft(phase3_client, monkeypatch, video_id="abcdefghijk")
    with phase3_client["Session"]() as db:
        from app.models.youtube_comment_queue import YouTubeCommentQueueItem
        item = db.get(YouTubeCommentQueueItem, uuid4_from(draft["id"]))
        assert item is not None
        item.status = "outcome_unknown"
        db.commit()
    response = phase3_client["client"].post(
        f"/api/v1/comments/youtube-queue/{draft['id']}/resolve-outcome",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={"outcome": "posted", "confirm_checked_youtube": True, "resolution_note": "Found the published comment in YouTube Studio."},
    )
    assert response.status_code == 422


def test_due_worker_pauses_all_scheduled_writes_after_quota_error(phase3_client: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timedelta, timezone
    from app.models.youtube_comment_queue import YouTubeCommentQueueItem, YouTubeCommentQueueState
    from app.services import youtube_comment_queue as queue_service
    from app.services.comment_sync import ProviderSyncError

    draft = _post_human_approved_youtube_draft(phase3_client, monkeypatch, video_id="mnopqrstuvw")
    _finish_contextual_draft_generation(phase3_client, monkeypatch, draft["id"])
    schedule = datetime.now(timezone.utc) + timedelta(minutes=5)
    approved = phase3_client["client"].post(
        f"/api/v1/comments/youtube-queue/{draft['id']}/approve",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={
            "approved_text": "The row-locking example makes the duplicate-job prevention mechanism concrete.",
            "scheduled_at": schedule.isoformat(),
            "confirm_target_and_channel": True,
            "confirm_relevance_and_nonspam": True,
        },
    )
    assert approved.status_code == 200, approved.text

    provider_calls: list[str] = []
    async def quota_failure(account, *, video_id, video_channel_id, text, settings, db):
        provider_calls.append(video_id)
        raise ProviderSyncError("provider_quota_exceeded", retryable=False)

    monkeypatch.setattr(queue_service, "publish_youtube_top_level_comment", quota_failure)
    with phase3_client["Session"]() as db:
        processed = queue_service.process_due_scheduled_youtube_comments(db, now=schedule + timedelta(minutes=1), limit=5)
        assert processed == 1
        item = db.get(YouTubeCommentQueueItem, uuid4_from(draft["id"]))
        assert item is not None and item.status == "blocked_quota"
        state = db.get(YouTubeCommentQueueState, 1)
        assert state is not None and state.paused_at is not None
        assert queue_service.process_due_scheduled_youtube_comments(db, now=schedule + timedelta(minutes=2), limit=5) == 0
        assert provider_calls == ["mnopqrstuvw"]


def test_due_worker_publishes_only_the_exact_approved_text_once(phase3_client: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timedelta, timezone
    from app.models.comment import SocialComment
    from app.models.youtube_comment_queue import YouTubeCommentQueueItem
    from app.services import youtube_comment_queue as queue_service

    draft = _post_human_approved_youtube_draft(phase3_client, monkeypatch, video_id="uvwxyz12345")
    _finish_contextual_draft_generation(phase3_client, monkeypatch, draft["id"])
    approved_text = "The row-locking example makes the duplicate-job prevention mechanism concrete."
    schedule = datetime.now(timezone.utc) + timedelta(minutes=5)
    response = phase3_client["client"].post(
        f"/api/v1/comments/youtube-queue/{draft['id']}/approve",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={
            "approved_text": approved_text,
            "scheduled_at": schedule.isoformat(),
            "confirm_target_and_channel": True,
            "confirm_relevance_and_nonspam": True,
        },
    )
    assert response.status_code == 200, response.text

    calls: list[dict] = []
    async def fake_publish(account, *, video_id, video_channel_id, text, settings, db):
        calls.append({"account_id": str(account.id), "video_id": video_id, "video_channel_id": video_channel_id, "text": text})
        return {
            "snippet": {"topLevelComment": {"id": "comment_test_published_1", "snippet": {
                "textOriginal": text,
                "authorDisplayName": account.display_name,
                "authorChannelId": {"value": account.platform_account_id},
                "publishedAt": datetime.now(timezone.utc).isoformat(),
            }}}
        }

    monkeypatch.setattr(queue_service, "publish_youtube_top_level_comment", fake_publish)
    with phase3_client["Session"]() as db:
        assert queue_service.process_due_scheduled_youtube_comments(db, now=schedule + timedelta(minutes=1), limit=10) == 1
        item = db.get(YouTubeCommentQueueItem, uuid4_from(draft["id"]))
        assert item is not None and item.status == "posted"
        assert item.approved_text == approved_text
        assert item.provider_comment_id == "comment_test_published_1"
        comments = db.query(SocialComment).filter_by(workspace_id=phase3_client["workspace_id"], platform_comment_id="comment_test_published_1").all()
        assert len(comments) == 1
        assert comments[0].text == approved_text
        # A later polling tick cannot send the completed write again.
        assert queue_service.process_due_scheduled_youtube_comments(db, now=schedule + timedelta(minutes=2), limit=10) == 0
    assert calls == [{
        "account_id": str(phase3_client["account_id"]),
        "video_id": "uvwxyz12345",
        "video_channel_id": "UC_target_video_owner",
        "text": approved_text,
    }]


def test_worker_refuses_to_publish_legacy_ai_queue_items(phase3_client: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timedelta, timezone
    from app.models.youtube_comment_queue import YouTubeCommentQueueItem
    from app.services import youtube_comment_queue as queue_service

    created = _post_human_approved_youtube_draft(phase3_client, monkeypatch, video_id="lmnopqrstuv")
    with phase3_client["Session"]() as db:
        item = db.get(YouTubeCommentQueueItem, uuid4_from(created["id"]))
        assert item is not None
        item.draft_source = "legacy_ai"
        item.status = "scheduled"
        item.approved_text = "This older AI text must never be published by the human queue."
        item.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

    calls: list[str] = []
    async def forbidden_publish(account, *, video_id, video_channel_id, text, settings, db):
        calls.append(video_id)
        raise AssertionError("legacy AI text must be blocked before the YouTube write")

    monkeypatch.setattr(queue_service, "publish_youtube_top_level_comment", forbidden_publish)
    with phase3_client["Session"]() as db:
        processed = queue_service.process_due_scheduled_youtube_comments(db, now=datetime.now(timezone.utc), limit=5)
        item = db.get(YouTubeCommentQueueItem, uuid4_from(created["id"]))
        assert processed == 1
        assert item is not None and item.status == "failed"
        assert item.error_code == "human_comment_required"
    assert calls == []


def test_unknown_provider_write_outcome_is_never_retried(phase3_client: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timedelta, timezone
    from app.models.youtube_comment_queue import YouTubeCommentQueueItem
    from app.services import youtube_comment_queue as queue_service
    from app.services.comment_sync import ProviderSyncError

    draft = _post_human_approved_youtube_draft(phase3_client, monkeypatch, video_id="klmnopqrstu")
    _finish_contextual_draft_generation(phase3_client, monkeypatch, draft["id"])
    schedule = datetime.now(timezone.utc) + timedelta(minutes=5)
    approval = phase3_client["client"].post(
        f"/api/v1/comments/youtube-queue/{draft['id']}/approve",
        headers={"X-CSRF-Token": phase3_client["csrf"]},
        json={
            "approved_text": "The row-locking example makes the duplicate-job prevention mechanism concrete.",
            "scheduled_at": schedule.isoformat(),
            "confirm_target_and_channel": True,
            "confirm_relevance_and_nonspam": True,
        },
    )
    assert approval.status_code == 200, approval.text

    calls: list[str] = []
    async def unknown_outcome(account, *, video_id, video_channel_id, text, settings, db):
        calls.append(video_id)
        raise ProviderSyncError("provider_outcome_unknown", retryable=False, outcome_unknown=True)

    monkeypatch.setattr(queue_service, "publish_youtube_top_level_comment", unknown_outcome)
    with phase3_client["Session"]() as db:
        assert queue_service.process_due_scheduled_youtube_comments(db, now=schedule + timedelta(minutes=1), limit=1) == 1
        item = db.get(YouTubeCommentQueueItem, uuid4_from(draft["id"]))
        assert item is not None and item.status == "outcome_unknown"
        assert item.error_code == "provider_outcome_unknown"
        assert queue_service.process_due_scheduled_youtube_comments(db, now=schedule + timedelta(minutes=2), limit=1) == 0
        assert calls == ["klmnopqrstu"]
