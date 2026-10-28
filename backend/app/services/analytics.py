"""Workspace-scoped reporting and provider analytics collection.

Operational metrics are derived from the platform's own durable records. Provider
metrics are stored separately so unavailable/unauthorized data is never presented as
zero. The service never stores access tokens or raw provider errors in analytics rows.
"""
from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import httpx
from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.redis_client import get_redis_client
from app.core.security import decrypt_secret
from app.models.ai_reply_draft import AIReplyDraft
from app.models.analytics_snapshot import AnalyticsSnapshot
from app.models.analytics_sync_run import AnalyticsSyncRun
from app.models.comment import SocialComment
from app.models.comment_automation_rule import CommentAutomationRule, CommentAutomationRun
from app.models.comment_publish_batch import CommentPublishItem
from app.models.comment_sync_run import CommentSyncRun
from app.models.media_item import MediaItem
from app.models.social_account import SocialAccount
from app.models.workspace import Workspace
from app.services.account_connections import refresh_social_account
from app.services.comment_jobs import QUEUE_NAME
from app.services.comment_sync import ProviderSyncError
from app.services.account_connections import OAuthFlowError

logger = logging.getLogger(__name__)
YOUTUBE_ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
INSTAGRAM_INSIGHTS_SCOPE = "instagram_business_manage_insights"
VALID_PLATFORMS = {"youtube", "instagram"}
YT_METRICS = (
    "views", "likes", "comments", "shares", "estimatedMinutesWatched",
    "subscribersGained", "subscribersLost",
)
IG_METRICS = ("views", "reach", "accounts_engaged", "total_interactions")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def resolve_period(start_date: date | None, end_date: date | None, *, max_days: int = 366) -> tuple[date, date]:
    today = utcnow().date()
    end = end_date or today
    start = start_date or (end - timedelta(days=29))
    if end < start:
        raise ValueError("end_date must be on or after start_date")
    if (end - start).days + 1 > max_days:
        raise ValueError(f"Date range may not exceed {max_days} days")
    return start, end


def _utc_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    return datetime.combine(start, time.min, tzinfo=timezone.utc), datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)


def _assert_filter_account(db: Session, workspace_id: uuid.UUID, platform: str | None, account_id: uuid.UUID | None) -> None:
    if platform and platform not in VALID_PLATFORMS:
        raise HTTPException(status_code=422, detail="platform must be youtube or instagram")
    if account_id is not None:
        account = db.scalar(select(SocialAccount.id).where(SocialAccount.id == account_id, SocialAccount.workspace_id == workspace_id))
        if account is None:
            # Do not distinguish a missing account from one in another workspace.
            raise HTTPException(status_code=404, detail="Connected account not found")
        if platform:
            account_platform = db.scalar(select(SocialAccount.platform).where(SocialAccount.id == account_id, SocialAccount.workspace_id == workspace_id))
            if account_platform != platform:
                raise HTTPException(status_code=422, detail="account_id does not belong to the selected platform")


def _comment_filters(workspace_id: uuid.UUID, start: date, end: date, platform: str | None, account_id: uuid.UUID | None):
    start_dt, end_dt = _utc_bounds(start, end)
    filters: list[Any] = [SocialComment.workspace_id == workspace_id, SocialComment.published_at >= start_dt, SocialComment.published_at < end_dt]
    if platform:
        filters.append(SocialComment.platform == platform)
    if account_id:
        filters.append(SocialComment.social_account_id == account_id)
    return filters


def _account_filters(workspace_id: uuid.UUID, platform: str | None, account_id: uuid.UUID | None):
    filters: list[Any] = [SocialAccount.workspace_id == workspace_id]
    if platform:
        filters.append(SocialAccount.platform == platform)
    if account_id:
        filters.append(SocialAccount.id == account_id)
    return filters


def _count(db: Session, statement) -> int:
    return int(db.scalar(statement) or 0)


def _avg_response_seconds(db: Session, workspace_id: uuid.UUID, start: date, end: date, platform: str | None, account_id: uuid.UUID | None) -> float | None:
    parent = aliased(SocialComment)
    start_dt, end_dt = _utc_bounds(start, end)
    filters: list[Any] = [
        SocialComment.workspace_id == workspace_id,
        SocialComment.is_owner_comment.is_(True), SocialComment.is_reply.is_(True),
        SocialComment.parent_comment_id == parent.id,
        SocialComment.published_at.is_not(None), parent.published_at.is_not(None),
        SocialComment.published_at >= start_dt, SocialComment.published_at < end_dt,
        parent.is_owner_comment.is_(False), parent.is_reply.is_(False),
    ]
    if platform:
        filters.extend([SocialComment.platform == platform, parent.platform == platform])
    if account_id:
        filters.extend([SocialComment.social_account_id == account_id, parent.social_account_id == account_id])
    pairs = db.execute(select(SocialComment.published_at, parent.published_at).select_from(SocialComment).join(parent, SocialComment.parent_comment_id == parent.id).where(*filters).limit(10000)).all()
    seconds: list[float] = []
    for reply_at, parent_at in pairs:
        if not reply_at or not parent_at:
            continue
        if reply_at.tzinfo is None:
            reply_at = reply_at.replace(tzinfo=timezone.utc)
        if parent_at.tzinfo is None:
            parent_at = parent_at.replace(tzinfo=timezone.utc)
        delta = (reply_at - parent_at).total_seconds()
        if 0 <= delta <= 365 * 86400:
            seconds.append(delta)
    return round(sum(seconds) / len(seconds), 2) if seconds else None


