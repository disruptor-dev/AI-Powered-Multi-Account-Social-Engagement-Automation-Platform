from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import get_current_admin, require_csrf, require_workspace_role
from app.models.comment import SocialComment
from app.models.ai_reply_draft import AIReplyDraft
from app.models.comment_publish_batch import CommentPublishBatch
from app.models.comment_sync_run import CommentSyncRun
from app.models.media_item import MediaItem
from app.models.social_account import SocialAccount
from app.services.workspace_context import active_workspace
from app.services.rate_limiting import rate_limit
from app.services.comment_jobs import SyncQueueUnavailable, queue_sync_run
from app.services.comment_publishing import batch_response as publish_batch_response
from app.schemas.comments import (
    CommentSyncRequest, CommentWorkflowStatusUpdate, ExternalVideoSyncRequest, MultiChannelPublishRequest, PublishCommentReplyRequest,
)
from app.schemas.phase4 import AIReplyDraftRequest, AIReplyDraftUpdate
from app.services.comment_sync import ProviderSyncError, YOUTUBE_FORCE_SSL_SCOPE, parse_youtube_video_url, publish_youtube_reply
from app.services.ai_drafting import AIDraftQueueUnavailable, AIDraftingError, context_fingerprint, draft_response, queue_ai_reply_draft

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/comments", tags=["unified comments inbox"])


class Platform(str, Enum):
    youtube = "youtube"
    instagram = "instagram"


class WorkflowFilter(str, Enum):
    all = "all"
    open = "open"
    resolved = "resolved"


class ResponseFilter(str, Enum):
    all = "all"
    unanswered = "unanswered"
    replied = "replied"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _iso(value: datetime | None) -> str | None:
    value = _utc(value)
    return value.isoformat() if value else None


def _owner_workspace(db: Session) -> Any:
    # Resolves the request's signed active-workspace context. The helper retains its
    # historic name to avoid churn across the Phase 3–5 route handlers.
    return active_workspace(db)


def _comment_item(comment: SocialComment, media: MediaItem, account: SocialAccount, parent: SocialComment | None) -> dict[str, Any]:
    return {
        "id": str(comment.id),
        "platform": comment.platform,
        "account_id": str(account.id),
        "account_name": account.display_name,
        "account_username": account.username,
        "account_status": account.status,
        "platform_comment_id": comment.platform_comment_id,
        "parent_platform_comment_id": comment.parent_platform_comment_id,
        "parent_comment_id": str(comment.parent_comment_id) if comment.parent_comment_id else None,
        "parent_author_name": parent.author_name if parent else None,
        "author_id": comment.author_id,
        "author_name": comment.author_name,
        "author_username": comment.author_username,
        "author_avatar_url": comment.author_avatar_url,
        "text": comment.text,
        "like_count": comment.like_count,
        "reply_count": comment.reply_count,
        "is_reply": comment.is_reply,
        "is_owner_comment": comment.is_owner_comment,
        "has_owner_reply": comment.has_owner_reply,
        "workflow_status": comment.workflow_status,
        "published_at": _iso(comment.published_at),
        "platform_updated_at": _iso(comment.platform_updated_at),
        "media": {
            "id": str(media.id),
            "platform_media_id": media.platform_media_id,
            "type": media.media_type,
            "source_type": media.source_type,
            "title": media.title or "Untitled post",
            "description": media.description,
            "permalink": media.permalink,
            "thumbnail_url": media.thumbnail_url,
            "published_at": _iso(media.published_at),
        },
        # Final permission is rechecked by YouTube when the user submits a reply.
        "can_reply": comment.platform == "youtube" and not comment.is_reply,
        "reply_permission_ready": account.status == "connected" and (
            "instagram_business_manage_comments" in (account.scopes or [])
            if comment.platform == "instagram"
            else "https://www.googleapis.com/auth/youtube.force-ssl" in (account.scopes or [])
        ),
    }


def _base_comment_query(workspace_id: uuid.UUID):
    return (
        select(SocialComment, MediaItem, SocialAccount)
        .join(MediaItem, SocialComment.media_item_id == MediaItem.id)
        .join(SocialAccount, SocialComment.social_account_id == SocialAccount.id)
        .where(
            SocialComment.workspace_id == workspace_id,
            MediaItem.workspace_id == workspace_id,
            SocialAccount.workspace_id == workspace_id,
            SocialAccount.status != "disconnected",
        )
    )


