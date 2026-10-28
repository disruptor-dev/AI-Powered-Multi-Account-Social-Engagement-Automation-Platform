from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from cryptography.fernet import Fernet
import pytest
import asyncio

from app.core.config import Settings, get_settings
from app.models.base import Base
from app.models.comment import SocialComment
from app.models.comment_sync_run import CommentSyncRun
from app.models.media_item import MediaItem
from app.models.social_account import SocialAccount
from app.models.workspace import Workspace
from app.services.comment_sync import _persist_snapshot, _youtube_external_video_snapshot, parse_provider_datetime


def test_provider_datetime_normalizes_utc() -> None:
    parsed = parse_provider_datetime("2026-10-26T08:05:02Z")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    assert parse_provider_datetime("not a timestamp") is None
    assert parse_provider_datetime(None) is None


def test_snapshot_upsert_is_deduplicated_and_preserves_local_workflow_state() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Test workspace")
        db.add(workspace)
        db.flush()
        account = SocialAccount(
            workspace_id=workspace.id,
            platform="youtube",
            platform_account_id="UC_test",
            display_name="Test channel",
            status="connected",
            access_token_encrypted="encrypted",
            scopes=["https://www.googleapis.com/auth/youtube.force-ssl"],
        )
        db.add(account)
        db.flush()
        published = datetime(2026, 10, 25, tzinfo=timezone.utc)
        snapshot = [{
            "platform_media_id": "video_one",
            "media_type": "video",
            "title": "Testing",
            "permalink": "https://www.youtube.com/watch?v=video_one",
            "published_at": published,
            "raw_metadata": {},
            "comments": [{
                "platform_comment_id": "thread_1",
                "parent_platform_comment_id": None,
                "author_id": "UC_viewer",
                "author_name": "Viewer",
                "author_username": None,
                "author_avatar_url": None,
                "text": "Original comment",
                "like_count": 1,
                "reply_count": 0,
                "published_at": published,
                "platform_updated_at": published,
                "is_reply": False,
                "is_owner_comment": False,
                "has_owner_reply": False,
                "raw_metadata": {},
            }],
        }]
        first = _persist_snapshot(db, account, snapshot)
        db.commit()
        assert first.media_processed == 1
        assert first.comments_inserted == 1

        stored = db.scalar(select(SocialComment).where(SocialComment.platform_comment_id == "thread_1"))
        assert stored is not None
        stored.workflow_status = "resolved"
        db.commit()
        snapshot[0]["comments"][0]["text"] = "Updated comment text"
        second = _persist_snapshot(db, account, snapshot)
        db.commit()
        assert second.comments_inserted == 0
        assert second.comments_updated == 1
        records = db.scalars(select(SocialComment).where(SocialComment.social_account_id == account.id)).all()
        assert len(records) == 1
        assert records[0].text == "Updated comment text"
        assert records[0].workflow_status == "resolved"
        assert db.scalar(select(MediaItem).where(MediaItem.social_account_id == account.id)) is not None
    engine.dispose()


def test_only_one_queued_or_running_sync_per_account() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Queue test workspace")
        db.add(workspace)
        db.flush()
        account = SocialAccount(
            workspace_id=workspace.id,
            platform="youtube",
            platform_account_id="UC_queue_test",
            display_name="Queue test channel",
            status="connected",
        )
        db.add(account)
        db.flush()
        first = CommentSyncRun(
            workspace_id=workspace.id,
            social_account_id=account.id,
            job_key="phase3-active-one",
            trigger="manual",
            status="queued",
        )
        db.add(first)
        db.commit()

        duplicate = CommentSyncRun(
            workspace_id=workspace.id,
            social_account_id=account.id,
            job_key="phase3-active-two",
            trigger="manual",
            status="running",
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        first = db.get(CommentSyncRun, first.id)
        assert first is not None
        first.status = "success"
        db.commit()
        db.add(CommentSyncRun(
            workspace_id=workspace.id,
            social_account_id=account.id,
            job_key="phase3-active-three",
            trigger="scheduled",
            status="queued",
        ))
        db.commit()
    engine.dispose()


def test_external_video_snapshot_includes_public_thread_and_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import comment_sync

    account = SocialAccount(
        platform="youtube", platform_account_id="UC_connected_channel", display_name="Connected channel",
        status="connected", scopes=["https://www.googleapis.com/auth/youtube.force-ssl"],
    )

    async def fake_provider_get_json(client, url, *, provider, token, params=None, cursor_url=False):
        assert provider == "youtube"
        assert token == "test-token"
        assert url.endswith("/videos")
        assert params["id"] == "dQw4w9WgXcQ"
        return {"items": [{"snippet": {"title": "External demo", "description": "Public video", "publishedAt": "2026-10-01T00:00:00Z", "thumbnails": {"default": {"url": "https://img.example/thumb.jpg"}}}}]}

    async def fake_paginate_youtube(client, token, path, params, *, max_pages, max_items):
        if path == "commentThreads":
            return [{"snippet": {"canReply": True, "totalReplyCount": 1, "topLevelComment": {
                "id": "top_comment_id", "snippet": {"authorDisplayName": "Public viewer", "textOriginal": "Does this work?", "authorChannelId": {"value": "UC_viewer"}, "publishedAt": "2026-10-02T00:00:00Z"}
            }}, "replies": {"comments": [{"id": "reply_comment_id", "snippet": {"authorDisplayName": "Other viewer", "textOriginal": "Yes", "publishedAt": "2026-10-02T01:00:00Z"}}]}}]
        return []

    monkeypatch.setattr(comment_sync, "_provider_get_json", fake_provider_get_json)
    monkeypatch.setattr(comment_sync, "_paginate_youtube", fake_paginate_youtube)
    settings = Settings(comment_sync_max_comments_per_media=10, comment_sync_max_replies_per_media=10, comment_sync_max_pages_per_edge=5)
    snapshots, warnings = asyncio.run(_youtube_external_video_snapshot(object(), "test-token", account, settings, "dQw4w9WgXcQ"))

    assert warnings == []
    assert len(snapshots) == 1
    assert snapshots[0]["source_type"] == "external_video"
    assert snapshots[0]["title"] == "External demo"
    assert snapshots[0]["permalink"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    comments = snapshots[0]["comments"]
    assert [comment["platform_comment_id"] for comment in comments] == ["top_comment_id", "reply_comment_id"]
    assert comments[0]["raw_metadata"]["can_reply"] is True
    assert comments[1]["parent_platform_comment_id"] == "top_comment_id"