def _avg_resolution_seconds(db: Session, workspace_id: uuid.UUID, start: date, end: date, platform: str | None, account_id: uuid.UUID | None) -> float | None:
    """Average time from the provider comment's publish time to its recorded resolution.

    Historical rows intentionally remain excluded when `resolved_at` is null; their
    resolution time cannot be reconstructed accurately from current workflow state.
    """
    start_dt, end_dt = _utc_bounds(start, end)
    filters: list[Any] = [
        SocialComment.workspace_id == workspace_id,
        SocialComment.is_owner_comment.is_(False),
        SocialComment.workflow_status == "resolved",
        SocialComment.resolved_at.is_not(None),
        SocialComment.resolved_at >= start_dt,
        SocialComment.resolved_at < end_dt,
        SocialComment.published_at.is_not(None),
    ]
    if platform:
        filters.append(SocialComment.platform == platform)
    if account_id:
        filters.append(SocialComment.social_account_id == account_id)
    pairs = db.execute(select(SocialComment.published_at, SocialComment.resolved_at).where(*filters).limit(10000)).all()
    seconds: list[float] = []
    for published_at, resolved_at in pairs:
        if not published_at or not resolved_at:
            continue
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        if resolved_at.tzinfo is None:
            resolved_at = resolved_at.replace(tzinfo=timezone.utc)
        delta = (resolved_at - published_at).total_seconds()
        if 0 <= delta <= 365 * 86400:
            seconds.append(delta)
    return round(sum(seconds) / len(seconds), 2) if seconds else None


def _provider_scope(account: SocialAccount) -> str | None:
    scopes = set(account.scopes or [])
    required = YOUTUBE_ANALYTICS_SCOPE if account.platform == "youtube" else INSTAGRAM_INSIGHTS_SCOPE
    return None if required in scopes else required


def _latest_snapshots_by_account(db: Session, workspace_id: uuid.UUID, accounts: list[SocialAccount]) -> dict[uuid.UUID, dict[str, Any]]:
    if not accounts:
        return {}
    account_ids = [item.id for item in accounts]
    rows = db.scalars(select(AnalyticsSnapshot).where(
        AnalyticsSnapshot.workspace_id == workspace_id,
        AnalyticsSnapshot.social_account_id.in_(account_ids),
        AnalyticsSnapshot.metric_scope == "account",
    ).order_by(AnalyticsSnapshot.metric_date.desc(), AnalyticsSnapshot.captured_at.desc())).all()
    result: dict[uuid.UUID, dict[str, Any]] = {}
    for row in rows:
        if row.social_account_id not in result:
            result[row.social_account_id] = {"date": row.metric_date.isoformat(), "captured_at": row.captured_at.isoformat() if row.captured_at else None, "metrics": row.metrics or {}, "source": row.source}
    return result