@router.get("", summary="List comments across all connected accounts")
def list_comments(
    response: Response,
    platform: Platform | None = Query(default=None),
    account_id: uuid.UUID | None = Query(default=None),
    query: str | None = Query(default=None, max_length=300),
    workflow_status: WorkflowFilter = Query(default=WorkflowFilter.open),
    response_status: ResponseFilter = Query(default=ResponseFilter.all),
    include_replies: bool = Query(default=False),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    sort: str = Query(default="newest", pattern="^(newest|oldest)$"),
    page: int = Query(default=1, ge=1, le=100000),
    page_size: int = Query(default=25, ge=1, le=100),
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _owner_workspace(db)
    statement = _base_comment_query(workspace.id)
    count_statement = select(func.count(SocialComment.id)).select_from(SocialComment).join(
        MediaItem, SocialComment.media_item_id == MediaItem.id
    ).join(SocialAccount, SocialComment.social_account_id == SocialAccount.id).where(
        SocialComment.workspace_id == workspace.id,
        MediaItem.workspace_id == workspace.id,
        SocialAccount.workspace_id == workspace.id,
        SocialAccount.status != "disconnected",
    )
    filters = []
    if platform:
        filters.append(SocialComment.platform == platform.value)
    if account_id:
        filters.append(SocialComment.social_account_id == account_id)
    if workflow_status != WorkflowFilter.all:
        filters.append(SocialComment.workflow_status == workflow_status.value)
    if not include_replies:
        filters.append(SocialComment.is_reply.is_(False))
    if response_status == ResponseFilter.unanswered:
        filters.extend([SocialComment.is_reply.is_(False), SocialComment.has_owner_reply.is_(False), SocialComment.is_owner_comment.is_(False)])
    elif response_status == ResponseFilter.replied:
        filters.append(or_(SocialComment.has_owner_reply.is_(True), SocialComment.is_owner_comment.is_(True)))
    if since:
        filters.append(SocialComment.published_at >= since)
    if until:
        filters.append(SocialComment.published_at <= until)
    if query and query.strip():
        term_value = query.strip().replace("%", "\\%").replace("_", "\\_")
        term = f"%{term_value}%"
        filters.append(or_(
            SocialComment.text.ilike(term, escape="\\"),
            SocialComment.author_name.ilike(term, escape="\\"),
            SocialComment.author_username.ilike(term, escape="\\"),
            MediaItem.title.ilike(term, escape="\\"),
            SocialAccount.display_name.ilike(term, escape="\\"),
        ))
    if filters:
        statement = statement.where(*filters)
        count_statement = count_statement.where(*filters)

    total = int(db.scalar(count_statement) or 0)
    ordering = SocialComment.published_at.asc().nulls_last() if sort == "oldest" else SocialComment.published_at.desc().nulls_last()
    rows = db.execute(statement.order_by(ordering, SocialComment.id.asc()).offset((page - 1) * page_size).limit(page_size)).all()
    parent_ids = {comment.parent_comment_id for comment, _, _ in rows if comment.parent_comment_id}
    parents: dict[uuid.UUID, SocialComment] = {}
    if parent_ids:
        parents = {parent.id: parent for parent in db.scalars(select(SocialComment).where(
            SocialComment.workspace_id == workspace.id,
            SocialComment.id.in_(parent_ids),
        )).all()}
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [_comment_item(comment, media, account, parents.get(comment.parent_comment_id)) for comment, media, account in rows],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": (total + page_size - 1) // page_size,
            "has_more": page * page_size < total,
        },
        "filters": {
            "platform": platform.value if platform else "all",
            "account_id": str(account_id) if account_id else "all",
            "workflow_status": workflow_status.value,
            "response_status": response_status.value,
            "include_replies": include_replies,
        },
    }


