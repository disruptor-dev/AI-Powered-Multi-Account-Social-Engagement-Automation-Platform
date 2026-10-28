from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import get_current_admin, require_csrf, require_workspace_role
from app.models.social_account import SocialAccount
from app.models.youtube_comment_queue import YouTubeCommentQueueItem
from app.schemas.youtube_comment_queue import YouTubeCommentDraftApproval, YouTubeHumanCommentCreate, YouTubeQueueOutcomeResolution, YouTubeQuotaResumeRequest
from app.services.comment_text_quality import video_comment_quality_warnings
from app.services.rate_limiting import rate_limit
from app.services.workspace_context import active_workspace
from app.services.team_management import audit_event
from app.services.comment_sync import ProviderSyncError, YOUTUBE_FORCE_SSL_SCOPE, get_youtube_video_details_for_comment_publish, parse_youtube_video_url
from app.services.youtube_comment_queue import get_queue_state, queue_item_response, utcnow

router = APIRouter(prefix="/api/v1/comments/youtube-queue", tags=["human-approved YouTube comment queue"])


def _normalized_text_fingerprint(value: str) -> str:
    # Normalize case, whitespace, punctuation, and emoji so cosmetic edits do not
    # bypass the duplicate-text guard. This is a duplicate safety check, not a
    # similarity detector; human review still decides whether text is appropriate.
    normalized = value.casefold()
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _provider_error(exc: ProviderSyncError) -> HTTPException:
    if exc.code == "provider_quota_exceeded":
        return HTTPException(status_code=503, detail="YouTube API quota was exceeded. The comment queue has been paused. Check the provider quota before resuming it.")
    if exc.reauthorization_required:
        return HTTPException(status_code=409, detail="Reconnect the selected YouTube channel and grant comment access.")
    if exc.code in {"youtube_video_unavailable", "comments_unavailable", "youtube_comments_unavailable"}:
        return HTTPException(status_code=422, detail="The target video is unavailable or does not accept comments.")
    if exc.code in {"provider_permission_denied", "comment_publish_scope_missing"}:
        return HTTPException(status_code=403, detail="The selected channel does not have permission to use this YouTube feature.")
    return HTTPException(status_code=503, detail="YouTube could not confirm the request. Review account/video access and try again later.")