def get_overview(db: Session, workspace: Workspace, start: date, end: date, platform: str | None = None, account_id: uuid.UUID | None = None) -> dict[str, Any]:
    _assert_filter_account(db, workspace.id, platform, account_id)
    comment_filters = _comment_filters(workspace.id, start, end, platform, account_id)
    account_filters = _account_filters(workspace.id, platform, account_id)
    external = [*comment_filters, SocialComment.is_owner_comment.is_(False)]
    root_external = [*external, SocialComment.is_reply.is_(False)]
    owner_replies = [*comment_filters, SocialComment.is_owner_comment.is_(True), SocialComment.is_reply.is_(True)]
    owner_top_level = [*comment_filters, SocialComment.is_owner_comment.is_(True), SocialComment.is_reply.is_(False)]

    received = _count(db, select(func.count(SocialComment.id)).where(*external))
    received_roots = _count(db, select(func.count(SocialComment.id)).where(*root_external))
    replied_roots = _count(db, select(func.count(SocialComment.id)).where(*root_external, SocialComment.has_owner_reply.is_(True)))
    unanswered_roots = _count(db, select(func.count(SocialComment.id)).where(*root_external, SocialComment.has_owner_reply.is_(False), SocialComment.workflow_status == "open"))
    start_dt, end_dt = _utc_bounds(start, end)
    resolved_filters: list[Any] = [
        SocialComment.workspace_id == workspace.id,
        SocialComment.is_owner_comment.is_(False),
        SocialComment.workflow_status == "resolved",
        SocialComment.resolved_at >= start_dt,
        SocialComment.resolved_at < end_dt,
    ]
    if platform:
        resolved_filters.append(SocialComment.platform == platform)
    if account_id:
        resolved_filters.append(SocialComment.social_account_id == account_id)
    resolved = _count(db, select(func.count(SocialComment.id)).where(*resolved_filters))
    replies_published = _count(db, select(func.count(SocialComment.id)).where(*owner_replies))
    top_level_published = _count(db, select(func.count(SocialComment.id)).where(*owner_top_level))
    comment_likes = int(db.scalar(select(func.coalesce(func.sum(SocialComment.like_count), 0)).where(*external)) or 0)
    observed_reply_count = int(db.scalar(select(func.coalesce(func.sum(SocialComment.reply_count), 0)).where(*root_external)) or 0)

    publish_filters: list[Any] = [CommentPublishItem.workspace_id == workspace.id, CommentPublishItem.created_at >= start_dt, CommentPublishItem.created_at < end_dt]
    if platform and platform != "youtube":
        publish_filters.append(False)
    if account_id:
        publish_filters.append(CommentPublishItem.social_account_id == account_id)
    publish_items = db.scalars(select(CommentPublishItem).where(*publish_filters)).all()
    publish_success = sum(1 for item in publish_items if item.status == "published")
    publish_failed = sum(1 for item in publish_items if item.status == "failed")
    publish_unknown = sum(1 for item in publish_items if item.status == "unknown")
    publish_terminal = publish_success + publish_failed + publish_unknown

    # Scope draft metrics to the selected target account/platform as well as workspace.
    # Drafts without a selected target account are not attributed to a platform filter.
    draft_target_filters: list[Any] = []
    if account_id:
        draft_target_filters.append(AIReplyDraft.target_account_id == account_id)
    elif platform:
        draft_target_filters.append(AIReplyDraft.target_account_id.in_(select(SocialAccount.id).where(
            SocialAccount.workspace_id == workspace.id, SocialAccount.platform == platform
        )))
    drafts_generated = _count(db, select(func.count(AIReplyDraft.id)).where(
        AIReplyDraft.workspace_id == workspace.id, AIReplyDraft.generated_at >= start_dt,
        AIReplyDraft.generated_at < end_dt, *draft_target_filters
    ))
    drafts_published = _count(db, select(func.count(AIReplyDraft.id)).where(
        AIReplyDraft.workspace_id == workspace.id, AIReplyDraft.published_at >= start_dt,
        AIReplyDraft.published_at < end_dt, *draft_target_filters
    ))
    draft_filters = [AIReplyDraft.workspace_id == workspace.id, AIReplyDraft.created_at >= start_dt, AIReplyDraft.created_at < end_dt, *draft_target_filters]
    drafts_pending = _count(db, select(func.count(AIReplyDraft.id)).where(*draft_filters, AIReplyDraft.status.in_(["queued", "generating", "draft", "approved"])))
    drafts_failed = _count(db, select(func.count(AIReplyDraft.id)).where(*draft_filters, AIReplyDraft.status == "failed"))

    run_filters: list[Any] = [CommentAutomationRun.workspace_id == workspace.id, CommentAutomationRun.queued_at >= start_dt, CommentAutomationRun.queued_at < end_dt]
    runs = db.scalars(select(CommentAutomationRun).where(*run_filters)).all()
    rules_filters: list[Any] = [CommentAutomationRule.workspace_id == workspace.id]
    rules = db.scalars(select(CommentAutomationRule).where(*rules_filters)).all()
    accounts = db.scalars(select(SocialAccount).where(*account_filters)).all()

    return {
        "period": {"start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": "UTC"},
        "filters": {"platform": platform, "account_id": str(account_id) if account_id else None},
        "kpis": {
            "comments_received": received,
            "root_comments_received": received_roots,
            "owner_replies_published": replies_published,
            "top_level_comments_published": top_level_published,
            "comments_resolved": resolved,
            "unanswered_comments": unanswered_roots,
            "response_rate": round((replied_roots / received_roots) * 100, 2) if received_roots else None,
            "average_response_time_seconds": _avg_response_seconds(db, workspace.id, start, end, platform, account_id),
            "average_resolution_time_seconds": _avg_resolution_seconds(db, workspace.id, start, end, platform, account_id),
            "comment_likes_received": comment_likes,
            "provider_reported_replies": observed_reply_count,
            "publish_attempts": len(publish_items),
            "publish_success_count": publish_success,
            "publish_failure_count": publish_failed,
            "publish_outcome_unknown_count": publish_unknown,
            "publish_success_rate": round(publish_success / publish_terminal * 100, 2) if publish_terminal else None,
            "ai_drafts_generated": drafts_generated,
            "ai_drafts_published": drafts_published,
            "ai_drafts_pending_review": drafts_pending,
            "ai_drafts_failed": drafts_failed,
            "automation_rules_total": len(rules),
            "automation_rules_enabled": sum(1 for rule in rules if rule.enabled),
            "automation_runs": len(runs),
            "automation_runs_success": sum(1 for run in runs if run.status == "success"),
            "automation_runs_partial": sum(1 for run in runs if run.status == "partial"),
            "automation_runs_failed": sum(1 for run in runs if run.status == "error"),
            "automation_comments_scanned": sum(int(run.scanned_count or 0) for run in runs),
            "automation_comments_matched": sum(int(run.matched_count or 0) for run in runs),
            "automation_drafts_queued": sum(int(run.drafts_queued_count or 0) for run in runs),
            "automation_execution_failures": sum(int(run.failed_count or 0) for run in runs),
            "connected_accounts": sum(1 for item in accounts if item.status == "connected"),
            "accounts_requiring_reauthorization": sum(1 for item in accounts if item.status == "reauthorization_required"),
            "accounts_disconnected": sum(1 for item in accounts if item.status == "disconnected"),
        },
        "definitions": {
            "comments_received": "Provider comments and replies first/published within the selected date range, excluding comments authored by connected accounts.",
            "owner_replies_published": "Stored replies authored by a connected account; historical values depend on imported comment history.",
            "response_rate": "Root incoming comments in the selected period that currently have a connected-account reply.",
            "comments_resolved": "Incoming comments whose resolution transition was recorded in the selected period. Historical rows without resolved_at are not retroactively counted.",
            "average_resolution_time_seconds": "Average elapsed time from comment publication to its recorded resolution, limited to known timestamps and intervals of at most 365 days. Historical resolution times are unavailable unless recorded.",
            "publish_success_rate": "Published items divided by terminal multi-channel publish items (published, failed, or outcome unknown).",
            "historical_limits": "Metrics begin when records were first synced or recorded by this application; they are not a reconstruction of earlier periods.",
        },
    }


