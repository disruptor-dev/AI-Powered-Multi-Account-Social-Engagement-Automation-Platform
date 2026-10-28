from __future__ import annotations

import csv
import io
import uuid
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import get_current_admin, require_csrf, require_workspace_role
from app.models.analytics_sync_run import AnalyticsSyncRun
from app.models.social_account import SocialAccount
from app.services.analytics import (
    _run_item,
    get_accounts_report,
    get_automation_report,
    get_overview,
    get_performance_report,
    get_timeseries,
    queue_analytics_sync_run,
    resolve_period,
    _provider_scope,
)
from app.services.rate_limiting import rate_limit
from app.services.workspace_context import active_workspace

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics and reporting"])


class AnalyticsRefreshRequest(BaseModel):
    account_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    start_date: date | None = None
    end_date: date | None = None


ReportType = Literal["overview", "timeseries", "accounts", "automation", "performance", "top_media"]


def _period(start_date: date | None, end_date: date | None) -> tuple[date, date]:
    try:
        return resolve_period(start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _context(db: Session):
    return active_workspace(db)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Cookie"


@router.get("/overview", summary="Workspace analytics KPIs and reporting definitions")
def analytics_overview(
    response: Response,
    start_date: date | None = None,
    end_date: date | None = None,
    platform: str | None = None,
    account_id: uuid.UUID | None = None,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _context(db)
    start, end = _period(start_date, end_date)
    result = get_overview(db, workspace, start, end, platform, account_id)
    _no_store(response)
    return result


@router.get("/timeseries", summary="Daily operational and available provider metric trends")
def analytics_timeseries(
    response: Response,
    start_date: date | None = None,
    end_date: date | None = None,
    platform: str | None = None,
    account_id: uuid.UUID | None = None,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _context(db)
    start, end = _period(start_date, end_date)
    result = get_timeseries(db, workspace, start, end, platform, account_id)
    _no_store(response)
    return result


@router.get("/accounts", summary="Per-account activity and synchronization health")
def analytics_accounts(
    response: Response,
    start_date: date | None = None,
    end_date: date | None = None,
    platform: str | None = None,
    account_id: uuid.UUID | None = None,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _context(db)
    start, end = _period(start_date, end_date)
    result = get_accounts_report(db, workspace, start, end, platform, account_id)
    _no_store(response)
    return result


@router.get("/automation", summary="AI draft and automation performance report")
def analytics_automation(
    response: Response,
    start_date: date | None = None,
    end_date: date | None = None,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _context(db)
    start, end = _period(start_date, end_date)
    result = get_automation_report(db, workspace, start, end)
    _no_store(response)
    return result


@router.get("/performance", summary="Authorized YouTube Analytics / Instagram Insights availability and data")
def analytics_performance(
    response: Response,
    platform: str | None = None,
    account_id: uuid.UUID | None = None,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _context(db)
    result = get_performance_report(db, workspace, date.today() - timedelta(days=29), date.today(), platform, account_id)
    _no_store(response)
    return result


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED, summary="Queue provider analytics refresh for selected connected accounts")
def refresh_analytics(
    payload: AnalyticsRefreshRequest,
    response: Response,
    _: None = Depends(require_csrf),
    __: None = Depends(require_workspace_role("editor")),
    ___: None = Depends(rate_limit("analytics-refresh", limit_attr="rate_limit_social_action_limit", window_attr="rate_limit_social_action_window_seconds")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _context(db)
    start, end = _period(payload.start_date, payload.end_date)
    max_refresh_accounts = max(1, min(int(get_settings().analytics_max_refresh_accounts), 500))
    if payload.account_ids is not None and len(set(payload.account_ids)) > max_refresh_accounts:
        raise HTTPException(status_code=422, detail=f"At most {max_refresh_accounts} accounts can be refreshed in one request")
    overflow_count = 0
    if payload.account_ids is not None:
        wanted = list(dict.fromkeys(payload.account_ids))
        if len(wanted) > max_refresh_accounts:
            raise HTTPException(status_code=422, detail=f"At most {max_refresh_accounts} accounts can be refreshed in one request")
        accounts = db.scalars(select(SocialAccount).where(SocialAccount.workspace_id == workspace.id, SocialAccount.id.in_(wanted)).order_by(SocialAccount.connected_at.asc())).all() if wanted else []
        if len(accounts) != len(wanted):
            raise HTTPException(status_code=404, detail="One or more selected accounts were not found in this workspace")
    else:
        total_accounts = int(db.scalar(select(func.count(SocialAccount.id)).where(SocialAccount.workspace_id == workspace.id)) or 0)
        accounts = db.scalars(select(SocialAccount).where(SocialAccount.workspace_id == workspace.id).order_by(
            case((SocialAccount.status == "connected", 0), else_=1), SocialAccount.connected_at.asc()
        ).limit(max_refresh_accounts)).all()
        overflow_count = max(0, total_accounts - len(accounts))
    queued: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if overflow_count:
        skipped.append({"account_id": None, "account_name": f"{overflow_count} additional account(s)", "reason": "analytics_refresh_account_limit_exceeded", "limit": max_refresh_accounts})
    for account in accounts:
        if account.status != "connected":
            skipped.append({"account_id": str(account.id), "account_name": account.display_name, "reason": "account_not_connected"})
            continue
        required_scope = _provider_scope(account)
        if required_scope:
            skipped.append({"account_id": str(account.id), "account_name": account.display_name, "reason": "analytics_scope_missing", "required_scope": required_scope, "action": "reconnect_account_to_grant_insights_permission"})
            continue
        try:
            run, created = queue_analytics_sync_run(db, account, start, end, trigger="manual")
        except RuntimeError:
            # Continue reporting per-account results; other account refreshes may already be queued.
            skipped.append({"account_id": str(account.id), "account_name": account.display_name, "reason": "analytics_queue_unavailable"})
            continue
        queued.append({"run_id": str(run.id), "account_id": str(account.id), "account_name": account.display_name, "platform": account.platform, "status": run.status, "created": created})
    _no_store(response)
    response.status_code = status.HTTP_202_ACCEPTED if queued else status.HTTP_200_OK
    return {"period": {"start_date": start.isoformat(), "end_date": end.isoformat()}, "queued": queued, "skipped": skipped, "queued_count": len(queued), "skipped_count": len(skipped), "note": "Provider insights require the corresponding OAuth scope. Reconnect accounts missing the required scope; data availability also depends on provider permissions and account eligibility."}


@router.get("/sync-runs", summary="Recent analytics refresh jobs")
def analytics_sync_runs(
    response: Response,
    limit: int = Query(default=25, ge=1, le=100),
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _context(db)
    rows = db.scalars(select(AnalyticsSyncRun).where(AnalyticsSyncRun.workspace_id == workspace.id).order_by(AnalyticsSyncRun.queued_at.desc()).limit(limit)).all()
    result = [_run_item(run, db) for run in rows]
    _no_store(response)
    return {"items": result, "count": len(result)}


@router.get("/sync-runs/{run_id}", summary="Read an analytics refresh job")
def analytics_sync_run(
    run_id: uuid.UUID,
    response: Response,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workspace = _context(db)
    run = db.scalar(select(AnalyticsSyncRun).where(AnalyticsSyncRun.id == run_id, AnalyticsSyncRun.workspace_id == workspace.id))
    if run is None:
        raise HTTPException(status_code=404, detail="Analytics refresh job not found")
    _no_store(response)
    return _run_item(run, db)


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = __import__("json").dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + value
    return value


def _csv_response(filename: str, rows: list[dict[str, Any]]) -> StreamingResponse:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    if rows:
        columns = list(rows[0].keys())
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_csv_cell(row.get(column)) for column in columns])
    else:
        writer.writerow(["message"])
        writer.writerow(["No data for the selected filters and date range"])
    raw = buffer.getvalue()
    response = StreamingResponse(iter([raw]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    return response


@router.get("/export.csv", summary="Export a workspace-scoped analytics report as CSV")
def export_analytics_csv(
    report: ReportType = "overview",
    start_date: date | None = None,
    end_date: date | None = None,
    platform: str | None = None,
    account_id: uuid.UUID | None = None,
    _: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    workspace = _context(db)
    start, end = _period(start_date, end_date)
    if report == "automation" and (platform or account_id):
        raise HTTPException(status_code=422, detail="Automation report is workspace-wide; remove platform/account filters")
    if report == "overview":
        data = get_overview(db, workspace, start, end, platform, account_id)
        rows = [{"metric": key, "value": value} for key, value in data["kpis"].items()]
        filename = "socialdesk-analytics-overview.csv"
    elif report == "timeseries":
        rows = get_timeseries(db, workspace, start, end, platform, account_id)["items"]
        filename = "socialdesk-analytics-timeseries.csv"
    elif report == "accounts":
        accounts = get_accounts_report(db, workspace, start, end, platform, account_id)["items"]
        rows = [{"account_id": item["account_id"], "platform": item["platform"], "display_name": item["display_name"], "username": item["username"], "status": item["status"], **item["comment_metrics"], "provider_metrics_date": item["provider_metrics_date"], "provider_metrics": item["provider_metrics"], "last_comment_sync_status": item["last_comment_sync_status"], "last_analytics_sync_status": item["last_analytics_sync_status"], "last_analytics_sync_error_code": item["last_analytics_sync_error_code"]} for item in accounts]
        filename = "socialdesk-analytics-accounts.csv"
    elif report == "automation":
        data = get_automation_report(db, workspace, start, end)
        rows = data["runs"]
        filename = "socialdesk-analytics-automation.csv"
    elif report == "performance":
        data = get_performance_report(db, workspace, start, end, platform, account_id)
        rows = [{"account_id": item["account_id"], "platform": item["platform"], "display_name": item["display_name"], "status": item["status"], "scope_granted": item["scope_granted"], "required_scope": item["required_scope"], "sync_status": item["sync_status"], "error_code": item["error_code"], "last_synced_at": item["last_synced_at"], "latest_snapshot_date": item["latest_snapshot_date"], "latest_metrics": item["latest_metrics"]} for item in data["items"]]
        filename = "socialdesk-analytics-performance.csv"
    else:
        data = get_performance_report(db, workspace, start, end, platform, account_id)
        rows = [{"account_id": item["account_id"], "account_name": item["account_name"], "platform": item["platform"], "media_id": item["media_id"], "media_type": item["media_type"], "title": item["title"], "permalink": item["permalink"], "published_at": item["published_at"], "metrics_date": item["metrics_date"], "metrics": item["metrics"], "metrics_basis": item["metrics_basis"]} for item in data.get("top_media", [])]
        filename = "socialdesk-analytics-top-media.csv"
    return _csv_response(filename, rows)
