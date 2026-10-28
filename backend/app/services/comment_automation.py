from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.redis_client import get_redis_client
from app.models.ai_reply_draft import AIReplyDraft
from app.models.comment import SocialComment
from app.models.comment_automation_rule import CommentAutomationExecution, CommentAutomationRule, CommentAutomationRun
from app.models.media_item import MediaItem
from app.models.social_account import SocialAccount
from app.services.ai_drafting import AIDraftQueueUnavailable, context_fingerprint, queue_ai_reply_draft
from app.services.comment_jobs import QUEUE_NAME

logger = logging.getLogger(__name__)


class AutomationQueueUnavailable(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def queue_automation_run(db: Session, run: CommentAutomationRun, redis_client=None) -> None:
    client = redis_client or get_redis_client()
    run.last_enqueued_at = utcnow()
    db.commit()
    try:
        client.lpush(QUEUE_NAME, json.dumps({"type": "comment_automation_run", "run_id": str(run.id), "workspace_id": str(run.workspace_id)}, separators=(",", ":")))
    except (RedisError, OSError, ValueError):
        run.status = "error"
        run.error_code = "automation_queue_unavailable"
        run.finished_at = utcnow()
        run.last_enqueued_at = None
        db.commit()
        raise AutomationQueueUnavailable from None


def _contains_any(text: str, values: list[str]) -> bool:
    lowered = text.casefold()
    return not values or any(value.casefold() in lowered for value in values)


def matches_rule(comment: SocialComment, media: MediaItem, account: SocialAccount, conditions: dict[str, Any]) -> bool:
    platform = conditions.get("platform", "all")
    if platform != "all" and comment.platform != platform:
        return False
    account_ids = conditions.get("account_ids") or []
    if account_ids and str(account.id) not in {str(value) for value in account_ids}:
        return False
    include_replies = bool(conditions.get("include_replies", False))
    if comment.is_reply and not include_replies:
        return False
    if conditions.get("exclude_owner_comments", True) and comment.is_owner_comment:
        return False
    if conditions.get("only_open", True) and comment.workflow_status != "open":
        return False
    if conditions.get("only_unanswered", True) and (comment.has_owner_reply or comment.is_owner_comment):
        return False
    if comment.like_count < int(conditions.get("min_likes", 0) or 0):
        return False
    text = comment.text or ""
    any_keywords = [str(v) for v in conditions.get("keywords_any", [])]
    all_keywords = [str(v) for v in conditions.get("keywords_all", [])]
    excluded = [str(v) for v in conditions.get("exclude_keywords", [])]
    if any_keywords and not _contains_any(text, any_keywords):
        return False
    lower = text.casefold()
    if any(value.casefold() not in lower for value in all_keywords):
        return False
    if any(value.casefold() in lower for value in excluded):
        return False
    author = str(comment.author_name or "").casefold()
    author_contains = conditions.get("author_contains")
    if author_contains and str(author_contains).casefold() not in author:
        return False
    media_type = conditions.get("media_type", "all")
    if media_type != "all" and media.media_type != media_type:
        return False
    # Keep the account argument in the function contract so future conditions can be
    # extended without changing the executor; current account filter is by account UUID.
    return True


def _draft_for_rule(db: Session, rule: CommentAutomationRule, comment: SocialComment, media: MediaItem) -> AIReplyDraft:
    cfg = rule.draft_settings or {}
    key = f"rule:{rule.id.hex}:{comment.id.hex}"
    existing = db.scalar(select(AIReplyDraft).where(AIReplyDraft.workspace_id == rule.workspace_id, AIReplyDraft.idempotency_key == key))
    if existing is not None:
        return existing
    settings = get_settings()
    draft = AIReplyDraft(
        workspace_id=rule.workspace_id,
        comment_id=comment.id,
        target_account_id=comment.social_account_id,
        automation_rule_id=rule.id,
        idempotency_key=key,
        status="queued",
        tone=str(cfg.get("tone", "helpful")),
        language=str(cfg.get("language", "match_comment")),
        custom_instructions=cfg.get("custom_instructions"),
        provider="openai_compatible",
        model=settings.ai_model.strip()[:160] or "unconfigured",
        prompt_version="reply-v1",
        context_fingerprint=context_fingerprint(comment.text, media.title, comment.platform),
    )
    db.add(draft)
    db.flush()
    return draft


def process_comment_automation_run(db: Session, run_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    run = db.scalar(select(CommentAutomationRun).where(CommentAutomationRun.id == run_id, CommentAutomationRun.workspace_id == workspace_id))
    if run is None or run.status not in {"queued", "running"}:
        return
    rule = db.scalar(select(CommentAutomationRule).where(
        CommentAutomationRule.id == run.rule_id,
        CommentAutomationRule.workspace_id == workspace_id,
    )) if run.rule_id else None
    if rule is None or not rule.enabled:
        run.status = "error"
        run.error_code = "automation_rule_unavailable"
        run.finished_at = utcnow()
        db.commit()
        return
    if not get_settings().ai_drafting_configured:
        run.status = "error"
        run.error_code = "ai_not_configured"
        run.finished_at = utcnow()
        db.commit()
        return

    run.status = "running"
    run.started_at = utcnow()
    run.error_code = None
    db.commit()
    settings = get_settings()
    limit = max(25, min(settings.automation_rule_lookback_limit, 2000))
    draft_cap = max(1, min(settings.ai_draft_max_comments_per_rule_run, 2000))
    rows = db.execute(
        select(SocialComment, MediaItem, SocialAccount)
        .join(MediaItem, MediaItem.id == SocialComment.media_item_id)
        .join(SocialAccount, SocialAccount.id == SocialComment.social_account_id)
        .where(
            SocialComment.workspace_id == workspace_id,
            MediaItem.workspace_id == workspace_id,
            SocialAccount.workspace_id == workspace_id,
            SocialAccount.status == "connected",
        )
        .order_by(SocialComment.published_at.desc().nullslast(), SocialComment.id.asc())
        .limit(limit)
    ).all()
    run.scanned_count = len(rows)
    db.commit()
    try:
        for comment, media, account in rows:
            if not matches_rule(comment, media, account, rule.conditions or {}):
                continue
            prior = db.scalar(select(CommentAutomationExecution).where(
                CommentAutomationExecution.rule_id == rule.id,
                CommentAutomationExecution.comment_id == comment.id,
            ))
            if prior is not None and prior.status != "failed":
                run.skipped_count += 1
                continue
            if run.drafts_queued_count + run.failed_count >= draft_cap:
                run.skipped_count += 1
                continue
            run.matched_count += 1
            execution = prior or CommentAutomationExecution(
                workspace_id=workspace_id, rule_id=rule.id, comment_id=comment.id,
                status="draft_queued", error_code=None,
            )
            try:
                draft = _draft_for_rule(db, rule, comment, media)
                if draft.status == "failed":
                    draft.status = "queued"
                    draft.error_code = None
                    draft.last_enqueued_at = None
                execution.ai_draft_id = draft.id
                execution.status = "draft_queued"
                execution.error_code = None
                db.add(execution)
                db.commit()
                if draft.status == "queued":
                    queue_ai_reply_draft(db, draft)
                if draft.status == "failed":
                    execution.status = "failed"
                    execution.error_code = draft.error_code or "ai_draft_queue_failed"
                    run.failed_count += 1
                else:
                    run.drafts_queued_count += 1
                db.commit()
            except AIDraftQueueUnavailable:
                db.rollback()
                existing = db.scalar(select(CommentAutomationExecution).where(
                    CommentAutomationExecution.rule_id == rule.id,
                    CommentAutomationExecution.comment_id == comment.id,
                ))
                if existing is not None:
                    existing.status = "failed"
                    existing.error_code = "ai_queue_unavailable"
                    db.commit()
                run.failed_count += 1
                logger.warning("Could not queue AI draft for rule_id=%s comment_id=%s", rule.id, comment.id)
            except IntegrityError:
                db.rollback()
                # Duplicate rule deliveries can race on the unique rule/comment key.
                existing = db.scalar(select(CommentAutomationExecution).where(
                    CommentAutomationExecution.rule_id == rule.id,
                    CommentAutomationExecution.comment_id == comment.id,
                ))
                if existing is not None:
                    run.skipped_count += 1
                else:
                    run.failed_count += 1
                    logger.warning("Could not persist rule execution for rule_id=%s comment_id=%s", rule.id, comment.id)
            except Exception:
                db.rollback()
                run.failed_count += 1
                logger.exception("Rule execution failed for rule_id=%s comment_id=%s", rule.id, comment.id)
            db.add(run)
            db.commit()
        rule.last_run_at = utcnow()
        run.status = "partial" if run.failed_count else "success"
        run.finished_at = utcnow()
        db.commit()
    except Exception:
        db.rollback()
        current = db.get(CommentAutomationRun, run_id)
        if current is not None:
            current.status = "error"
            current.error_code = "automation_internal_error"
            current.finished_at = utcnow()
            db.commit()
        logger.exception("Automation run failed unexpectedly id=%s", run_id)


def enqueue_due_automation_rule_runs(*, redis_client=None, now: datetime | None = None, limit: int = 20) -> int:
    settings = get_settings()
    if not settings.ai_drafting_configured:
        return 0
    now = now or utcnow()
    client = redis_client or get_redis_client()
    db = SessionLocal()
    queued = 0
    try:
        # Re-deliver old queued runs if Redis lost a message or the worker was restarted.
        stale_cutoff = now - timedelta(seconds=180)
        stale_runs = db.scalars(select(CommentAutomationRun).where(
            CommentAutomationRun.status == "queued",
            (CommentAutomationRun.last_enqueued_at.is_(None) | (CommentAutomationRun.last_enqueued_at < stale_cutoff)),
        ).order_by(CommentAutomationRun.queued_at.asc()).limit(limit)).all()
        for stale_run in stale_runs:
            stale_run.last_enqueued_at = now
            db.commit()
            try:
                client.lpush(QUEUE_NAME, json.dumps({"type": "comment_automation_run", "run_id": str(stale_run.id), "workspace_id": str(stale_run.workspace_id)}, separators=(",", ":")))
                queued += 1
            except (RedisError, OSError, ValueError):
                stale_run.last_enqueued_at = None
                db.commit()
                return queued
        interval = max(60, min(settings.automation_rule_interval_seconds, 86400))
        due_before = now - timedelta(seconds=interval)
        rules = db.scalars(select(CommentAutomationRule).where(
            CommentAutomationRule.enabled.is_(True),
            (CommentAutomationRule.last_scheduled_at.is_(None) | (CommentAutomationRule.last_scheduled_at < due_before)),
        ).order_by(CommentAutomationRule.priority.asc(), CommentAutomationRule.created_at.asc()).limit(limit)).all()
        for rule in rules:
            run = CommentAutomationRun(workspace_id=rule.workspace_id, rule_id=rule.id, trigger="scheduled", status="queued")
            db.add(run)
            rule.last_scheduled_at = now
            db.commit()
            try:
                client.lpush(QUEUE_NAME, json.dumps({"type": "comment_automation_run", "run_id": str(run.id), "workspace_id": str(run.workspace_id)}, separators=(",", ":")))
                run.last_enqueued_at = now
                db.commit()
                queued += 1
            except (RedisError, OSError, ValueError):
                run.status = "error"
                run.error_code = "automation_queue_unavailable"
                run.finished_at = now
                rule.last_scheduled_at = None
                db.commit()
                logger.warning("Could not enqueue scheduled automation rule id=%s", rule.id)
        return queued
    finally:
        db.close()
