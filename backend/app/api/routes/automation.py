from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import get_current_admin, require_csrf, require_workspace_role
from app.models.comment_automation_rule import CommentAutomationExecution, CommentAutomationRule, CommentAutomationRun
from app.models.social_account import SocialAccount
from app.services.workspace_context import active_workspace
from app.schemas.phase4 import AutomationRuleCreate, AutomationRuleUpdate, AutomationRunRequest
from app.services.comment_automation import AutomationQueueUnavailable, queue_automation_run
from app.services.ai_drafting import draft_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/automation", tags=["Phase 4 comment automation"])


def _workspace(db: Session):
    return active_workspace(db)


def _rule_dict(rule: CommentAutomationRule) -> dict[str, Any]:
    return {
        "id": str(rule.id), "name": rule.name, "description": rule.description, "enabled": rule.enabled,
        "priority": rule.priority, "conditions": rule.conditions or {}, "draft_settings": rule.draft_settings or {},
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
        "last_scheduled_at": rule.last_scheduled_at.isoformat() if rule.last_scheduled_at else None,
        "last_run_at": rule.last_run_at.isoformat() if rule.last_run_at else None,
        # This rule only creates drafts. The backend intentionally has no auto-publish action.
        "action": "create_ai_draft_for_manual_review",
    }