@router.post("/generate", status_code=status.HTTP_410_GONE, summary="Retired: the human queue never generates AI comment text")
def reject_ai_generation_for_human_queue() -> None:
    """Keep the old endpoint safe for stale clients instead of silently generating AI text."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="AI text generation is disabled in the Human YouTube Comment Queue. Write the comment yourself and submit it using POST /api/v1/comments/youtube-queue.",
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Queue a human-written YouTube top-level comment for approval")
async def create_human_video_comment_queue_item(
    payload: YouTubeHumanCommentCreate,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("editor")),
    __email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
    __rate: None = Depends(rate_limit("youtube-comment-manual-create", limit_attr="rate_limit_social_action_limit", window_attr="rate_limit_social_action_window_seconds")),
) -> dict[str, Any]:
    """Save text explicitly entered by the user; no AI service or draft queue is invoked."""
    if not payload.confirm_one_channel_one_video:
        raise HTTPException(status_code=422, detail="Confirm the selected channel and unique target video before adding the human-written comment.")
    workspace = active_workspace(db)
    try:
        video_id = parse_youtube_video_url(payload.video_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    prior = db.scalar(select(YouTubeCommentQueueItem).where(
        YouTubeCommentQueueItem.workspace_id == workspace.id,
        YouTubeCommentQueueItem.idempotency_key == payload.idempotency_key,
    ))
    if prior is not None:
        same_request = (
            prior.draft_source == "human"
            and prior.social_account_id == payload.account_id
            and prior.target_video_id == video_id
            and (prior.draft_text or "") == payload.comment_text
        )
        if not same_request:
            raise HTTPException(status_code=409, detail="This idempotency key was already used for a different or legacy AI queue item.")
        response.headers["Cache-Control"] = "no-store"
        body = queue_item_response(db, prior)
        body["duplicate_request"] = True
        return body

    state = get_queue_state(db)
    if state.paused_at is not None:
        raise HTTPException(status_code=503, detail="YouTube comment publishing is paused after a quota error. An admin must confirm quota availability before adding a new item.")

    account = db.scalar(select(SocialAccount).where(
        SocialAccount.id == payload.account_id,
        SocialAccount.workspace_id == workspace.id,
    ))
    if account is None:
        raise HTTPException(status_code=404, detail="Selected YouTube channel was not found in this workspace.")
    if account.platform != "youtube":
        raise HTTPException(status_code=422, detail="Select one connected YouTube channel.")
    if account.status != "connected":
        raise HTTPException(status_code=409, detail="Reconnect the selected YouTube channel before adding a comment.")
    if YOUTUBE_FORCE_SSL_SCOPE not in (account.scopes or []):
        raise HTTPException(status_code=409, detail="Reconnect the selected channel and grant YouTube comment access.")

    active_statuses = ["queued_generation", "generating", "pending_approval", "scheduled", "publishing", "posted", "blocked_quota", "outcome_unknown"]
    existing_video = db.scalar(select(YouTubeCommentQueueItem).where(
        YouTubeCommentQueueItem.workspace_id == workspace.id,
        YouTubeCommentQueueItem.target_video_id == video_id,
        YouTubeCommentQueueItem.status.in_(active_statuses),
    ))
    if existing_video is not None:
        raise HTTPException(status_code=409, detail="This video is already assigned to a queue item in this workspace. Each target video must map to one channel and one comment item.")

    active_channel_statuses = ["queued_generation", "generating", "pending_approval", "scheduled", "publishing", "blocked_quota", "outcome_unknown"]
    existing_channel_item = db.scalar(select(YouTubeCommentQueueItem).where(
        YouTubeCommentQueueItem.workspace_id == workspace.id,
        YouTubeCommentQueueItem.social_account_id == account.id,
        YouTubeCommentQueueItem.status.in_(active_channel_statuses),
    ))
    if existing_channel_item is not None:
        raise HTTPException(status_code=409, detail="This channel already has an active queue item. Complete, cancel, or resolve it before assigning another video to that channel.")

    try:
        details = await get_youtube_video_details_for_comment_publish(account, video_id=video_id, settings=get_settings(), db=db)
    except ProviderSyncError as exc:
        if exc.code == "provider_quota_exceeded":
            state.paused_at = utcnow()
            state.pause_reason = "provider_quota_exceeded"
            state.updated_at = utcnow()
            db.commit()
        raise _provider_error(exc) from None

    human_text = payload.comment_text
    item = YouTubeCommentQueueItem(
        workspace_id=workspace.id,
        social_account_id=account.id,
        idempotency_key=payload.idempotency_key,
        status="pending_approval",
        draft_source="human",
        target_video_id=video_id,
        target_video_url=f"https://www.youtube.com/watch?v={video_id}",
        target_video_channel_id=details["channel_id"],
        target_video_title=details["title"],
        target_video_description=details.get("description") or "",
        target_video_thumbnail_url=details.get("thumbnail_url"),
        tone="manual",
        language="manual",
        custom_instructions=None,
        prompt_version="human-authored-v1",
        draft_text=human_text,
        quality_warnings=video_comment_quality_warnings(human_text),
    )
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(YouTubeCommentQueueItem).where(
            YouTubeCommentQueueItem.workspace_id == workspace.id,
            YouTubeCommentQueueItem.target_video_id == video_id,
            YouTubeCommentQueueItem.status.in_(active_statuses),
        ))
        if existing is not None:
            raise HTTPException(status_code=409, detail="This video is already assigned to a queue item in this workspace. Each target video must map to one channel and one comment item.") from None
        existing = db.scalar(select(YouTubeCommentQueueItem).where(
            YouTubeCommentQueueItem.workspace_id == workspace.id,
            YouTubeCommentQueueItem.idempotency_key == payload.idempotency_key,
        ))
        if existing is not None:
            raise HTTPException(status_code=409, detail="A duplicate manual-comment request was received. Refresh the queue and use the existing item.") from None
        raise HTTPException(status_code=409, detail="The target video or selected channel was reserved by a concurrent request. Refresh the queue and retry.") from None

    db.refresh(item)
    audit_event(
        db,
        workspace_id=workspace.id,
        actor_email=__email,
        actor_user_id=None,
        action="youtube_human_comment_queued",
        entity_type="youtube_comment_queue_item",
        entity_id=str(item.id),
        details={"account_id": str(account.id), "video_id": video_id, "draft_source": "human"},
    )
    db.commit()
    db.refresh(item)
    response.headers["Cache-Control"] = "no-store"
    body = queue_item_response(db, item)
    body["duplicate_request"] = False
    body["human_authored"] = True
    return body


@router.get("", summary="List the active workspace's top-level comment queue")
def list_video_comment_queue(
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = active_workspace(db)
    rows = db.scalars(select(YouTubeCommentQueueItem).where(
        YouTubeCommentQueueItem.workspace_id == workspace.id,
    ).order_by(YouTubeCommentQueueItem.created_at.desc()).limit(limit)).all()
    state = get_queue_state(db)
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [queue_item_response(db, item) for item in rows],
        "queue_paused": state.paused_at is not None,
        "pause_reason": state.pause_reason,
        "paused_at": state.paused_at.isoformat() if state.paused_at else None,
        "manual_review_required": True,
        "randomized_delays_enabled": False,
    }


@router.post("/{queue_item_id}/approve", summary="Approve exact text and schedule one YouTube comment")
def approve_video_comment(
    queue_item_id: uuid.UUID,
    payload: YouTubeCommentDraftApproval,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("editor")),
    approver_email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
    __rate: None = Depends(rate_limit("youtube-comment-approve", limit_attr="rate_limit_social_action_limit", window_attr="rate_limit_social_action_window_seconds")),
) -> dict[str, Any]:
    workspace = active_workspace(db)
    item = db.scalar(select(YouTubeCommentQueueItem).where(
        YouTubeCommentQueueItem.id == queue_item_id,
        YouTubeCommentQueueItem.workspace_id == workspace.id,
    ))
    if item is None:
        raise HTTPException(status_code=404, detail="Queue item not found")
    if item.draft_source != "human":
        raise HTTPException(status_code=409, detail="This item was not authored manually and cannot be approved in the Human YouTube Comment Queue. Create a new human-written comment.")
    if item.status not in {"pending_approval", "blocked_quota"}:
        raise HTTPException(status_code=409, detail=f"This item cannot be approved while its status is {item.status}.")
    if not payload.confirm_target_and_channel or not payload.confirm_relevance_and_nonspam:
        raise HTTPException(status_code=422, detail="Confirm the target channel/video and that this is a relevant, non-repetitive comment.")

    now = utcnow()
    scheduled_at = payload.scheduled_at.astimezone(timezone.utc)
    if scheduled_at < now + timedelta(minutes=1):
        raise HTTPException(status_code=422, detail="Choose a publication time at least one minute in the future. The platform does not add randomized delays.")
    if scheduled_at > now + timedelta(days=30):
        raise HTTPException(status_code=422, detail="Schedule no more than 30 days in advance.")

    warnings = video_comment_quality_warnings(payload.approved_text)
    blocking_warnings = [warning for warning in warnings if warning in {"generic_or_subscription_phrase", "excessive_emoji", "very_short_comment_review_required"}]
    if blocking_warnings:
        raise HTTPException(status_code=422, detail="Please revise the draft before approval: remove generic/subscription phrases, use at most one emoji, and provide a specific meaningful comment.")

    account = db.scalar(select(SocialAccount).where(
        SocialAccount.id == item.social_account_id,
        SocialAccount.workspace_id == workspace.id,
    ))
    if account is None or account.platform != "youtube" or account.status != "connected" or YOUTUBE_FORCE_SSL_SCOPE not in (account.scopes or []):
        raise HTTPException(status_code=409, detail="Reconnect the selected YouTube channel and verify comment permissions before scheduling.")

    # Atomic state transition prevents two concurrent approvals from overwriting
    # the reviewed text or silently moving an already-scheduled item.
    try:
        claimed = db.execute(update(YouTubeCommentQueueItem).where(
            YouTubeCommentQueueItem.id == item.id,
            YouTubeCommentQueueItem.workspace_id == workspace.id,
            YouTubeCommentQueueItem.status.in_(["pending_approval", "blocked_quota"]),
        ).values(
            approved_text=payload.approved_text,
            text_fingerprint=_normalized_text_fingerprint(payload.approved_text),
            scheduled_at=scheduled_at,
            approved_at=now,
            approved_by_email=approver_email[:320],
            status="scheduled",
            error_code=None,
            finished_at=None,
            updated_at=now,
        ))
        if claimed.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=409, detail="This queue item was changed by another request. Refresh the queue and review its current state.")
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="This comment text or target video is already queued/posted in this workspace. Use a genuinely distinct comment or review the existing queue item.") from None
    db.refresh(item)
    response.headers["Cache-Control"] = "no-store"
    return queue_item_response(db, item)


@router.post("/{queue_item_id}/cancel", summary="Cancel an unpublished queue item")
def cancel_video_comment(
    queue_item_id: uuid.UUID,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("editor")),
    __: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = active_workspace(db)
    item = db.scalar(select(YouTubeCommentQueueItem).where(
        YouTubeCommentQueueItem.id == queue_item_id,
        YouTubeCommentQueueItem.workspace_id == workspace.id,
    ))
    if item is None:
        raise HTTPException(status_code=404, detail="Queue item not found")
    if item.status not in {"queued_generation", "generating", "pending_approval", "scheduled", "blocked_quota", "failed"}:
        raise HTTPException(status_code=409, detail="Posted or outcome-unknown comments cannot be cancelled. Check YouTube before taking further action.")
    now = utcnow()
    cancelled = db.execute(update(YouTubeCommentQueueItem).where(
        YouTubeCommentQueueItem.id == item.id,
        YouTubeCommentQueueItem.workspace_id == workspace.id,
        YouTubeCommentQueueItem.status.in_(["queued_generation", "generating", "pending_approval", "scheduled", "blocked_quota", "failed"]),
    ).values(status="cancelled", cancelled_at=now, finished_at=now, updated_at=now))
    if cancelled.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="This item has already started publishing or has changed. Refresh the queue before trying again.")
    db.commit()
    db.refresh(item)
    response.headers["Cache-Control"] = "no-store"
    return queue_item_response(db, item)


@router.post("/{queue_item_id}/resolve-outcome", summary="Resolve an uncertain YouTube write after a human checks YouTube")
def resolve_unknown_video_comment_outcome(
    queue_item_id: uuid.UUID,
    payload: YouTubeQueueOutcomeResolution,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("admin")),
    admin_email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = active_workspace(db)
    item = db.scalar(select(YouTubeCommentQueueItem).where(
        YouTubeCommentQueueItem.id == queue_item_id,
        YouTubeCommentQueueItem.workspace_id == workspace.id,
    ))
    if item is None:
        raise HTTPException(status_code=404, detail="Queue item not found")
    if item.status != "outcome_unknown":
        raise HTTPException(status_code=409, detail="Only items with an uncertain provider outcome can be resolved this way.")
    if not payload.confirm_checked_youtube:
        raise HTTPException(status_code=422, detail="Confirm that you checked the selected channel's comments directly on YouTube.")

    now = utcnow()
    values: dict[str, Any] = {"finished_at": now, "updated_at": now}
    if payload.outcome == "posted":
        values.update({
            "status": "posted",
            "provider_comment_id": payload.provider_comment_id,
            "published_at": item.published_at or now,
            "error_code": None,
        })
    else:
        # Only an admin who has checked YouTube can release the item. The explicit
        # resolution is audited so the next queue action is attributable.
        values.update({
            "status": "failed",
            "provider_comment_id": None,
            "published_at": None,
            "error_code": "manual_verified_not_posted",
        })
    changed = db.execute(update(YouTubeCommentQueueItem).where(
        YouTubeCommentQueueItem.id == item.id,
        YouTubeCommentQueueItem.workspace_id == workspace.id,
        YouTubeCommentQueueItem.status == "outcome_unknown",
    ).values(**values).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="This item was resolved by another request. Refresh the queue.")
    audit_event(
        db,
        workspace_id=workspace.id,
        actor_email=admin_email,
        actor_user_id=None,
        action="youtube_comment_outcome_resolved",
        entity_type="youtube_comment_queue_item",
        entity_id=str(item.id),
        details={"outcome": payload.outcome, "provider_comment_id_supplied": bool(payload.provider_comment_id), "resolution_note": payload.resolution_note[:500]},
    )
    db.commit()
    resolved = db.get(YouTubeCommentQueueItem, item.id)
    response.headers["Cache-Control"] = "no-store"
    return queue_item_response(db, resolved or item)


@router.post("/quota/resume", summary="Resume scheduled writes after an admin verifies the YouTube quota reset")
def resume_after_quota_reset(
    payload: YouTubeQuotaResumeRequest,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("admin")),
    admin_email: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not payload.confirm_quota_checked:
        raise HTTPException(status_code=422, detail="Confirm that you checked the YouTube API quota and it is available before resuming.")
    state = get_queue_state(db)
    state.paused_at = None
    state.pause_reason = None
    state.updated_at = utcnow()
    state.updated_by_email = admin_email[:320]
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"queue_paused": False, "resumed_by": admin_email, "resumed_at": state.updated_at.isoformat(), "note": "Items marked blocked_quota still require human review and a new explicit schedule."}
