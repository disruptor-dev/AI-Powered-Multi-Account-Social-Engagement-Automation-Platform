from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.security import encrypt_secret
from app.models.base import Base
from app.models.comment import SocialComment
from app.models.comment_publish_batch import CommentPublishBatch, CommentPublishItem
from app.models.media_item import MediaItem
from app.models.social_account import SocialAccount
from app.models.workspace import Workspace
from app.services import comment_sync, comment_publishing
from app.services.comment_sync import ProviderSyncError, publish_youtube_top_level_comment


class FakeHttpResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeAsyncClient:
    def __init__(self, response: FakeHttpResponse | None = None, error: Exception | None = None, calls: list[dict] | None = None, **_: Any) -> None:
        self.response = response
        self.error = error
        self.calls = calls if calls is not None else []

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        self.calls.append({"url": url, **kwargs})
        if self.error:
            raise self.error
        assert self.response is not None
        return self.response


def test_top_level_publish_uses_comment_threads_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode()
    settings = Settings(token_encryption_key=key, comment_sync_request_timeout_seconds=12)
    account = SocialAccount(
        workspace_id=uuid.uuid4(), platform="youtube", platform_account_id="UC_test_channel",
        display_name="Channel A", status="connected",
        scopes=["https://www.googleapis.com/auth/youtube.force-ssl"],
        access_token_encrypted=encrypt_secret("test-access-token", settings),
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    calls: list[dict] = []
    resource = {"id": "thread_123", "snippet": {"videoId": "abcdefghijk", "topLevelComment": {"id": "comment_123", "snippet": {"textOriginal": "A useful, distinct comment"}}}}
    monkeypatch.setattr(comment_sync.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(FakeHttpResponse(200, resource), calls=calls, **kwargs))

    result = asyncio.run(publish_youtube_top_level_comment(
        account, video_id="abcdefghijk", video_channel_id="UC_video_owner",
        text="A useful, distinct comment", settings=settings,
    ))

    assert result["id"] == "thread_123"
    call = calls[0]
    assert call["url"].endswith("/commentThreads")
    assert call["params"] == {"part": "snippet"}
    assert call["headers"]["Authorization"] == "Bearer test-access-token"
    assert call["json"] == {
        "snippet": {
            "channelId": "UC_video_owner",
            "videoId": "abcdefghijk",
            "topLevelComment": {"snippet": {"textOriginal": "A useful, distinct comment"}},
        }
    }


def test_top_level_publish_network_failure_is_marked_outcome_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode()
    settings = Settings(token_encryption_key=key, comment_sync_request_timeout_seconds=12)
    account = SocialAccount(
        workspace_id=uuid.uuid4(), platform="youtube", platform_account_id="UC_test_channel",
        display_name="Channel A", status="connected",
        scopes=["https://www.googleapis.com/auth/youtube.force-ssl"],
        access_token_encrypted=encrypt_secret("test-access-token", settings),
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    monkeypatch.setattr(comment_sync.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(error=httpx.ReadTimeout("connection dropped"), **kwargs))
    with pytest.raises(ProviderSyncError) as caught:
        asyncio.run(publish_youtube_top_level_comment(
            account, video_id="abcdefghijk", video_channel_id="UC_video_owner",
            text="A useful, distinct comment", settings=settings,
        ))
    assert caught.value.code == "provider_outcome_unknown"
    assert caught.value.outcome_unknown is True


def _publish_test_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, class_=Session, autoflush=False, autocommit=False)


def _make_batch(db: Session, *, count: int = 2) -> tuple[CommentPublishBatch, list[SocialAccount]]:
    workspace = Workspace(name="Publish test workspace")
    db.add(workspace)
    db.flush()
    accounts: list[SocialAccount] = []
    for n in range(count):
        account = SocialAccount(
            workspace_id=workspace.id, platform="youtube", platform_account_id=f"UC_publish_test_{n}",
            display_name=f"Publish channel {n + 1}", status="connected",
            scopes=["https://www.googleapis.com/auth/youtube.force-ssl"],
            access_token_encrypted="encrypted-test-token",
            access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(account)
        accounts.append(account)
    db.flush()
    batch = CommentPublishBatch(
        workspace_id=workspace.id, idempotency_key=f"worker-test-{uuid.uuid4()}",
        request_fingerprint="f" * 64, target_video_id="abcdefghijk",
        target_video_url="https://www.youtube.com/watch?v=abcdefghijk", status="queued", total_count=count,
    )
    db.add(batch)
    db.flush()
    for i, account in enumerate(accounts):
        text = f"A distinct comment number {i + 1}."
        db.add(CommentPublishItem(
            batch_id=batch.id, workspace_id=workspace.id, social_account_id=account.id,
            target_video_id="abcdefghijk", position=i + 1, text=text,
            text_fingerprint=comment_publishing.text_fingerprint(text), status="queued",
        ))
    db.commit()
    db.refresh(batch)
    return batch, accounts


def test_legacy_worker_retires_multichannel_batch_without_public_writes() -> None:
    engine, TestSession = _publish_test_db()
    try:
        with TestSession() as db:
            batch, _accounts = _make_batch(db)
            batch_id, workspace_id = batch.id, batch.workspace_id

            # The legacy processor no longer imports or calls a provider write helper.
            comment_publishing.process_comment_publish_batch(db, batch_id, workspace_id)
            db.refresh(batch)
            items = db.scalars(select(CommentPublishItem).where(CommentPublishItem.batch_id == batch_id)).all()
            assert batch.status == "error"
            assert batch.error_code == "legacy_multi_channel_publishing_disabled"
            assert all(item.status == "failed" for item in items)
            assert all(item.error_code == "legacy_multi_channel_publishing_disabled" for item in items)
            assert db.scalars(select(SocialComment)).all() == []

            # Duplicate Redis delivery remains a no-op.
            comment_publishing.process_comment_publish_batch(db, batch_id, workspace_id)
            assert all(item.status == "failed" for item in items)
    finally:
        engine.dispose()


def test_legacy_worker_marks_inflight_outcomes_unknown_without_retry() -> None:
    engine, TestSession = _publish_test_db()
    try:
        with TestSession() as db:
            batch, _accounts = _make_batch(db)
            items = db.scalars(select(CommentPublishItem).where(CommentPublishItem.batch_id == batch.id).order_by(CommentPublishItem.position)).all()
            items[0].status = "publishing"
            items[1].status = "queued"
            batch.status = "running"
            db.commit()
            batch_id, workspace_id = batch.id, batch.workspace_id

            comment_publishing.process_comment_publish_batch(db, batch_id, workspace_id)
            db.refresh(batch)
            items = db.scalars(select(CommentPublishItem).where(CommentPublishItem.batch_id == batch_id).order_by(CommentPublishItem.position)).all()
            assert batch.status == "partial"
            assert batch.unknown_count == 1
            assert batch.failed_count == 1
            assert [item.status for item in items] == ["unknown", "failed"]
            assert items[0].error_code == "provider_outcome_unknown"
            assert items[1].error_code == "legacy_multi_channel_publishing_disabled"
    finally:
        engine.dispose()


def test_youtube_quota_403_is_distinguished_from_other_forbidden_responses() -> None:
    assert comment_sync._youtube_error_code(
        {"error": {"errors": [{"reason": "quotaExceeded"}]}}, 403,
    ) == ("provider_quota_exceeded", False, True)
    assert comment_sync._youtube_error_code(
        {"error": {"errors": [{"reason": "forbidden"}]}}, 403,
    ) == ("provider_permission_denied", False, False)