def get_timeseries(db: Session, workspace: Workspace, start: date, end: date, platform: str | None = None, account_id: uuid.UUID | None = None) -> dict[str, Any]:
    _assert_filter_account(db, workspace.id, platform, account_id)
    days = (end - start).days + 1
    if days > 366:
        raise ValueError("Date range may not exceed 366 days")
    items: dict[str, dict[str, Any]] = {}
    cursor = start
    while cursor <= end:
        items[cursor.isoformat()] = {
            "date": cursor.isoformat(), "comments_received": 0, "owner_replies_published": 0,
            "top_level_comments_published": 0, "comments_resolved": 0, "unanswered_comments": 0,
            "comment_likes_received": 0, "views": None, "reach": None, "platform_likes": None,
            "shares": None, "platform_comments": None, "estimated_minutes_watched": None,
            "subscribers_gained": None, "total_interactions": None, "accounts_engaged": None,
        }
        cursor += timedelta(days=1)

    # Aggregate operational events in SQL to avoid loading every comment into memory.
    filters = _comment_filters(workspace.id, start, end, platform, account_id)
    day_expr = func.date(SocialComment.published_at)
    rows = db.execute(select(
        day_expr.label("day"),
        func.sum(case((SocialComment.is_owner_comment.is_(False), 1), else_=0)).label("incoming"),
        func.sum(case((and_(SocialComment.is_owner_comment.is_(True), SocialComment.is_reply.is_(True)), 1), else_=0)).label("replies"),
        func.sum(case((and_(SocialComment.is_owner_comment.is_(True), SocialComment.is_reply.is_(False)), 1), else_=0)).label("top_level"),
        func.sum(case((and_(SocialComment.is_owner_comment.is_(False), SocialComment.is_reply.is_(False), SocialComment.has_owner_reply.is_(False), SocialComment.workflow_status == "open"), 1), else_=0)).label("unanswered"),
        func.sum(case((SocialComment.is_owner_comment.is_(False), SocialComment.like_count), else_=0)).label("likes"),
    ).where(*filters).group_by(day_expr).order_by(day_expr)).all()
    for row in rows:
        key = row.day.isoformat() if hasattr(row.day, "isoformat") else str(row.day)
        if key not in items:
            continue
        items[key].update({
            "comments_received": int(row.incoming or 0), "owner_replies_published": int(row.replies or 0),
            "top_level_comments_published": int(row.top_level or 0),
            "unanswered_comments": int(row.unanswered or 0), "comment_likes_received": int(row.likes or 0),
        })

    # Resolution is a separate workflow event, not the original comment publication
    # event; attribute it to resolved_at so charts reflect what happened that day.
    start_dt, end_dt = _utc_bounds(start, end)
    resolved_filters: list[Any] = [
        SocialComment.workspace_id == workspace.id,
        SocialComment.is_owner_comment.is_(False),
        SocialComment.workflow_status == "resolved",
        SocialComment.resolved_at >= start_dt,
        SocialComment.resolved_at < end_dt,
    ]
    if platform:
        resolved_filters.append(SocialComment.platform == platform)
    if account_id:
        resolved_filters.append(SocialComment.social_account_id == account_id)
    resolved_day = func.date(SocialComment.resolved_at)
    for row in db.execute(select(resolved_day.label("day"), func.count(SocialComment.id).label("total")).where(*resolved_filters).group_by(resolved_day)).all():
        key = row.day.isoformat() if hasattr(row.day, "isoformat") else str(row.day)
        if key in items:
            items[key]["comments_resolved"] = int(row.total or 0)

    snapshot_filters: list[Any] = [AnalyticsSnapshot.workspace_id == workspace.id, AnalyticsSnapshot.metric_date >= start, AnalyticsSnapshot.metric_date <= end, AnalyticsSnapshot.metric_scope == "account"]
    if platform:
        snapshot_filters.append(AnalyticsSnapshot.platform == platform)
    if account_id:
        snapshot_filters.append(AnalyticsSnapshot.social_account_id == account_id)
    for snapshot in db.scalars(select(AnalyticsSnapshot).where(*snapshot_filters)).all():
        key = snapshot.metric_date.isoformat()
        if key not in items:
            continue
        metrics = snapshot.metrics or {}
        metric_map = {
            "views": "views", "reach": "reach", "likes": "platform_likes", "shares": "shares",
            "comments": "platform_comments", "estimatedMinutesWatched": "estimated_minutes_watched",
            "subscribersGained": "subscribers_gained", "total_interactions": "total_interactions", "accounts_engaged": "accounts_engaged",
        }
        for source_key, destination in metric_map.items():
            value = metrics.get(source_key)
            if isinstance(value, (int, float)) and math.isfinite(value):
                items[key][destination] = (items[key][destination] or 0) + value
    return {"period": {"start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": "UTC"}, "items": list(items.values()), "note": "Provider metrics are sums of available daily snapshots and may not be comparable across platforms. Missing snapshots are stored as missing, not inferred as zero."}


def get_accounts_report(db: Session, workspace: Workspace, start: date, end: date, platform: str | None = None, account_id: uuid.UUID | None = None) -> dict[str, Any]:
    _assert_filter_account(db, workspace.id, platform, account_id)
    account_filters = _account_filters(workspace.id, platform, account_id)
    accounts = db.scalars(select(SocialAccount).where(*account_filters).order_by(SocialAccount.platform, SocialAccount.display_name)).all()
    start_dt, end_dt = _utc_bounds(start, end)
    latest = _latest_snapshots_by_account(db, workspace.id, accounts)
    item_list: list[dict[str, Any]] = []
    for account in accounts:
        comment_filters: list[Any] = [SocialComment.workspace_id == workspace.id, SocialComment.social_account_id == account.id, SocialComment.published_at >= start_dt, SocialComment.published_at < end_dt]
        incoming = _count(db, select(func.count(SocialComment.id)).where(*comment_filters, SocialComment.is_owner_comment.is_(False)))
        replies = _count(db, select(func.count(SocialComment.id)).where(*comment_filters, SocialComment.is_owner_comment.is_(True), SocialComment.is_reply.is_(True)))
        own_top = _count(db, select(func.count(SocialComment.id)).where(*comment_filters, SocialComment.is_owner_comment.is_(True), SocialComment.is_reply.is_(False)))
        resolved_filters = [SocialComment.workspace_id == workspace.id, SocialComment.social_account_id == account.id,
            SocialComment.is_owner_comment.is_(False), SocialComment.workflow_status == "resolved",
            SocialComment.resolved_at >= start_dt, SocialComment.resolved_at < end_dt]
        resolved = _count(db, select(func.count(SocialComment.id)).where(*resolved_filters))
        likes = int(db.scalar(select(func.coalesce(func.sum(SocialComment.like_count), 0)).where(*comment_filters, SocialComment.is_owner_comment.is_(False))) or 0)
        metric = latest.get(account.id)
        latest_run = db.scalar(select(AnalyticsSyncRun).where(AnalyticsSyncRun.social_account_id == account.id).order_by(AnalyticsSyncRun.queued_at.desc()).limit(1))
        latest_sync = db.scalar(select(CommentSyncRun).where(CommentSyncRun.social_account_id == account.id).order_by(CommentSyncRun.queued_at.desc()).limit(1))
        required_scope = _provider_scope(account)
        item_list.append({
            "account_id": str(account.id), "platform": account.platform, "display_name": account.display_name,
            "username": account.username, "status": account.status, "connected_at": account.connected_at.isoformat() if account.connected_at else None,
            "last_comment_sync_status": latest_sync.status if latest_sync else account.last_sync_status,
            "last_comment_sync_at": latest_sync.finished_at.isoformat() if latest_sync and latest_sync.finished_at else (account.last_sync_completed_at.isoformat() if account.last_sync_completed_at else None),
            "last_comment_sync_error_code": latest_sync.error_code if latest_sync else account.last_sync_error_code,
            "comment_metrics": {"received": incoming, "owner_replies_published": replies, "top_level_comments_published": own_top, "resolved": resolved, "likes_received": likes},
            "provider_metrics": metric["metrics"] if metric else None,
            "provider_metrics_date": metric["date"] if metric else None,
            "provider_metrics_captured_at": metric["captured_at"] if metric else None,
            "provider_metrics_status": "scope_missing" if required_scope else ("account_not_connected" if account.status != "connected" else (latest_run.status if latest_run else "never")),
            "required_scope": required_scope,
            "last_analytics_sync_at": latest_run.finished_at.isoformat() if latest_run and latest_run.finished_at else None,
            "last_analytics_sync_status": latest_run.status if latest_run else "never",
            "last_analytics_sync_error_code": latest_run.error_code if latest_run else None,
        })
    return {"period": {"start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": "UTC"}, "items": item_list, "count": len(item_list)}