def _run_dict(run: CommentAutomationRun) -> dict[str, Any]:
    return {
        "id": str(run.id), "rule_id": str(run.rule_id) if run.rule_id else None,
        "status": run.status, "trigger": run.trigger, "scanned_count": run.scanned_count,
        "matched_count": run.matched_count, "drafts_queued_count": run.drafts_queued_count,
        "skipped_count": run.skipped_count, "failed_count": run.failed_count, "error_code": run.error_code,
        "queued_at": run.queued_at.isoformat() if run.queued_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def _validate_rule_accounts(db: Session, workspace_id, account_ids: list[uuid.UUID]) -> None:
    if not account_ids:
        return
    accounts = db.scalars(select(SocialAccount.id).where(
        SocialAccount.workspace_id == workspace_id,
        SocialAccount.id.in_(account_ids),
    )).all()
    if len(set(accounts)) != len(set(account_ids)):
        raise HTTPException(status_code=404, detail="One or more rule account filters were not found in this workspace")


@router.get("/status", summary="Report Phase 4 AI capability without exposing credentials")
def automation_status(
    response: Response,
    _: str = Depends(get_current_admin),
) -> dict[str, Any]:
    settings = get_settings()
    response.headers["Cache-Control"] = "no-store"
    return {
        "ai_drafting_configured": settings.ai_drafting_configured,
        "provider": "openai_compatible" if settings.ai_drafting_configured else None,
        "model": settings.ai_model.strip() if settings.ai_drafting_configured else None,
        "manual_review_required": True,
        "automation_can_publish_automatically": False,
        "automation_rule_interval_seconds": max(60, min(settings.automation_rule_interval_seconds, 86400)),
    }


@router.get("/rules", summary="List comment automation rules")
def list_rules(
    response: Response,
    enabled: bool | None = Query(default=None),
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _workspace(db)
    statement = select(CommentAutomationRule).where(CommentAutomationRule.workspace_id == workspace.id)
    if enabled is not None:
        statement = statement.where(CommentAutomationRule.enabled.is_(enabled))
    rules = db.scalars(statement.order_by(CommentAutomationRule.priority.asc(), CommentAutomationRule.created_at.desc())).all()
    response.headers["Cache-Control"] = "no-store"
    return {"items": [_rule_dict(rule) for rule in rules], "count": len(rules)}


@router.post("/rules", status_code=status.HTTP_201_CREATED, summary="Create a draft-only comment automation rule")
def create_rule(
    payload: AutomationRuleCreate,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("editor")),
    __: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _workspace(db)
    _validate_rule_accounts(db, workspace.id, payload.conditions.account_ids)
    rule = CommentAutomationRule(
        workspace_id=workspace.id, name=payload.name, description=payload.description,
        enabled=payload.enabled, priority=payload.priority,
        conditions=payload.conditions.model_dump(mode="json"),
        draft_settings=payload.draft_settings.model_dump(mode="json"),
    )
    db.add(rule)
    try:
        db.commit()
        db.refresh(rule)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A rule with this name already exists") from None
    response.headers["Cache-Control"] = "no-store"
    return _rule_dict(rule)


@router.patch("/rules/{rule_id}", summary="Update a draft-only comment automation rule")
def update_rule(
    rule_id: uuid.UUID,
    payload: AutomationRuleUpdate,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("editor")),
    __: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _workspace(db)
    rule = db.scalar(select(CommentAutomationRule).where(CommentAutomationRule.id == rule_id, CommentAutomationRule.workspace_id == workspace.id))
    if rule is None:
        raise HTTPException(status_code=404, detail="Automation rule not found")
    values = payload.model_dump(exclude_unset=True)
    if "conditions" in values and payload.conditions is not None:
        _validate_rule_accounts(db, workspace.id, payload.conditions.account_ids)
        rule.conditions = payload.conditions.model_dump(mode="json")
    if "draft_settings" in values and payload.draft_settings is not None:
        rule.draft_settings = payload.draft_settings.model_dump(mode="json")
    for key in ("name", "description", "enabled", "priority"):
        if key in values:
            setattr(rule, key, values[key])
    try:
        db.commit()
        db.refresh(rule)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A rule with this name already exists") from None
    response.headers["Cache-Control"] = "no-store"
    return _rule_dict(rule)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a comment automation rule")
def delete_rule(
    rule_id: uuid.UUID,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("editor")),
    __: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> Response:
    workspace = _workspace(db)
    rule = db.scalar(select(CommentAutomationRule).where(CommentAutomationRule.id == rule_id, CommentAutomationRule.workspace_id == workspace.id))
    if rule is None:
        raise HTTPException(status_code=404, detail="Automation rule not found")
    db.delete(rule)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/rules/{rule_id}/run", status_code=status.HTTP_202_ACCEPTED, summary="Run a rule against recent comments and queue AI drafts for review")
def run_rule(
    rule_id: uuid.UUID,
    payload: AutomationRunRequest,
    response: Response,
    _: None = Depends(require_csrf),
    __role: None = Depends(require_workspace_role("editor")),
    __: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not payload.confirm_draft_only:
        raise HTTPException(status_code=422, detail="Confirm the rule will create drafts only and will not publish replies automatically")
    if not get_settings().ai_drafting_configured:
        raise HTTPException(status_code=503, detail="Configure AI_API_KEY, AI_BASE_URL, and AI_MODEL in the backend environment first")
    workspace = _workspace(db)
    rule = db.scalar(select(CommentAutomationRule).where(CommentAutomationRule.id == rule_id, CommentAutomationRule.workspace_id == workspace.id))
    if rule is None:
        raise HTTPException(status_code=404, detail="Automation rule not found")
    if not rule.enabled:
        raise HTTPException(status_code=409, detail="Enable this rule before running it")
    run = CommentAutomationRun(workspace_id=workspace.id, rule_id=rule.id, trigger="manual", status="queued")
    db.add(run)
    db.commit()
    try:
        queue_automation_run(db, run)
    except AutomationQueueUnavailable:
        raise HTTPException(status_code=503, detail="The background queue is unavailable. Try again shortly") from None
    response.headers["Cache-Control"] = "no-store"
    return _run_dict(run)


@router.get("/runs/{run_id}", summary="Read an automation run result")
def get_run(
    run_id: uuid.UUID,
    response: Response,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _workspace(db)
    run = db.scalar(select(CommentAutomationRun).where(CommentAutomationRun.id == run_id, CommentAutomationRun.workspace_id == workspace.id))
    if run is None:
        raise HTTPException(status_code=404, detail="Automation run not found")
    response.headers["Cache-Control"] = "no-store"
    return _run_dict(run)


@router.get("/rules/{rule_id}/executions", summary="Inspect recent per-comment rule actions")
def list_rule_executions(
    rule_id: uuid.UUID,
    response: Response,
    page: int = Query(default=1, ge=1, le=100000),
    page_size: int = Query(default=25, ge=1, le=100),
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _workspace(db)
    rule = db.scalar(select(CommentAutomationRule).where(CommentAutomationRule.id == rule_id, CommentAutomationRule.workspace_id == workspace.id))
    if rule is None:
        raise HTTPException(status_code=404, detail="Automation rule not found")
    rows = db.scalars(select(CommentAutomationExecution).where(
        CommentAutomationExecution.workspace_id == workspace.id,
        CommentAutomationExecution.rule_id == rule.id,
    ).order_by(CommentAutomationExecution.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    response.headers["Cache-Control"] = "no-store"
    return {"items": [{
        "id": str(item.id), "rule_id": str(item.rule_id), "comment_id": str(item.comment_id),
        "ai_draft_id": str(item.ai_draft_id) if item.ai_draft_id else None, "status": item.status,
        "error_code": item.error_code, "created_at": item.created_at.isoformat() if item.created_at else None,
    } for item in rows], "page": page, "page_size": page_size}
