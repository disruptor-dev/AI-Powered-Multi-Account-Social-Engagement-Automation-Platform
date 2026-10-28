from __future__ import annotations

from collections.abc import Generator
import asyncio
from datetime import date, datetime, timedelta, timezone
import csv
import io
import uuid

import pytest
from argon2 import PasswordHasher
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import create_session_token
from app.main import app
from app.models.base import Base
from app.models.analytics_snapshot import AnalyticsSnapshot
from app.models.analytics_sync_run import AnalyticsSyncRun
from app.models.ai_reply_draft import AIReplyDraft
from app.models.comment import SocialComment
from app.models.media_item import MediaItem
from app.models.social_account import SocialAccount
from app.models.workspace import Workspace
from app.services.analytics import _parse_instagram_metric, _parse_youtube_report, _sync_youtube_media_snapshots, resolve_period

OWNER_PASSWORD = "Phase8-Analytics-Test-Password!"


@pytest.fixture
def analytics_client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    monkeypatch.setenv("APP_ADMIN_EMAIL", "analytics-owner@example.com")
    monkeypatch.setenv("APP_ADMIN_PASSWORD_HASH", PasswordHasher().hash(OWNER_PASSWORD))
    monkeypatch.setenv("SESSION_SECRET_KEY", "phase-eight-test-session-secret-longer-than-32")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AI_REPLY_ENABLED", "false")
    get_settings.cache_clear()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, class_=Session, autoflush=False, autocommit=False)

    def override_get_db() -> Generator[Session, None, None]:
        db = session_factory()
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