@router.get("/summary", summary="Get unified inbox counts")
def comments_summary(
    response: Response,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _owner_workspace(db)
    visible = and_(
        SocialComment.workspace_id == workspace.id,
        SocialAccount.workspace_id == workspace.id,
        SocialAccount.status != "disconnected",
    )
    root_comments = SocialComment.is_reply.is_(False)
    open_count = int(db.scalar(select(func.count(SocialComment.id)).join(
        SocialAccount, SocialAccount.id == SocialComment.social_account_id
    ).where(visible, root_comments, SocialComment.workflow_status == "open")) or 0)
    unanswered = int(db.scalar(select(func.count(SocialComment.id)).join(
        SocialAccount, SocialAccount.id == SocialComment.social_account_id
    ).where(visible, root_comments, SocialComment.workflow_status == "open", SocialComment.has_owner_reply.is_(False), SocialComment.is_owner_comment.is_(False))) or 0)
    resolved = int(db.scalar(select(func.count(SocialComment.id)).join(
        SocialAccount, SocialAccount.id == SocialComment.social_account_id
    ).where(visible, root_comments, SocialComment.workflow_status == "resolved")) or 0)
    all_comments = int(db.scalar(select(func.count(SocialComment.id)).join(
        SocialAccount, SocialAccount.id == SocialComment.social_account_id
    ).where(visible)) or 0)
    needs_attention = int(db.scalar(select(func.count(SocialAccount.id)).where(
        SocialAccount.workspace_id == workspace.id,
        SocialAccount.status.in_(["reauthorization_required"]),
    )) or 0)
    latest_sync = db.scalar(select(func.max(SocialAccount.last_sync_completed_at)).where(
        SocialAccount.workspace_id == workspace.id,
        SocialAccount.status != "disconnected",
    ))
    response.headers["Cache-Control"] = "no-store"
    return {
        "open_comments": open_count,
        "unanswered_comments": unanswered,
        "resolved_comments": resolved,
        "all_comments": all_comments,
        "accounts_needing_attention": needs_attention,
        "last_sync_at": _iso(latest_sync),
    }


@router.get("/sync-status", summary="Get per-account synchronization status")
def sync_status(
    response: Response,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _owner_workspace(db)
    accounts = db.scalars(select(SocialAccount).where(
        SocialAccount.workspace_id == workspace.id,
        SocialAccount.status != "disconnected",
    ).order_by(SocialAccount.platform.asc(), SocialAccount.display_name.asc())).all()
    latest_runs = db.scalars(select(CommentSyncRun).where(
        CommentSyncRun.workspace_id == workspace.id,
    ).order_by(CommentSyncRun.queued_at.desc()).limit(15)).all()
    response.headers["Cache-Control"] = "no-store"
    return {
        "accounts": [{
            "id": str(account.id),
            "platform": account.platform,
            "display_name": account.display_name,
            "username": account.username,
            "status": account.status,
            "can_publish_replies": account.platform == "youtube" and account.status == "connected" and YOUTUBE_FORCE_SSL_SCOPE in (account.scopes or []),
            "last_sync_status": account.last_sync_status,
            "last_sync_started_at": _iso(account.last_sync_started_at),
            "last_sync_completed_at": _iso(account.last_sync_completed_at),
            "last_sync_error_code": account.last_sync_error_code,
            "last_sync_media_count": account.last_sync_media_count,
            "last_sync_comment_count": account.last_sync_comment_count,
        } for account in accounts],
        "recent_runs": [{
            "id": str(run.id),
            "account_id": str(run.social_account_id),
            "trigger": run.trigger,
            "status": run.status,
            "queued_at": _iso(run.queued_at),
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
            "media_processed": run.media_processed,
            "comments_inserted": run.comments_inserted,
            "comments_updated": run.comments_updated,
            "comments_skipped": run.comments_skipped,
            "error_code": run.error_code,
            "target_platform_media_id": run.target_platform_media_id,
        } for run in latest_runs],
    }


@router.post("/sync", summary="Queue comment synchronization")
def request_comment_sync(
    payload: CommentSyncRequest,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("editor")),
    __: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
    __rate: None = Depends(rate_limit("comment-sync", limit_attr="rate_limit_social_action_limit", window_attr="rate_limit_social_action_window_seconds")),
) -> dict[str, Any]:
    workspace = _owner_workspace(db)
    statement = select(SocialAccount).where(
        SocialAccount.workspace_id == workspace.id,
        SocialAccount.status == "connected",
    )
    if payload.account_id:
        statement = statement.where(SocialAccount.id == payload.account_id)
    accounts = db.scalars(statement.order_by(SocialAccount.platform.asc(), SocialAccount.display_name.asc())).all()
    if payload.account_id and not accounts:
        account = db.scalar(select(SocialAccount).where(
            SocialAccount.id == payload.account_id,
            SocialAccount.workspace_id == workspace.id,
        ))
        if account is None:
            raise HTTPException(status_code=404, detail="Connected account not found")
        raise HTTPException(status_code=409, detail="Reconnect this account before synchronizing comments")

    results = []
    queued_count = 0
    try:
        for account in accounts:
            run, queued = queue_sync_run(db, account, trigger="manual")
            queued_count += int(queued)
            results.append({"account_id": str(account.id), "account_name": account.display_name, "run_id": str(run.id), "status": run.status, "new_job_queued": queued})
    except SyncQueueUnavailable:
        raise HTTPException(status_code=503, detail="The background queue is unavailable. Try again shortly.") from None
    response.status_code = status.HTTP_202_ACCEPTED if queued_count else status.HTTP_200_OK
    response.headers["Cache-Control"] = "no-store"
    return {"status": "queued" if queued_count else "already_running" if results else "no_accounts", "queued": queued_count, "items": results}


@router.patch("/{comment_id}/status", summary="Resolve or reopen an inbox comment")
def update_comment_status(
    comment_id: uuid.UUID,
    payload: CommentWorkflowStatusUpdate,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("editor")),
    __: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    workspace = _owner_workspace(db)
    comment = db.scalar(select(SocialComment).where(
        SocialComment.id == comment_id,
        SocialComment.workspace_id == workspace.id,
    ))
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if payload.workflow_status == "resolved":
        if comment.workflow_status != "resolved" or comment.resolved_at is None:
            comment.resolved_at = datetime.now(timezone.utc)
    else:
        comment.resolved_at = None
    comment.workflow_status = payload.workflow_status
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"id": str(comment.id), "workflow_status": comment.workflow_status}