def get_automation_report(db: Session, workspace: Workspace, start: date, end: date) -> dict[str, Any]:
    start_dt, end_dt = _utc_bounds(start, end)
    rules = db.scalars(select(CommentAutomationRule).where(CommentAutomationRule.workspace_id == workspace.id).order_by(CommentAutomationRule.priority, CommentAutomationRule.name)).all()
    run_period_filters = [CommentAutomationRun.workspace_id == workspace.id, CommentAutomationRun.queued_at >= start_dt, CommentAutomationRun.queued_at < end_dt]
    run_aggregates = db.execute(select(
        func.count(CommentAutomationRun.id),
        func.sum(case((CommentAutomationRun.status == "success", 1), else_=0)),
        func.sum(case((CommentAutomationRun.status == "partial", 1), else_=0)),
        func.sum(case((CommentAutomationRun.status == "error", 1), else_=0)),
        func.sum(CommentAutomationRun.scanned_count), func.sum(CommentAutomationRun.matched_count),
        func.sum(CommentAutomationRun.drafts_queued_count), func.sum(CommentAutomationRun.failed_count),
    ).where(*run_period_filters)).one()
    runs = db.scalars(select(CommentAutomationRun).where(*run_period_filters).order_by(CommentAutomationRun.queued_at.desc()).limit(5000)).all()
    run_items = [{
        "run_id": str(run.id), "rule_id": str(run.rule_id) if run.rule_id else None, "status": run.status,
        "trigger": run.trigger, "queued_at": run.queued_at.isoformat() if run.queued_at else None,
        "scanned_count": int(run.scanned_count or 0), "matched_count": int(run.matched_count or 0),
        "drafts_queued_count": int(run.drafts_queued_count or 0), "failed_count": int(run.failed_count or 0), "error_code": run.error_code,
    } for run in runs]
    return {"period": {"start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": "UTC"},
            "rules_total": len(rules), "rules_enabled": sum(1 for rule in rules if rule.enabled),
            "runs_total": int(run_aggregates[0] or 0), "runs_success": int(run_aggregates[1] or 0),
            "runs_partial": int(run_aggregates[2] or 0), "runs_failed": int(run_aggregates[3] or 0),
            "scanned_comments": int(run_aggregates[4] or 0), "matched_comments": int(run_aggregates[5] or 0),
            "drafts_queued": int(run_aggregates[6] or 0), "failed_executions": int(run_aggregates[7] or 0),
            "by_rule": [{"id": str(rule.id), "name": rule.name, "enabled": bool(rule.enabled), "priority": rule.priority, "last_run_at": rule.last_run_at.isoformat() if rule.last_run_at else None} for rule in rules],
            "runs": run_items}


def get_performance_report(db: Session, workspace: Workspace, start: date, end: date, platform: str | None = None, account_id: uuid.UUID | None = None) -> dict[str, Any]:
    _assert_filter_account(db, workspace.id, platform, account_id)
    account_filters = _account_filters(workspace.id, platform, account_id)
    accounts = db.scalars(select(SocialAccount).where(*account_filters).order_by(SocialAccount.platform, SocialAccount.display_name)).all()
    latest = _latest_snapshots_by_account(db, workspace.id, accounts)
    latest_runs: dict[uuid.UUID, AnalyticsSyncRun] = {}
    for account in accounts:
        run = db.scalar(select(AnalyticsSyncRun).where(AnalyticsSyncRun.workspace_id == workspace.id, AnalyticsSyncRun.social_account_id == account.id).order_by(AnalyticsSyncRun.queued_at.desc()).limit(1))
        if run:
            latest_runs[account.id] = run
    items = []
    for account in accounts:
        required = _provider_scope(account)
        last = latest.get(account.id)
        run = latest_runs.get(account.id)
        items.append({
            "account_id": str(account.id), "platform": account.platform, "display_name": account.display_name,
            "status": account.status, "scope_granted": required is None, "required_scope": required,
            "sync_status": run.status if run else "never", "error_code": run.error_code if run else None,
            "period_start": run.period_start.isoformat() if run else None, "period_end": run.period_end.isoformat() if run else None,
            "last_synced_at": run.finished_at.isoformat() if run and run.finished_at else None,
            "latest_snapshot_date": last["date"] if last else None,
            "latest_metrics": last["metrics"] if last else None,
        })
    recent_runs = db.scalars(select(AnalyticsSyncRun).where(AnalyticsSyncRun.workspace_id == workspace.id).order_by(AnalyticsSyncRun.queued_at.desc()).limit(25)).all()
    top_media: list[dict[str, Any]] = []
    if accounts:
        account_ids = [item.id for item in accounts]
        media = db.scalars(select(MediaItem).where(
            MediaItem.workspace_id == workspace.id,
            MediaItem.social_account_id.in_(account_ids),
            MediaItem.source_type.in_(["account_video", "account_media"]),
        )).all()
        media_index = {(item.social_account_id, item.platform_media_id): item for item in media}
        media_scan_limit = max(250, min(len(account_ids) * max(1, int(get_settings().analytics_media_limit)) * 5, 2500))
        snapshots = db.scalars(select(AnalyticsSnapshot).where(
            AnalyticsSnapshot.workspace_id == workspace.id,
            AnalyticsSnapshot.social_account_id.in_(account_ids),
            AnalyticsSnapshot.metric_scope == "media",
        ).order_by(AnalyticsSnapshot.captured_at.desc(), AnalyticsSnapshot.metric_date.desc()).limit(media_scan_limit)).all()
        latest_media: dict[tuple[uuid.UUID, str], AnalyticsSnapshot] = {}
        for snapshot in snapshots:
            media_id = snapshot.platform_media_id or snapshot.entity_key
            key = (snapshot.social_account_id, str(media_id))
            if key not in latest_media and key in media_index:
                latest_media[key] = snapshot
        for (social_account_id, media_id), snapshot in latest_media.items():
            media_item = media_index.get((social_account_id, media_id))
            account = next((item for item in accounts if item.id == social_account_id), None)
            if media_item is None or account is None:
                continue
            metrics = snapshot.metrics or {}
            top_media.append({
                "account_id": str(social_account_id), "account_name": account.display_name, "platform": account.platform,
                "media_id": media_id, "media_type": media_item.media_type, "title": media_item.title or media_item.description or "Untitled media",
                "permalink": media_item.permalink, "thumbnail_url": media_item.thumbnail_url,
                "published_at": media_item.published_at.isoformat() if media_item.published_at else None,
                "metrics_date": snapshot.metric_date.isoformat(), "metrics": metrics,
                "metrics_basis": "current_cumulative" if account.platform == "youtube" else "provider_lifetime_or_current",
            })
        top_media.sort(key=lambda item: (
            float(item["metrics"].get("views", 0) or 0),
            float(item["metrics"].get("reach", 0) or 0),
            float(item["metrics"].get("total_interactions", 0) or 0),
            float(item["metrics"].get("likes", 0) or 0),
        ), reverse=True)
    return {"items": items, "count": len(items), "top_media": top_media[:25], "runs": [_run_item(run, db) for run in recent_runs]}