def _login(client: TestClient) -> dict:
    response = client.post("/api/v1/auth/login", headers={"X-CSRF-Token": _csrf(client)}, json={"email": "analytics-owner@example.com", "password": OWNER_PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def _seed(client: TestClient, *, scope: bool = True, display_name: str = "Analytics channel") -> tuple[str, str]:
    session = _login(client)
    workspace_id = uuid.UUID(session["workspace_id"])
    override = app.dependency_overrides[get_db]
    generator = override()
    db = next(generator)
    try:
        account = SocialAccount(
            workspace_id=workspace_id, platform="youtube", platform_account_id=f"yt-{uuid.uuid4().hex[:8]}",
            display_name=display_name, username="analytics", status="connected",
            access_token_encrypted="unused-in-api-tests", refresh_token_encrypted=None,
            scopes=["https://www.googleapis.com/auth/youtube.force-ssl", "https://www.googleapis.com/auth/yt-analytics.readonly"] if scope else ["https://www.googleapis.com/auth/youtube.force-ssl"],
        )
        db.add(account)
        db.flush()
        media = MediaItem(workspace_id=workspace_id, social_account_id=account.id, platform="youtube", platform_media_id=f"video-{uuid.uuid4().hex[:8]}", media_type="video", title="Test video")
        db.add(media)
        db.flush()
        root = SocialComment(
            workspace_id=workspace_id, social_account_id=account.id, media_item_id=media.id, platform="youtube",
            platform_comment_id=f"root-{uuid.uuid4().hex[:8]}", author_name="Viewer", text="A public comment", like_count=4,
            reply_count=1, published_at=datetime.now(timezone.utc) - timedelta(days=2), is_reply=False, is_owner_comment=False,
            has_owner_reply=True, workflow_status="resolved", resolved_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db.add(root)
        db.flush()
        reply = SocialComment(
            workspace_id=workspace_id, social_account_id=account.id, media_item_id=media.id, parent_comment_id=root.id,
            parent_platform_comment_id=root.platform_comment_id, platform="youtube", platform_comment_id=f"reply-{uuid.uuid4().hex[:8]}",
            author_name="Analytics channel", text="Thanks for the comment", published_at=datetime.now(timezone.utc) - timedelta(days=1),
            is_reply=True, is_owner_comment=True, workflow_status="open",
        )
        db.add(reply)
        db.add(AnalyticsSnapshot(workspace_id=workspace_id, social_account_id=account.id, platform="youtube", source="youtube_analytics", metric_scope="account", entity_key="account", metric_date=date.today(), metrics={"views": 100, "likes": 12, "estimatedMinutesWatched": 50}))
        db.commit()
        return str(account.id), str(workspace_id)
    finally:
        try:
            generator.close()
        except Exception:
            pass


def test_overview_reports_operational_metrics_and_date_range(analytics_client: TestClient) -> None:
    _seed(analytics_client)
    start = (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()
    end = (datetime.now(timezone.utc).date()).isoformat()
    response = analytics_client.get(f"/api/v1/analytics/overview?start_date={start}&end_date={end}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kpis"]["comments_received"] == 1
    assert body["kpis"]["owner_replies_published"] == 1
    assert body["kpis"]["comments_resolved"] == 1
    assert body["kpis"]["response_rate"] == 100.0
    assert 80000 <= body["kpis"]["average_resolution_time_seconds"] <= 88000
    assert body["period"]["timezone"] == "UTC"


def test_timeseries_attributes_resolution_to_resolved_at_day(analytics_client: TestClient) -> None:
    _seed(analytics_client)
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=7)).isoformat()
    end = today.isoformat()
    response = analytics_client.get(f"/api/v1/analytics/timeseries?start_date={start}&end_date={end}")
    assert response.status_code == 200, response.text
    by_day = {item["date"]: item for item in response.json()["items"]}
    assert by_day[(today - timedelta(days=2)).isoformat()]["comments_received"] == 1
    assert by_day[(today - timedelta(days=2)).isoformat()]["comments_resolved"] == 0
    assert by_day[(today - timedelta(days=1)).isoformat()]["comments_resolved"] == 1


def test_account_filter_is_workspace_scoped(analytics_client: TestClient) -> None:
    account_id, _ = _seed(analytics_client)
    assert analytics_client.get(f"/api/v1/analytics/accounts?account_id={account_id}").status_code == 200
    # An arbitrary ID must not turn into a cross-workspace object lookup.
    response = analytics_client.get(f"/api/v1/analytics/accounts?account_id={uuid.uuid4()}")
    assert response.status_code == 404


def test_refresh_skips_accounts_missing_analytics_scope(analytics_client: TestClient) -> None:
    account_id, _ = _seed(analytics_client, scope=False)
    response = analytics_client.post("/api/v1/analytics/refresh", headers={"X-CSRF-Token": _csrf(analytics_client)}, json={"account_ids": [account_id]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queued_count"] == 0
    assert body["skipped"][0]["reason"] == "analytics_scope_missing"
    assert "required_scope" in body["skipped"][0]


def test_csv_export_escapes_formula_cells(analytics_client: TestClient) -> None:
    _seed(analytics_client, display_name="=IMPORTDATA(\"bad\")")
    response = analytics_client.get("/api/v1/analytics/export.csv?report=accounts")
    assert response.status_code == 200, response.text
    assert "text/csv" in response.headers["content-type"]
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[1][2].startswith("'=IMPORTDATA")


def test_bad_date_range_returns_422_and_max_span_is_enforced(analytics_client: TestClient) -> None:
    _login(analytics_client)
    start = date.today() - timedelta(days=500)
    end = date.today()
    response = analytics_client.get(f"/api/v1/analytics/timeseries?start_date={start}&end_date={end}")
    assert response.status_code == 422
    with pytest.raises(ValueError):
        resolve_period(start, end)


def test_youtube_report_parser_parses_daily_numeric_metrics() -> None:
    parsed = _parse_youtube_report({
        "columnHeaders": [{"name": "day"}, {"name": "views"}, {"name": "likes"}, {"name": "estimatedMinutesWatched"}],
        "rows": [["2026-10-20", 123, 9, 18.5], ["bad-date", 2, 1, 3]],
    })
    assert parsed == [(date(2026, 10, 20), {"views": 123, "likes": 9, "estimatedMinutesWatched": 18.5})]


def test_instagram_insights_parser_merges_metric_values_by_day() -> None:
    parsed = _parse_instagram_metric({"data": [{"name": "reach", "values": [{"value": 12, "end_time": "2026-10-20T07:00:00+0000"}]}]}, "reach", date(2026, 10, 20))
    assert parsed == [(date(2026, 10, 20), {"reach": 12})]


def test_performance_report_includes_workspace_scoped_top_media_and_export(analytics_client: TestClient) -> None:
    account_id, workspace_id = _seed(analytics_client)
    # Save a current per-video snapshot; this is deliberately distinct from account daily data.
    engine_session = app.dependency_overrides[get_db]()
    db = next(engine_session)
    try:
        media = db.query(MediaItem).filter(MediaItem.social_account_id == uuid.UUID(account_id)).first()
        assert media is not None
        db.add(AnalyticsSnapshot(
            workspace_id=uuid.UUID(workspace_id), social_account_id=uuid.UUID(account_id), platform="youtube",
            source="youtube_video_statistics", metric_scope="media", entity_key=media.platform_media_id,
            platform_media_id=media.platform_media_id, metric_date=date.today(),
            metrics={"views": 4321, "likes": 321, "comments": 45},
        ))
        db.commit()
    finally:
        try:
            engine_session.close()
        except Exception:
            pass
    response = analytics_client.get("/api/v1/analytics/performance")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["top_media"]) == 1
    assert body["top_media"][0]["metrics"]["views"] == 4321
    export = analytics_client.get("/api/v1/analytics/export.csv?report=top_media")
    assert export.status_code == 200, export.text
    assert "youtube_video_statistics" not in export.text
    assert "4321" in export.text


def test_performance_report_omits_media_without_provider_snapshot(analytics_client: TestClient) -> None:
    _seed(analytics_client)
    response = analytics_client.get("/api/v1/analytics/performance")
    assert response.status_code == 200, response.text
    assert response.json()["top_media"] == []


def test_instagram_parser_supports_direct_scalar_values() -> None:
    parsed = _parse_instagram_metric({"data": [{"name": "total_interactions", "value": 18}]}, "total_interactions", date(2026, 10, 20))
    assert parsed == [(date(2026, 10, 20), {"total_interactions": 18})]


def test_youtube_media_snapshot_import_parses_provider_count_strings(analytics_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.analytics as analytics_service

    account_id, _ = _seed(analytics_client)
    generator = app.dependency_overrides[get_db]()
    db = next(generator)
    try:
        account = db.get(SocialAccount, uuid.UUID(account_id))
        media = db.scalar(select(MediaItem).where(MediaItem.social_account_id == uuid.UUID(account_id)))
        assert account is not None and media is not None

        async def fake_get_json(client, url, params, token):
            assert url.endswith("/youtube/v3/videos")
            assert params["id"] == media.platform_media_id
            return {"items": [{"id": media.platform_media_id, "statistics": {"viewCount": "1200", "likeCount": "44", "commentCount": "9"}}]}

        monkeypatch.setattr(analytics_service, "_get_json", fake_get_json)
        received, stored, partial = asyncio.run(_sync_youtube_media_snapshots(db, object(), account, "fake-token", date.today(), 10))
        assert (received, stored, partial) == (1, 1, False)
        db.flush()
        snapshot = db.scalar(select(AnalyticsSnapshot).where(
            AnalyticsSnapshot.social_account_id == account.id,
            AnalyticsSnapshot.metric_scope == "media",
            AnalyticsSnapshot.platform_media_id == media.platform_media_id,
        ))
        assert snapshot is not None
        assert snapshot.metrics == {"views": 1200, "likes": 44, "comments": 9}
    finally:
        generator.close()


def test_overview_ai_draft_metrics_respect_selected_account(analytics_client: TestClient) -> None:
    account_id, workspace_id = _seed(analytics_client)
    generator = app.dependency_overrides[get_db]()
    db = next(generator)
    try:
        root = db.scalar(select(SocialComment).where(SocialComment.workspace_id == uuid.UUID(workspace_id), SocialComment.is_owner_comment.is_(False)))
        assert root is not None
        account_b = SocialAccount(
            workspace_id=uuid.UUID(workspace_id), platform="youtube", platform_account_id=f"yt-{uuid.uuid4().hex[:8]}",
            display_name="Second channel", username="second", status="connected", scopes=[],
        )
        db.add(account_b)
        db.flush()
        today = datetime.now(timezone.utc)
        for target_id in (uuid.UUID(account_id), account_b.id):
            db.add(AIReplyDraft(
                workspace_id=uuid.UUID(workspace_id), comment_id=root.id, target_account_id=target_id,
                automation_rule_id=None, idempotency_key=f"analytics-test-{uuid.uuid4().hex}", status="draft",
                tone="helpful", language="match_comment", provider="openai_compatible", model="gpt-4o-mini",
                prompt_version="reply-v1", context_fingerprint="a" * 64, draft_text="Thanks for sharing this.", generated_at=today,
            ))
        db.commit()
    finally:
        generator.close()
    today_text = datetime.now(timezone.utc).date().isoformat()
    response = analytics_client.get(f"/api/v1/analytics/overview?start_date={today_text}&end_date={today_text}&account_id={account_id}")
    assert response.status_code == 200, response.text
    assert response.json()["kpis"]["ai_drafts_generated"] == 1


def test_timeseries_keeps_unavailable_provider_metrics_missing(analytics_client: TestClient) -> None:
    _seed(analytics_client)
    today = date.today()
    start = (today - timedelta(days=2)).isoformat()
    response = analytics_client.get(f"/api/v1/analytics/timeseries?start_date={start}&end_date={today.isoformat()}")
    assert response.status_code == 200, response.text
    prior_day = next(item for item in response.json()["items"] if item["date"] == start)
    assert prior_day["views"] is None
    assert prior_day["reach"] is None
    assert isinstance(prior_day["comments_received"], int)


def test_account_report_uses_analytics_run_timestamp_when_comment_sync_is_absent(analytics_client: TestClient) -> None:
    account_id, workspace_id = _seed(analytics_client)
    now = datetime.now(timezone.utc)
    generator = app.dependency_overrides[get_db]()
    db = next(generator)
    try:
        db.add(AnalyticsSyncRun(
            workspace_id=uuid.UUID(workspace_id), social_account_id=uuid.UUID(account_id), trigger="manual", status="success",
            period_start=now.date() - timedelta(days=2), period_end=now.date(), finished_at=now,
        ))
        db.commit()
    finally:
        generator.close()
    response = analytics_client.get(f"/api/v1/analytics/accounts?account_id={account_id}")
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["last_analytics_sync_at"] is not None