@router.get("/ai/status", summary="Report AI draft service readiness without exposing the API key")
def ai_draft_status(
    response: Response,
    _: str = Depends(get_current_admin),
) -> dict[str, Any]:
    settings = get_settings()
    response.headers["Cache-Control"] = "no-store"
    return {
        "configured": settings.ai_drafting_configured,
        "provider": "openai_compatible" if settings.ai_drafting_configured else None,
        "model": settings.ai_model.strip() if settings.ai_drafting_configured else None,
        "manual_review_required": True,
        "auto_publish_enabled": False,
    }


@router.get("/ai/summary", summary="Get AI draft counts for the owner workspace")
def ai_draft_summary(
    response: Response,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _owner_workspace(db)
    counts = {
        key: int(db.scalar(select(func.count(AIReplyDraft.id)).where(
            AIReplyDraft.workspace_id == workspace.id, AIReplyDraft.status == key,
        )) or 0)
        for key in ("queued", "generating", "draft", "approved", "rejected", "published", "failed")
    }
    total = int(db.scalar(select(func.count(AIReplyDraft.id)).where(AIReplyDraft.workspace_id == workspace.id)) or 0)
    response.headers["Cache-Control"] = "no-store"
    return {"total": total, "pending_review": counts["draft"] + counts["approved"], "active": counts["queued"] + counts["generating"], "by_status": counts, "manual_review_required": True}


@router.get("/ai-drafts", summary="List persisted AI drafts for the current inbox page")
def list_ai_drafts(
    response: Response,
    comment_ids: list[uuid.UUID] = Query(default=[]),
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if len(comment_ids) > 50:
        raise HTTPException(status_code=422, detail="Request at most 50 comment IDs at a time")
    workspace = _owner_workspace(db)
    if not comment_ids:
        return {"items": []}
    rows = db.scalars(select(AIReplyDraft).where(
        AIReplyDraft.workspace_id == workspace.id,
        AIReplyDraft.comment_id.in_(set(comment_ids)),
    ).order_by(AIReplyDraft.created_at.desc()).limit(250)).all()
    response.headers["Cache-Control"] = "no-store"
    return {"items": [draft_response(draft) for draft in rows]}


@router.post("/{comment_id}/ai-drafts", status_code=status.HTTP_202_ACCEPTED, summary="Queue an AI reply suggestion for human review")
def queue_ai_draft_for_comment(
    comment_id: uuid.UUID,
    payload: AIReplyDraftRequest,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("editor")),
    __: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
    __rate: None = Depends(rate_limit("ai-draft", limit_attr="rate_limit_social_action_limit", window_attr="rate_limit_social_action_window_seconds")),
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.ai_drafting_configured:
        raise HTTPException(status_code=503, detail="AI drafting is not configured. Set AI_API_KEY, AI_BASE_URL, and AI_MODEL in the backend environment")
    workspace = _owner_workspace(db)
    comment = db.scalar(select(SocialComment).where(SocialComment.id == comment_id, SocialComment.workspace_id == workspace.id))
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.is_owner_comment:
        raise HTTPException(status_code=422, detail="AI drafts are only available for comments from other users")
    media = db.scalar(select(MediaItem).where(MediaItem.id == comment.media_item_id, MediaItem.workspace_id == workspace.id))
    if media is None:
        raise HTTPException(status_code=409, detail="The comment's post/video context is unavailable")
    draft = AIReplyDraft(
        workspace_id=workspace.id, comment_id=comment.id, idempotency_key=f"manual:{uuid.uuid4().hex}",
        status="queued", tone=payload.tone, language=payload.language,
        custom_instructions=payload.custom_instructions, provider="openai_compatible",
        model=settings.ai_model.strip()[:160] or "unconfigured", prompt_version="reply-v1",
        context_fingerprint=context_fingerprint(comment.text, media.title, comment.platform),
    )
    db.add(draft)
    db.commit()
    try:
        queue_ai_reply_draft(db, draft)
    except AIDraftQueueUnavailable:
        raise HTTPException(status_code=503, detail="The background queue is unavailable. The draft was saved with a failed status; try generating again after the worker recovers") from None
    response.headers["Cache-Control"] = "no-store"
    return draft_response(draft)


@router.patch("/ai-drafts/{draft_id}", summary="Edit, approve, or reject an AI-generated reply draft")
def update_ai_draft(
    draft_id: uuid.UUID,
    payload: AIReplyDraftUpdate,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("editor")),
    __: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _owner_workspace(db)
    draft = db.scalar(select(AIReplyDraft).where(AIReplyDraft.id == draft_id, AIReplyDraft.workspace_id == workspace.id))
    if draft is None:
        raise HTTPException(status_code=404, detail="AI draft not found")
    if draft.status not in {"draft", "approved", "rejected"}:
        raise HTTPException(status_code=409, detail=f"Draft cannot be edited while it is {draft.status}")
    values = payload.model_dump(exclude_unset=True)
    if "draft_text" in values:
        draft.draft_text = values["draft_text"]
        if payload.status is None:
            draft.status = "draft"
            draft.reviewed_at = None
    if payload.status is not None:
        if payload.status == "approved" and not (draft.draft_text or "").strip() and not values.get("draft_text"):
            raise HTTPException(status_code=422, detail="A non-empty draft must exist before approval")
        draft.status = payload.status
        if payload.status in {"approved", "rejected"}:
            draft.reviewed_at = datetime.now(timezone.utc)
        else:
            draft.reviewed_at = None
    db.commit()
    db.refresh(draft)
    response.headers["Cache-Control"] = "no-store"
    return draft_response(draft)


@router.post("/external-video", status_code=status.HTTP_202_ACCEPTED, summary="Queue comments import for a public YouTube video URL")
def queue_external_video_comments(
    payload: ExternalVideoSyncRequest,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("editor")),
    __: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
    __rate: None = Depends(rate_limit("external-video-import", limit_attr="rate_limit_social_action_limit", window_attr="rate_limit_social_action_window_seconds")),
) -> dict[str, Any]:
    workspace = _owner_workspace(db)
    try:
        video_id = parse_youtube_video_url(payload.video_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    account = db.scalar(select(SocialAccount).where(
        SocialAccount.id == payload.account_id,
        SocialAccount.workspace_id == workspace.id,
    ))
    if account is None:
        raise HTTPException(status_code=404, detail="Connected YouTube channel not found")
    if account.platform != "youtube":
        raise HTTPException(status_code=422, detail="Select a YouTube channel to import this video")
    if account.status != "connected":
        raise HTTPException(status_code=409, detail="Reconnect this YouTube channel before importing comments")
    if YOUTUBE_FORCE_SSL_SCOPE not in (account.scopes or []):
        raise HTTPException(status_code=409, detail="Reconnect this channel and grant YouTube comment access before importing or replying")

    try:
        run, queued = queue_sync_run(db, account, trigger="manual", target_platform_media_id=video_id)
    except SyncQueueUnavailable:
        raise HTTPException(status_code=503, detail="The background queue is unavailable. Try again shortly.") from None
    if not queued and run.target_platform_media_id != video_id:
        raise HTTPException(status_code=409, detail="A different sync is already queued or running for this channel. Wait for it to finish, then import this video.")
    response.status_code = status.HTTP_202_ACCEPTED if queued else status.HTTP_200_OK
    response.headers["Cache-Control"] = "no-store"
    return {
        "status": run.status,
        "queued": queued,
        "run_id": str(run.id),
        "account_id": str(account.id),
        "account_name": account.display_name,
        "video_id": video_id,
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
    }


@router.post("/publish-batches", status_code=status.HTTP_410_GONE, summary="Retired legacy multi-channel comment publisher")
def queue_multi_channel_comment_publish(
    payload: MultiChannelPublishRequest,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("editor")),
    __: str = Depends(get_current_admin),
) -> dict[str, Any]:
    """Fail closed: legacy direct/simultaneous public writes are no longer supported."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Direct multi-channel publishing is disabled. Create one contextual draft for one connected channel and one unique video, review it, and explicitly schedule it in the human-approval queue.",
    )


@router.get("/publish-batches/{batch_id}", summary="Read per-channel publish results")
def get_multi_channel_publish_batch(
    batch_id: uuid.UUID,
    response: Response,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _owner_workspace(db)
    batch = db.scalar(select(CommentPublishBatch).where(
        CommentPublishBatch.id == batch_id,
        CommentPublishBatch.workspace_id == workspace.id,
    ))
    if batch is None:
        raise HTTPException(status_code=404, detail="Publish batch not found")
    response.headers["Cache-Control"] = "no-store"
    return publish_batch_response(db, batch)


@router.get("/runs/{run_id}", summary="Read the status of a queued comment job")
def get_comment_run(
    run_id: uuid.UUID,
    response: Response,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _owner_workspace(db)
    run = db.scalar(select(CommentSyncRun).where(
        CommentSyncRun.id == run_id, CommentSyncRun.workspace_id == workspace.id,
    ))
    if run is None:
        raise HTTPException(status_code=404, detail="Sync job not found")
    account = db.get(SocialAccount, run.social_account_id)
    response.headers["Cache-Control"] = "no-store"
    return {
        "id": str(run.id),
        "status": run.status,
        "error_code": run.error_code,
        "target_platform_media_id": run.target_platform_media_id,
        "account_id": str(run.social_account_id),
        "account_name": account.display_name if account else "Connected channel",
        "queued_at": _iso(run.queued_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "media_processed": run.media_processed,
        "comments_inserted": run.comments_inserted,
        "comments_updated": run.comments_updated,
        "comments_skipped": run.comments_skipped,
    }


def _reply_error(exc: ProviderSyncError) -> HTTPException:
    statuses = {
        "reply_text_invalid": 422,
        "reply_platform_unsupported": 422,
        "account_disconnected": 409,
        "reply_scope_missing": 409,
        "provider_reauthorization_required": 409,
        "token_refresh_failed": 409,
        "token_decryption_failed": 409,
        "access_token_missing": 409,
        "reply_not_allowed": 409,
        "reply_target_unavailable": 404,
        "reply_permission_denied": 403,
        "provider_permission_denied": 403,
        "provider_rate_limited": 429,
        "provider_quota_exceeded": 503,
        "provider_unavailable": 502,
        "provider_request_failed": 502,
        "provider_invalid_response": 502,
    }
    messages = {
        "reply_text_invalid": "Enter a non-empty reply within YouTube's text limit.",
        "reply_platform_unsupported": "Replies from this platform are not supported here.",
        "account_disconnected": "Reconnect the selected YouTube channel before replying.",
        "reply_scope_missing": "Reconnect this channel and grant YouTube comment access before replying.",
        "provider_reauthorization_required": "Reconnect the selected YouTube channel before replying.",
        "token_refresh_failed": "Could not refresh channel authorization. Reconnect and try again.",
        "token_decryption_failed": "Saved channel authorization could not be unlocked. Check TOKEN_ENCRYPTION_KEY.",
        "access_token_missing": "Reconnect the selected YouTube channel before replying.",
        "reply_not_allowed": "YouTube does not allow this channel to reply to this comment.",
        "reply_target_unavailable": "This comment may have been deleted, hidden, or made unavailable.",
        "reply_permission_denied": "YouTube denied reply permission for the selected channel.",
        "provider_permission_denied": "YouTube denied permission for this operation.",
        "provider_rate_limited": "YouTube rate-limited this request. Try again shortly.",
        "provider_quota_exceeded": "The YouTube API quota has been reached.",
        "provider_unavailable": "YouTube is temporarily unavailable. Try again shortly.",
        "provider_request_failed": "YouTube rejected the reply. Check the comment and channel permissions.",
        "provider_invalid_response": "YouTube returned an unexpected response.",
    }
    return HTTPException(status_code=statuses.get(exc.code, 502), detail=messages.get(exc.code, "Could not publish the reply."))


@router.post("/{comment_id}/reply", status_code=status.HTTP_201_CREATED, summary="Publish a YouTube reply from a selected connected channel")
async def publish_comment_reply(
    comment_id: uuid.UUID,
    payload: PublishCommentReplyRequest,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("editor")),
    __: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
    __rate: None = Depends(rate_limit("comment-reply", limit_attr="rate_limit_social_action_limit", window_attr="rate_limit_social_action_window_seconds")),
) -> dict[str, Any]:
    workspace = _owner_workspace(db)
    source_comment = db.scalar(select(SocialComment).where(
        SocialComment.id == comment_id, SocialComment.workspace_id == workspace.id,
    ))
    if source_comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    ai_draft: AIReplyDraft | None = None
    if payload.ai_draft_id is not None:
        ai_draft = db.scalar(select(AIReplyDraft).where(
            AIReplyDraft.id == payload.ai_draft_id,
            AIReplyDraft.workspace_id == workspace.id,
            AIReplyDraft.comment_id == source_comment.id,
        ))
        if ai_draft is None:
            raise HTTPException(status_code=404, detail="AI draft not found for this comment")
        if ai_draft.status != "approved":
            raise HTTPException(status_code=409, detail="Approve the AI draft before publishing it")
        if (ai_draft.draft_text or "").strip() != payload.text.strip():
            raise HTTPException(status_code=409, detail="The reply differs from the approved AI draft. Save and approve the edited text before publishing")
    if source_comment.platform != "youtube":
        raise HTTPException(status_code=422, detail="Reply publishing currently supports YouTube comments only")

    source_media = db.scalar(select(MediaItem).where(
        MediaItem.id == source_comment.media_item_id, MediaItem.workspace_id == workspace.id,
    ))
    source_account = db.scalar(select(SocialAccount).where(
        SocialAccount.id == source_comment.social_account_id, SocialAccount.workspace_id == workspace.id,
    ))
    reply_account = db.scalar(select(SocialAccount).where(
        SocialAccount.id == payload.account_id, SocialAccount.workspace_id == workspace.id,
    ))
    if source_media is None or source_account is None:
        raise HTTPException(status_code=404, detail="The video's comment context is no longer available")
    if reply_account is None:
        raise HTTPException(status_code=404, detail="Selected YouTube channel not found")
    if reply_account.platform != "youtube":
        raise HTTPException(status_code=422, detail="Replies can only be published from a YouTube channel")
    if reply_account.status != "connected":
        raise HTTPException(status_code=409, detail="Reconnect the selected YouTube channel before replying")
    if YOUTUBE_FORCE_SSL_SCOPE not in (reply_account.scopes or []):
        raise HTTPException(status_code=409, detail="Reconnect this channel and grant YouTube comment access before replying")

    # YouTube only accepts replies to a top-level comment. When a user replies to an
    # existing reply in the UI, publish into its thread using that thread's root ID.
    root_comment = source_comment
    if source_comment.is_reply:
        root_comment = db.scalar(select(SocialComment).where(
            SocialComment.social_account_id == source_comment.social_account_id,
            SocialComment.media_item_id == source_comment.media_item_id,
            SocialComment.platform_comment_id == source_comment.parent_platform_comment_id,
            SocialComment.is_reply.is_(False),
        ))
        if root_comment is None:
            raise HTTPException(status_code=409, detail="The top-level comment for this thread is unavailable")
    parent_platform_id = root_comment.platform_comment_id
    try:
        published = await publish_youtube_reply(
            reply_account, parent_comment_id=parent_platform_id, text=payload.text, db=db,
        )
    except ProviderSyncError as exc:
        if exc.reauthorization_required:
            reply_account.status = "reauthorization_required"
            reply_account.last_error_code = exc.code
        if ai_draft is not None and exc.outcome_unknown:
            ai_draft.status = "failed"
            ai_draft.error_code = "provider_outcome_unknown"
            db.commit()
        elif exc.reauthorization_required:
            db.commit()
        raise _reply_error(exc) from None

    returned_snippet = published.get("snippet", {}) if isinstance(published.get("snippet"), dict) else {}
    published_id = str(published.get("id") or "")
    if not published_id:
        raise HTTPException(status_code=502, detail="YouTube accepted an unexpected response. Refresh the inbox before retrying.")

    # Make the imported video's context available under the channel that actually sent
    # the reply, even when it differs from the channel used to ingest the comment.
    reply_media = db.scalar(select(MediaItem).where(
        MediaItem.social_account_id == reply_account.id,
        MediaItem.platform == "youtube",
        MediaItem.platform_media_id == source_media.platform_media_id,
    ))
    if reply_media is None:
        reply_media = MediaItem(
            workspace_id=workspace.id, social_account_id=reply_account.id, platform="youtube",
            platform_media_id=source_media.platform_media_id, media_type=source_media.media_type,
            source_type="external_video", title=source_media.title, description=source_media.description,
            permalink=source_media.permalink, thumbnail_url=source_media.thumbnail_url,
            published_at=source_media.published_at, raw_metadata={"external_video": True},
        )
        db.add(reply_media)
        db.flush()
    elif reply_media.source_type != "account_video":
        reply_media.source_type = "external_video"

    mirror_parent = db.scalar(select(SocialComment).where(
        SocialComment.social_account_id == reply_account.id,
        SocialComment.media_item_id == reply_media.id,
        SocialComment.platform_comment_id == root_comment.platform_comment_id,
    ))
    if mirror_parent is None:
        mirror_parent = SocialComment(
            workspace_id=workspace.id, social_account_id=reply_account.id, media_item_id=reply_media.id,
            platform="youtube", platform_comment_id=root_comment.platform_comment_id,
            author_id=root_comment.author_id, author_name=root_comment.author_name,
            author_username=root_comment.author_username, author_avatar_url=root_comment.author_avatar_url,
            text=root_comment.text, like_count=root_comment.like_count, reply_count=root_comment.reply_count,
            published_at=root_comment.published_at, platform_updated_at=root_comment.platform_updated_at,
            is_reply=False, is_owner_comment=root_comment.is_owner_comment,
            has_owner_reply=root_comment.has_owner_reply, workflow_status=root_comment.workflow_status,
            raw_metadata=root_comment.raw_metadata or {},
        )
        db.add(mirror_parent)
        db.flush()
    else:
        mirror_parent.has_owner_reply = True

    existing_reply = db.scalar(select(SocialComment).where(
        SocialComment.social_account_id == reply_account.id,
        SocialComment.platform_comment_id == published_id,
    ))
    is_new = existing_reply is None
    reply_author_channel = returned_snippet.get("authorChannelId") if isinstance(returned_snippet.get("authorChannelId"), dict) else {}
    reply_author_id = reply_author_channel.get("value") if isinstance(reply_author_channel, dict) else None
    reply_text = str(returned_snippet.get("textOriginal") or returned_snippet.get("textDisplay") or payload.text)[:20000]
    reply_published_at = None
    if isinstance(returned_snippet.get("publishedAt"), str):
        try:
            reply_published_at = datetime.fromisoformat(returned_snippet["publishedAt"].replace("Z", "+00:00"))
        except ValueError:
            reply_published_at = None
    if existing_reply is None:
        existing_reply = SocialComment(
            workspace_id=workspace.id, social_account_id=reply_account.id, media_item_id=reply_media.id,
            parent_comment_id=mirror_parent.id, platform="youtube", platform_comment_id=published_id,
            parent_platform_comment_id=mirror_parent.platform_comment_id,
            author_id=str(reply_author_id or reply_account.platform_account_id),
            author_name=str(returned_snippet.get("authorDisplayName") or reply_account.display_name)[:300],
            author_avatar_url=returned_snippet.get("authorProfileImageUrl"), text=reply_text,
            like_count=0, reply_count=0, published_at=reply_published_at,
            is_reply=True, is_owner_comment=True, has_owner_reply=False, workflow_status="open",
            raw_metadata={"published_by_connected_channel": str(reply_account.id)},
        )
        db.add(existing_reply)
    else:
        existing_reply.text = reply_text
        existing_reply.parent_comment_id = mirror_parent.id
        existing_reply.is_owner_comment = True
        existing_reply.last_seen_at = datetime.now(timezone.utc)
    root_comment.has_owner_reply = True
    if ai_draft is not None:
        ai_draft.status = "published"
        ai_draft.published_at = datetime.now(timezone.utc)
        ai_draft.reviewed_at = ai_draft.reviewed_at or ai_draft.published_at
        ai_draft.error_code = None
    if is_new:
        root_comment.reply_count += 1
    mirror_parent.has_owner_reply = True
    if mirror_parent.id != root_comment.id:
        mirror_parent.reply_count = max(mirror_parent.reply_count, root_comment.reply_count)
    try:
        db.commit()
    except Exception:
        db.rollback()
        # The remote insert already succeeded; instruct users to refresh rather than
        # blindly retry and potentially publish duplicate replies.
        logger.exception("YouTube reply published but local persistence failed for account_id=%s", reply_account.id)
        raise HTTPException(status_code=502, detail="YouTube published the reply, but this platform could not save its local copy. Refresh the inbox before retrying.") from None

    response.headers["Cache-Control"] = "no-store"
    return {
        "id": str(existing_reply.id),
        "platform_comment_id": published_id,
        "comment_id": str(root_comment.id),
        "account_id": str(reply_account.id),
        "account_name": reply_account.display_name,
        "text": reply_text,
        "published_at": _iso(reply_published_at),
        "status": "published",
    }