def _run_item(run: AnalyticsSyncRun, db: Session) -> dict[str, Any]:
    account = db.get(SocialAccount, run.social_account_id)
    return {"id": str(run.id), "account_id": str(run.social_account_id), "account_name": account.display_name if account else "Removed account", "platform": account.platform if account else None,
            "status": run.status, "trigger": run.trigger, "period_start": run.period_start.isoformat(), "period_end": run.period_end.isoformat(),
            "rows_received": run.rows_received, "rows_stored": run.rows_stored, "error_code": run.error_code,
            "queued_at": run.queued_at.isoformat() if run.queued_at else None, "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None}


def queue_analytics_sync_run(db: Session, account: SocialAccount, start: date, end: date, *, trigger: str = "manual", redis_client=None) -> tuple[AnalyticsSyncRun, bool]:
    active = db.scalar(select(AnalyticsSyncRun).where(AnalyticsSyncRun.social_account_id == account.id, AnalyticsSyncRun.status.in_(["queued", "running"])).order_by(AnalyticsSyncRun.queued_at.desc()).limit(1).with_for_update())
    if active is not None:
        return active, False
    run_id = uuid.uuid4()
    run = AnalyticsSyncRun(id=run_id, workspace_id=account.workspace_id, social_account_id=account.id, trigger=trigger,
                           status="queued", period_start=start, period_end=end)
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        active = db.scalar(select(AnalyticsSyncRun).where(AnalyticsSyncRun.social_account_id == account.id, AnalyticsSyncRun.status.in_(["queued", "running"])).order_by(AnalyticsSyncRun.queued_at.desc()).limit(1))
        if active:
            return active, False
        raise
    db.refresh(run)
    payload = {"type": "analytics_sync", "job_id": str(run.id), "run_id": str(run.id), "account_id": str(account.id), "workspace_id": str(account.workspace_id)}
    try:
        (redis_client or get_redis_client()).lpush(QUEUE_NAME, json.dumps(payload, separators=(",", ":")))
    except (RedisError, OSError, ValueError):
        run.status = "error"
        run.error_code = "analytics_queue_unavailable"
        run.finished_at = utcnow()
        db.commit()
        raise RuntimeError("analytics_queue_unavailable") from None
    return run, True


def enqueue_due_analytics_syncs() -> int:
    settings = get_settings()
    interval = max(3600, int(settings.analytics_sync_interval_seconds))
    today = utcnow().date()
    end_date = today - timedelta(days=max(0, int(settings.analytics_data_lag_days)))
    start_date = end_date - timedelta(days=max(1, min(365, int(settings.analytics_sync_lookback_days))) - 1)
    queued = 0
    with SessionLocal() as db:
        accounts = db.scalars(select(SocialAccount).where(SocialAccount.status == "connected").order_by(SocialAccount.connected_at.asc())).all()
        for account in accounts:
            if _provider_scope(account):
                continue
            latest = db.scalar(select(AnalyticsSyncRun).where(AnalyticsSyncRun.social_account_id == account.id).order_by(AnalyticsSyncRun.queued_at.desc()).limit(1))
            if latest is not None:
                if latest.status in {"queued", "running"}:
                    continue
                finished = latest.finished_at or latest.queued_at
                if finished and _aware(finished) > utcnow() - timedelta(seconds=interval):
                    continue
            try:
                _, created = queue_analytics_sync_run(db, account, start_date, end_date, trigger="scheduled")
                queued += int(created)
            except Exception:
                db.rollback()
                logger.warning("analytics_schedule_enqueue_failed account_id=%s", account.id)
    return queued


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class AnalyticsProviderError(Exception):
    def __init__(self, code: str, *, partial: bool = False):
        self.code = code
        self.partial = partial
        super().__init__(code)


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any], token: str) -> dict[str, Any]:
    try:
        response = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, follow_redirects=False)
    except httpx.HTTPError:
        raise AnalyticsProviderError("provider_unavailable") from None
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400 or not isinstance(payload, dict) or payload.get("error"):
        if response.status_code == 401:
            code = "provider_reauthorization_required"
        elif response.status_code == 403:
            code = "analytics_permission_or_api_access_missing"
        elif response.status_code == 429:
            code = "provider_quota_exceeded"
        else:
            code = "provider_request_failed"
        raise AnalyticsProviderError(code)
    return payload


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(value) if float(value).is_integer() else float(value)


def _save_snapshot(db: Session, account: SocialAccount, metric_date: date, source: str, metrics: dict[str, Any], *, entity_key: str = "account", metric_scope: str = "account", platform_media_id: str | None = None) -> bool:
    metrics = {str(key): _number(value) for key, value in metrics.items() if _number(value) is not None}
    if not metrics:
        return False
    row = db.scalar(select(AnalyticsSnapshot).where(
        AnalyticsSnapshot.social_account_id == account.id, AnalyticsSnapshot.metric_date == metric_date,
        AnalyticsSnapshot.metric_scope == metric_scope, AnalyticsSnapshot.entity_key == entity_key, AnalyticsSnapshot.source == source,
    ))
    if row is None:
        row = AnalyticsSnapshot(workspace_id=account.workspace_id, social_account_id=account.id, platform=account.platform, source=source,
                                metric_scope=metric_scope, entity_key=entity_key, platform_media_id=platform_media_id, metric_date=metric_date, metrics=metrics)
        db.add(row)
    else:
        row.metrics = metrics
        row.captured_at = utcnow()
        row.platform_media_id = platform_media_id
    return True


def _parse_youtube_report(payload: dict[str, Any]) -> list[tuple[date, dict[str, Any]]]:
    columns = payload.get("columnHeaders")
    rows = payload.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return []
    headers = [str(item.get("name", "")) if isinstance(item, dict) else "" for item in columns]
    output: list[tuple[date, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(headers):
            continue
        data = dict(zip(headers, row))
        try:
            metric_date = date.fromisoformat(str(data.get("day", ""))[:10])
        except ValueError:
            continue
        metrics: dict[str, Any] = {}
        for key in YT_METRICS:
            num = _number(data.get(key))
            if num is not None:
                metrics[key] = num
        if metrics:
            output.append((metric_date, metrics))
    return output


def _parse_instagram_metric(payload: dict[str, Any], requested_name: str, fallback_date: date) -> list[tuple[date, dict[str, Any]]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    output: dict[date, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or requested_name)
        values = item.get("values")
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, dict):
                    continue
                raw_end = value.get("end_time") or value.get("endTime")
                try:
                    metric_date = date.fromisoformat(str(raw_end)[:10]) if raw_end else fallback_date
                except ValueError:
                    metric_date = fallback_date
                num = _number(value.get("value"))
                if num is not None:
                    output.setdefault(metric_date, {})[name] = num
        total = item.get("total_value")
        if isinstance(total, dict):
            num = _number(total.get("value"))
            if num is not None:
                output.setdefault(fallback_date, {})[name] = num
        # Some provider versions return a direct scalar value instead of values/total_value.
        direct = _number(item.get("value"))
        if direct is not None:
            output.setdefault(fallback_date, {})[name] = direct
    return [(day, metrics) for day, metrics in sorted(output.items()) if metrics]


def _provider_integer(value: Any) -> int | None:
    """Parse count fields that YouTube Data API returns as decimal strings."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


async def _sync_youtube_media_snapshots(
    db: Session, client: httpx.AsyncClient, account: SocialAccount, token: str, snapshot_date: date, limit: int
) -> tuple[int, int, bool]:
    media_rows = db.scalars(select(MediaItem).where(
        MediaItem.workspace_id == account.workspace_id,
        MediaItem.social_account_id == account.id,
        MediaItem.platform == "youtube",
        MediaItem.source_type == "account_video",
    ).order_by(MediaItem.published_at.desc(), MediaItem.last_seen_at.desc()).limit(limit)).all()
    received = 0
    stored = 0
    partial = False
    # The YouTube Data API videos.list endpoint accepts at most 50 IDs per request.
    for index in range(0, len(media_rows), 50):
        batch = media_rows[index:index + 50]
        try:
            payload = await _get_json(
                client,
                "https://youtube.googleapis.com/youtube/v3/videos",
                {"part": "statistics", "id": ",".join(item.platform_media_id for item in batch)},
                token,
            )
        except AnalyticsProviderError:
            partial = True
            continue
        items = payload.get("items")
        by_id = {str(item.get("id")): item for item in items if isinstance(item, dict) and item.get("id")} if isinstance(items, list) else {}
        received += len(by_id)
        for media in batch:
            provider_item = by_id.get(media.platform_media_id)
            stats = provider_item.get("statistics") if isinstance(provider_item, dict) else None
            if not isinstance(stats, dict):
                partial = True
                continue
            metrics = {
                key: parsed for source_key, key in (("viewCount", "views"), ("likeCount", "likes"), ("commentCount", "comments"), ("favoriteCount", "favorites"))
                if (parsed := _provider_integer(stats.get(source_key))) is not None
            }
            if _save_snapshot(db, account, snapshot_date, "youtube_video_statistics", metrics,
                              entity_key=media.platform_media_id, metric_scope="media", platform_media_id=media.platform_media_id):
                stored += 1
            else:
                partial = True
    return received, stored, partial


async def _sync_instagram_media_snapshots(
    db: Session, client: httpx.AsyncClient, account: SocialAccount, token: str, snapshot_date: date, limit: int, api_version: str
) -> tuple[int, int, bool]:
    media_rows = db.scalars(select(MediaItem).where(
        MediaItem.workspace_id == account.workspace_id,
        MediaItem.social_account_id == account.id,
        MediaItem.platform == "instagram",
        MediaItem.source_type == "account_media",
    ).order_by(MediaItem.published_at.desc(), MediaItem.last_seen_at.desc()).limit(limit)).all()
    received = 0
    stored = 0
    partial = False
    metric_names = ("views", "reach", "likes", "comments", "saved", "shares", "total_interactions")
    for media in media_rows:
        endpoint = f"https://graph.instagram.com/{api_version}/{media.platform_media_id}/insights"
        metrics: dict[str, Any] = {}
        try:
            payload = await _get_json(client, endpoint, {"metric": ",".join(metric_names), "period": "lifetime"}, token)
            for _, values in _parse_instagram_metric(payload, "media_insights", snapshot_date):
                metrics.update(values)
        except AnalyticsProviderError:
            # Insights availability varies by media type and API version. Fall back to
            # individual metrics; don't fail the account-level daily report for one metric.
            metric_failures = 0
            for metric in metric_names:
                try:
                    payload = await _get_json(client, endpoint, {"metric": metric, "period": "lifetime"}, token)
                    for _, values in _parse_instagram_metric(payload, metric, snapshot_date):
                        metrics.update(values)
                except AnalyticsProviderError:
                    metric_failures += 1
            if metric_failures:
                partial = True
        if metrics:
            received += 1
            if _save_snapshot(db, account, snapshot_date, "instagram_media_insights", metrics,
                              entity_key=media.platform_media_id, metric_scope="media", platform_media_id=media.platform_media_id):
                stored += 1
        else:
            partial = True
    return received, stored, partial


async def process_analytics_sync_run(db: Session, run_id: uuid.UUID, account_id: uuid.UUID) -> None:
    run = db.get(AnalyticsSyncRun, run_id)
    if run is None:
        return
    if run.status not in {"queued", "running"}:
        return
    account = db.scalar(select(SocialAccount).where(SocialAccount.id == account_id).with_for_update())
    now = utcnow()
    if account is None or account.workspace_id != run.workspace_id:
        run.status = "error"; run.error_code = "account_not_found"; run.finished_at = now; db.commit(); return
    if account.status != "connected":
        run.status = "error"; run.error_code = "account_disconnected"; run.finished_at = now; db.commit(); return
    missing_scope = _provider_scope(account)
    if missing_scope:
        run.status = "error"; run.error_code = "analytics_scope_missing"; run.finished_at = now; db.commit(); return

    run.status = "running"; run.started_at = now; run.error_code = None; db.commit()
    settings = get_settings()
    try:
        expiry = _aware(account.access_token_expires_at) if account.access_token_expires_at else None
        if expiry is None or expiry <= utcnow() + timedelta(minutes=5):
            await refresh_social_account(account, settings)
            db.commit()
        token = decrypt_secret(account.access_token_encrypted, settings)
        if not token:
            raise AnalyticsProviderError("access_token_missing")
        timeout = max(5.0, min(float(settings.analytics_request_timeout_seconds), 60.0))
        rows: list[tuple[date, dict[str, Any]]] = []
        partial = False
        async with httpx.AsyncClient(timeout=timeout) as client:
            if account.platform == "youtube":
                url = "https://youtubeanalytics.googleapis.com/v2/reports"
                params = {
                    "ids": "channel==MINE", "startDate": run.period_start.isoformat(), "endDate": run.period_end.isoformat(),
                    "metrics": ",".join(YT_METRICS), "dimensions": "day", "sort": "day", "maxResults": str(min(366, (run.period_end - run.period_start).days + 1)),
                }
                payload = await _get_json(client, url, params, token)
                rows = _parse_youtube_report(payload)
            elif account.platform == "instagram":
                url = f"https://graph.instagram.com/{settings.meta_graph_api_version}/{account.platform_account_id}/insights"
                since = int(datetime.combine(run.period_start, time.min, tzinfo=timezone.utc).timestamp())
                until = int(datetime.combine(run.period_end + timedelta(days=1), time.min, tzinfo=timezone.utc).timestamp())
                by_date: dict[date, dict[str, Any]] = {}
                errors = 0
                for metric in IG_METRICS:
                    try:
                        payload = await _get_json(client, url, {"metric": metric, "period": "day", "since": str(since), "until": str(until)}, token)
                        for metric_date, values in _parse_instagram_metric(payload, metric, run.period_end):
                            by_date.setdefault(metric_date, {}).update(values)
                    except AnalyticsProviderError:
                        errors += 1
                if errors == len(IG_METRICS):
                    raise AnalyticsProviderError("instagram_insights_unavailable")
                partial = errors > 0
                rows = [(day, values) for day, values in sorted(by_date.items()) if values]
            else:
                raise AnalyticsProviderError("unsupported_platform")

        stored = 0
        for metric_date, metrics in rows:
            if metric_date < run.period_start or metric_date > run.period_end:
                continue
            if _save_snapshot(db, account, metric_date, "youtube_analytics" if account.platform == "youtube" else "instagram_insights", metrics):
                stored += 1
        # Per-video/post metrics complement account-level daily trends. These are
        # current snapshots, not fabricated daily history, and are capped per account.
        media_limit = max(1, min(int(settings.analytics_media_limit), 50))
        media_received = media_stored = 0
        try:
            async with httpx.AsyncClient(timeout=timeout) as media_client:
                if account.platform == "youtube":
                    media_received, media_stored, media_partial = await _sync_youtube_media_snapshots(
                        db, media_client, account, token, utcnow().date(), media_limit
                    )
                else:
                    media_received, media_stored, media_partial = await _sync_instagram_media_snapshots(
                        db, media_client, account, token, utcnow().date(), media_limit, settings.meta_graph_api_version
                    )
            partial = partial or media_partial
            stored += media_stored
        except (AnalyticsProviderError, httpx.HTTPError):
            partial = True
        run.rows_received = len(rows) + media_received
        run.rows_stored = stored
        run.status = "partial" if partial else "success"
        run.error_code = "some_metrics_unavailable" if partial else ("no_metrics_returned" if not rows else None)
        if not rows:
            run.status = "partial"
        run.finished_at = utcnow()
        db.commit()
    except AnalyticsProviderError as exc:
        db.rollback()
        fresh_run = db.get(AnalyticsSyncRun, run_id)
        if fresh_run:
            fresh_run.status = "error"
            fresh_run.error_code = exc.code
            fresh_run.finished_at = utcnow()
            db.commit()
    except (OAuthFlowError, HTTPException) as exc:
        db.rollback()
        fresh_run = db.get(AnalyticsSyncRun, run_id)
        if fresh_run:
            detail = str(getattr(exc, "detail", "provider_reauthorization_required")).lower()
            fresh_run.status = "error"
            fresh_run.error_code = "provider_reauthorization_required" if "reconnect" in detail or "refresh token" in detail else "token_refresh_failed"
            fresh_run.finished_at = utcnow()
            db.commit()
    except Exception:
        db.rollback()
        logger.exception("analytics_sync_unexpected_failure run_id=%s", run_id)
        fresh_run = db.get(AnalyticsSyncRun, run_id)
        if fresh_run:
            fresh_run.status = "error"
            fresh_run.error_code = "analytics_sync_failed"
            fresh_run.finished_at = utcnow()
            db.commit()
