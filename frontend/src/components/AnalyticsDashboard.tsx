"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { explainApiError, fetchCsrfToken } from "@/lib/csrf";

type Platform = "youtube" | "instagram";
type AccountMetric = {
  account_id: string; platform: Platform; display_name: string; username: string | null; status: string;
  last_comment_sync_status: string; last_comment_sync_at: string | null; last_comment_sync_error_code: string | null;
  comment_metrics: { received: number; owner_replies_published: number; top_level_comments_published: number; resolved: number; likes_received: number };
  provider_metrics: Record<string, number> | null; provider_metrics_date: string | null; provider_metrics_status: string;
  required_scope: string | null; last_analytics_sync_at: string | null; last_analytics_sync_status: string; last_analytics_sync_error_code: string | null;
};
type Kpis = Record<string, number | null>;
type AnalyticsOverview = { period: { start_date: string; end_date: string }; filters: { platform: string | null; account_id: string | null }; kpis: Kpis; definitions: Record<string, string> };
type TrendItem = { date: string; comments_received: number; owner_replies_published: number; top_level_comments_published: number; comments_resolved: number; unanswered_comments: number; comment_likes_received: number; views: number | null; reach: number | null; platform_likes: number | null; shares: number | null; platform_comments: number | null; estimated_minutes_watched: number | null; subscribers_gained: number | null; total_interactions: number | null; accounts_engaged: number | null };
type AccountsResponse = { items: AccountMetric[]; count: number };
type AutomationReport = { rules_total: number; rules_enabled: number; runs_total: number; runs_success: number; runs_partial: number; runs_failed: number; scanned_comments: number; matched_comments: number; drafts_queued: number; failed_executions: number };
type PerformanceItem = { account_id: string; platform: Platform; display_name: string; status: string; scope_granted: boolean; required_scope: string | null; sync_status: string; error_code: string | null; last_synced_at: string | null; latest_snapshot_date: string | null; latest_metrics: Record<string, number> | null };
type TopMediaItem = { account_id: string; account_name: string; platform: Platform; media_id: string; media_type: string; title: string; permalink: string | null; thumbnail_url: string | null; published_at: string | null; metrics_date: string; metrics: Record<string, number>; metrics_basis: string };
type SyncRun = { id: string; account_id: string; account_name: string; platform: Platform | null; status: string; period_start: string; period_end: string; rows_stored: number; error_code: string | null; queued_at: string | null; finished_at: string | null };

const formatNumber = (value: number | null | undefined) => value == null ? "—" : new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value);
const formatPercent = (value: number | null | undefined) => value == null ? "—" : `${value.toFixed(1)}%`;
const formatDuration = (seconds: number | null | undefined) => {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)} sec`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} hr`;
};
const dateOnly = (date: Date) => date.toISOString().slice(0, 10);

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `${fallback} (${response.status})`));
  return body as T;
}

export default function AnalyticsDashboard({ apiBaseUrl, canRefresh }: { apiBaseUrl: string; canRefresh: boolean }) {
  const [rangeDays, setRangeDays] = useState(30);
  const [platform, setPlatform] = useState<"all" | Platform>("all");
  const [accountId, setAccountId] = useState("all");
  const [overview, setOverview] = useState<AnalyticsOverview | null>(null);
  const [timeseries, setTimeseries] = useState<TrendItem[]>([]);
  const [accounts, setAccounts] = useState<AccountMetric[]>([]);
  const [automation, setAutomation] = useState<AutomationReport | null>(null);
  const [performance, setPerformance] = useState<PerformanceItem[]>([]);
  const [topMedia, setTopMedia] = useState<TopMediaItem[]>([]);
  const [runs, setRuns] = useState<SyncRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [exportType, setExportType] = useState("overview");
  const [reloadVersion, setReloadVersion] = useState(0);

  const period = useMemo(() => {
    const end = new Date();
    const start = new Date(end);
    start.setUTCDate(start.getUTCDate() - (rangeDays - 1));
    return { start_date: dateOnly(start), end_date: dateOnly(end) };
  }, [rangeDays]);

  const filterQuery = useMemo(() => {
    const params = new URLSearchParams({ start_date: period.start_date, end_date: period.end_date });
    if (platform !== "all") params.set("platform", platform);
    if (accountId !== "all") params.set("account_id", accountId);
    return params.toString();
  }, [period, platform, accountId]);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [overviewResponse, trendResponse, accountResponse, automationResponse, performanceResponse, runsResponse] = await Promise.all([
        fetch(`${apiBaseUrl}/api/v1/analytics/overview?${filterQuery}`, { credentials: "include", cache: "no-store" }),
        fetch(`${apiBaseUrl}/api/v1/analytics/timeseries?${filterQuery}`, { credentials: "include", cache: "no-store" }),
        fetch(`${apiBaseUrl}/api/v1/analytics/accounts?${filterQuery}`, { credentials: "include", cache: "no-store" }),
        fetch(`${apiBaseUrl}/api/v1/analytics/automation?start_date=${period.start_date}&end_date=${period.end_date}`, { credentials: "include", cache: "no-store" }),
        fetch(`${apiBaseUrl}/api/v1/analytics/performance${platform !== "all" || accountId !== "all" ? `?${new URLSearchParams({ ...(platform !== "all" ? { platform } : {}), ...(accountId !== "all" ? { account_id: accountId } : {}) })}` : ""}`, { credentials: "include", cache: "no-store" }),
        fetch(`${apiBaseUrl}/api/v1/analytics/sync-runs?limit=10`, { credentials: "include", cache: "no-store" }),
      ]);
      const [overviewBody, trendBody, accountBody, automationBody, performanceBody, runsBody] = await Promise.all([
        readJson<AnalyticsOverview>(overviewResponse, "Could not load analytics"),
        readJson<{ items: TrendItem[] }>(trendResponse, "Could not load trends"),
        readJson<AccountsResponse>(accountResponse, "Could not load account metrics"),
        readJson<AutomationReport>(automationResponse, "Could not load automation analytics"),
        readJson<{ items: PerformanceItem[]; top_media?: TopMediaItem[] }>(performanceResponse, "Could not load platform performance"),
        readJson<{ items: SyncRun[] }>(runsResponse, "Could not load refresh history"),
      ]);
      setOverview(overviewBody);
      setTimeseries(trendBody.items ?? []);
      setAccounts(accountBody.items ?? []);
      setAutomation(automationBody);
      setPerformance(performanceBody.items ?? []);
      setTopMedia(performanceBody.top_media ?? []);
      setRuns(runsBody.items ?? []);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Analytics could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [apiBaseUrl, filterQuery, period, platform, accountId]);

  useEffect(() => { void loadAll(); }, [loadAll, reloadVersion]);

  const hasActiveRuns = runs.some((run) => run.status === "queued" || run.status === "running");
  useEffect(() => {
    if (!hasActiveRuns) return;
    const timer = window.setTimeout(() => setReloadVersion((value) => value + 1), 4000);
    return () => window.clearTimeout(timer);
  }, [hasActiveRuns, runs, reloadVersion]);

  const maxTrend = Math.max(1, ...timeseries.map((item) => item.comments_received));
  const visibleTrend = timeseries.slice(-Math.min(rangeDays, 30));
  const hasCommentTrend = visibleTrend.some((item) => item.comments_received > 0 || item.owner_replies_published > 0);
  const providerTrend = visibleTrend.filter((item) => item.views != null || item.reach != null);
  const maxDailyViews = Math.max(1, ...providerTrend.map((item) => item.views ?? 0));
  const maxDailyReach = Math.max(1, ...providerTrend.map((item) => item.reach ?? 0));
  const insightsCards = useMemo(() => {
    const available = performance.filter((item) => item.latest_metrics && item.scope_granted);
    const sumMetric = (key: string) => {
      const values = available.map((item) => item.latest_metrics?.[key]).filter((value): value is number => typeof value === "number" && Number.isFinite(value));
      return { value: values.length ? values.reduce((total, value) => total + value, 0) : null, accounts: values.length };
    };
    return { views: sumMetric("views"), reach: sumMetric("reach"), interactions: sumMetric("total_interactions"), watched: sumMetric("estimatedMinutesWatched"), subscribers: sumMetric("subscribersGained") };
  }, [performance]);

  async function refreshProviderMetrics() {
    setRefreshing(true);
    setNotice("");
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      const selectedAccounts = accounts.filter((item) => item.status === "connected" && item.provider_metrics_status !== "scope_missing");
      const response = await fetch(`${apiBaseUrl}/api/v1/analytics/refresh`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ account_ids: selectedAccounts.map((item) => item.account_id), ...period }),
      });
      const body = await readJson<{ queued_count: number; skipped_count: number; queued: { account_name: string; created: boolean }[]; skipped: { account_name: string; reason: string }[]; note: string }>(response, "Could not queue analytics refresh");
      const queuedNames = body.queued.map((item) => item.account_name).slice(0, 3).join(", ");
      setNotice(body.queued_count ? `Queued ${body.queued_count} account refresh${body.queued_count === 1 ? "" : "es"}${queuedNames ? ` (${queuedNames}${body.queued_count > 3 ? ", …" : ""})` : ""}. Results will appear after the worker completes.${body.skipped_count ? ` ${body.skipped_count} account(s) skipped; inspect their permissions below.` : ""}` : `No eligible account was queued. ${body.skipped_count ? `${body.skipped_count} account(s) need a permission or connection fix.` : "Connect an eligible account first."}`);
      setReloadVersion((value) => value + 1);
    } catch (refreshError) {
      setNotice(refreshError instanceof Error ? refreshError.message : "Could not queue analytics refresh.");
    } finally {
      setRefreshing(false);
    }
  }

  async function exportReport() {
    try {
      const params = new URLSearchParams({ report: exportType, ...period });
      if (exportType !== "automation") {
        if (platform !== "all") params.set("platform", platform);
        if (accountId !== "all") params.set("account_id", accountId);
      }
      const response = await fetch(`${apiBaseUrl}/api/v1/analytics/export.csv?${params}`, { credentials: "include", cache: "no-store" });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(explainApiError(response.status, body.detail, `Export failed (${response.status})`));
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = exportType === "top_media" ? "socialdesk-top-media-current-snapshots.csv" : `socialdesk-${exportType}-${period.start_date}-to-${period.end_date}.csv`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (exportError) {
      setNotice(exportError instanceof Error ? exportError.message : "Could not export the report.");
    }
  }

  const kpis = overview?.kpis ?? {};
  const statusLabel = (status: string) => status.replaceAll("_", " ");

  return (
    <div className="analytics-page">
      <div className="welcome-row analytics-welcome">
        <div><p className="eyebrow">PHASE 8 · ANALYTICS & REPORTING</p><h1>See what is happening across your channels.</h1><p className="page-subtitle">Operational metrics come from your saved comment, publishing, and automation records. Platform insights appear only after the required provider permissions are granted.</p></div>
        <div className="analytics-actions"><label className="analytics-export-select"><span>Export</span><select value={exportType} onChange={(event) => setExportType(event.target.value)}><option value="overview">Overview KPIs</option><option value="timeseries">Daily trend</option><option value="accounts">Per-account report</option><option value="automation">Automation runs</option><option value="performance">Provider performance</option><option value="top_media">Top-performing media</option></select></label><button className="secondary-action" type="button" onClick={() => void exportReport()}>Download CSV</button></div>
      </div>

      <div className="analytics-filterbar">
        <div className="range-options" role="group" aria-label="Reporting period">
          {[7, 30, 90, 365].map((days) => <button key={days} type="button" className={rangeDays === days ? "selected" : ""} onClick={() => setRangeDays(days)}>{days === 365 ? "12 months" : `${days} days`}</button>)}
        </div>
        <label><span>Platform</span><select value={platform} onChange={(event) => { setPlatform(event.target.value as "all" | Platform); setAccountId("all"); }}><option value="all">All platforms</option><option value="youtube">YouTube</option><option value="instagram">Instagram</option></select></label>
        <label><span>Account</span><select value={accountId} onChange={(event) => setAccountId(event.target.value)}><option value="all">All accounts</option>{accounts.map((account) => <option key={account.account_id} value={account.account_id}>{account.display_name} · {account.platform}</option>)}</select></label>
        <span className="analytics-period">{period.start_date} – {period.end_date} · UTC</span>
      </div>

      {notice && <div className="analytics-notice"><span>{notice}</span><button type="button" onClick={() => setNotice("")} aria-label="Dismiss notice">×</button></div>}
      {error && <div className="inline-warning analytics-error">{error}<button type="button" className="text-link" onClick={() => setReloadVersion((value) => value + 1)}>Retry</button></div>}
      {loading && !overview ? <div className="analytics-loading"><span className="loading-mark" />Loading analytics for this workspace…</div> : <>
        <div className="section-heading dashboard-section-heading"><div><h2>Workspace performance</h2><p>Saved activity during the selected period.</p></div><span className="muted-label">OPERATIONAL DATA</span></div>
        <div className="metric-grid analytics-metric-grid">
          <article className="metric-card"><div className="metric-top"><span className="metric-label">Incoming comments</span><span className="metric-icon blue">▤</span></div><div className="metric-value">{formatNumber(kpis.comments_received as number)}</div><div className="metric-foot">Includes external comments and replies recorded in the inbox</div></article>
          <article className="metric-card"><div className="metric-top"><span className="metric-label">Replies published</span><span className="metric-icon green">↩</span></div><div className="metric-value">{formatNumber(kpis.owner_replies_published as number)}</div><div className="metric-foot">Plus {formatNumber(kpis.top_level_comments_published as number)} new top-level comments</div></article>
          <article className="metric-card"><div className="metric-top"><span className="metric-label">Response rate</span><span className="metric-icon violet">◌</span></div><div className="metric-value">{formatPercent(kpis.response_rate as number | null)}</div><div className="metric-foot">Root comments that currently have a connected-account reply</div></article>
          <article className="metric-card"><div className="metric-top"><span className="metric-label">Avg. response time</span><span className="metric-icon amber">◷</span></div><div className="metric-value">{formatDuration(kpis.average_response_time_seconds as number | null)}</div><div className="metric-foot">Only replies with a known parent and timestamps</div></article>
          <article className="metric-card"><div className="metric-top"><span className="metric-label">Avg. resolution time</span><span className="metric-icon amber">✓</span></div><div className="metric-value">{formatDuration(kpis.average_resolution_time_seconds as number | null)}</div><div className="metric-foot">Only resolutions recorded with known timestamps</div></article>
          <article className="metric-card"><div className="metric-top"><span className="metric-label">Unanswered comments</span><span className="metric-icon amber">!</span></div><div className="metric-value">{formatNumber(kpis.unanswered_comments as number)}</div><div className="metric-foot">Open root comments without a connected-account reply</div></article>
          <article className="metric-card"><div className="metric-top"><span className="metric-label">AI drafts generated</span><span className="metric-icon violet">✧</span></div><div className="metric-value">{formatNumber(kpis.ai_drafts_generated as number)}</div><div className="metric-foot">{formatNumber(kpis.ai_drafts_pending_review as number)} currently pending review</div></article>
          <article className="metric-card"><div className="metric-top"><span className="metric-label">Publish success rate</span><span className="metric-icon green">✓</span></div><div className="metric-value">{formatPercent(kpis.publish_success_rate as number | null)}</div><div className="metric-foot">{formatNumber(kpis.publish_failure_count as number)} failed · {formatNumber(kpis.publish_outcome_unknown_count as number)} unknown outcome</div></article>
          <article className="metric-card"><div className="metric-top"><span className="metric-label">Automation runs</span><span className="metric-icon blue">⌘</span></div><div className="metric-value">{formatNumber(kpis.automation_runs as number)}</div><div className="metric-foot">{formatNumber(kpis.automation_drafts_queued as number)} drafts queued for review</div></article>
        </div>

        <div className="analytics-two-col">
          <section className="panel-card analytics-chart-card"><div className="panel-heading"><div><h2>Comment activity</h2><p>Daily incoming comments versus published replies.</p></div><span className="muted-label">LAST {Math.min(rangeDays, 30)} DAYS</span></div>
            <div className="analytics-legend"><span><i className="legend-incoming" /> Incoming</span><span><i className="legend-replies" /> Replies</span></div>
            {!hasCommentTrend ? <p className="analytics-empty">No comment history exists for this period yet.</p> : <div className="trend-chart" role="img" aria-label="Daily incoming comments and published replies. Hover a bar for the date and count.">{visibleTrend.map((item) => <div className="trend-day" key={item.date} title={`${item.date}: ${item.comments_received} incoming, ${item.owner_replies_published} replies`}><div className="trend-bars"><span className="trend-bar incoming" style={{ height: `${Math.max(item.comments_received > 0 ? 4 : 0, item.comments_received / maxTrend * 100)}%` }} /><span className="trend-bar replies" style={{ height: `${Math.max(item.owner_replies_published > 0 ? 4 : 0, item.owner_replies_published / maxTrend * 100)}%` }} /></div><small>{item.date.slice(5)}</small></div>)}</div>}
            <div className="analytics-note">Only dates with stored records contribute. Pre-existing activity before the first sync is not reconstructed.</div>
          </section>
          <section className="panel-card analytics-chart-card"><div className="panel-heading"><div><h2>Automation effectiveness</h2><p>Draft-only automation and review workload.</p></div><span className="muted-label">CURRENT WORKSPACE</span></div>
            <div className="analytics-automation-grid"><div><small>Enabled rules</small><strong>{formatNumber(automation?.rules_enabled)}</strong><span>of {formatNumber(automation?.rules_total)} total</span></div><div><small>Comments scanned</small><strong>{formatNumber(automation?.scanned_comments)}</strong><span>during the selected period</span></div><div><small>Matched comments</small><strong>{formatNumber(automation?.matched_comments)}</strong><span>{formatNumber(automation?.drafts_queued)} drafts queued</span></div><div><small>Failed executions</small><strong>{formatNumber(automation?.failed_executions)}</strong><span>{formatNumber(automation?.runs_failed)} failed runs</span></div></div>
            <div className="analytics-note">Rules create drafts for human review; this dashboard does not assume that drafts were published.</div>
          </section>
        </div>

        <section className="panel-card analytics-chart-card"><div className="panel-heading"><div><h2>Daily provider metrics</h2><p>Daily views and reach returned by YouTube/Instagram for the current filters.</p></div><span className="muted-label">PROVIDER DATA</span></div>
          <div className="analytics-legend"><span><i className="legend-provider-views" /> Views</span><span><i className="legend-provider-reach" /> Reach</span></div>
          {providerTrend.length === 0 ? <p className="analytics-empty">No dated provider insights are available for this period. Refresh eligible account insights after granting the required scopes.</p> : <div className="trend-chart provider-trend-chart" role="img" aria-label="Daily provider views and reach. Each series is normalized independently; missing provider values are not shown as zero.">{visibleTrend.map((item) => <div className="trend-day" key={`provider-${item.date}`} title={`${item.date}: views ${item.views == null ? "unavailable" : item.views}, reach ${item.reach == null ? "unavailable" : item.reach}`}><div className="trend-bars"><span className="trend-bar provider-views" style={{ height: item.views == null ? "0%" : `${Math.max(item.views > 0 ? 4 : 0, item.views / maxDailyViews * 100)}%` }} /><span className="trend-bar provider-reach" style={{ height: item.reach == null ? "0%" : `${Math.max(item.reach > 0 ? 4 : 0, item.reach / maxDailyReach * 100)}%` }} /></div><small>{item.date.slice(5)}</small></div>)}</div>}
          <div className="analytics-note">Views and reach series are scaled independently and should be compared within the same platform/account selection, not against one another. Unavailable metrics remain missing.</div>
        </section>

        <section className="panel-card analytics-provider-card"><div className="panel-heading"><div><h2>Social platform insights</h2><p>Authorized provider metrics; each provider exposes a different set of measures.</p></div>{canRefresh && <button className="primary-action" type="button" onClick={() => void refreshProviderMetrics()} disabled={refreshing || accounts.length === 0}>{refreshing ? "Queueing…" : "Refresh provider insights"}</button>}</div>
          <div className="analytics-insights-grid"><article><span>Latest available views</span><strong>{formatNumber(insightsCards.views.value)}</strong><small>Available for {insightsCards.views.accounts} account(s); latest snapshot dates may differ</small></article><article><span>Reach (Instagram)</span><strong>{formatNumber(insightsCards.reach.value)}</strong><small>Available for {insightsCards.reach.accounts} account(s); Instagram availability varies</small></article><article><span>Reported interactions</span><strong>{formatNumber(insightsCards.interactions.value)}</strong><small>Available for {insightsCards.interactions.accounts} account(s)</small></article><article><span>Watch time</span><strong>{insightsCards.watched.value == null ? "—" : `${formatNumber(insightsCards.watched.value)} min`}</strong><small>YouTube estimated minutes watched · {insightsCards.watched.accounts} account(s)</small></article></div>
          <div className="analytics-account-list">{performance.filter((item) => platform === "all" || item.platform === platform).filter((item) => accountId === "all" || item.account_id === accountId).map((item) => <div className="analytics-account-row" key={item.account_id}><div className={`platform-mark ${item.platform}`}>{item.platform === "youtube" ? "YT" : "IG"}</div><div className="analytics-account-label"><strong>{item.display_name}</strong><small>{item.platform} · {item.latest_snapshot_date ? `metrics through ${item.latest_snapshot_date}` : "No metrics snapshot yet"}</small></div><div className="analytics-account-status"><span className={`status-pill ${item.scope_granted ? item.sync_status : "needs-scope"}`}>{item.scope_granted ? statusLabel(item.sync_status) : "Permission needed"}</span>{!item.scope_granted && <small>Reconnect this account to grant <code>{item.required_scope}</code>.</small>}{item.error_code && <small>Last safe error: {statusLabel(item.error_code)}</small>}</div></div>)}</div>
          <div className="analytics-note">YouTube channel analytics requires <code>yt-analytics.readonly</code>; Instagram insights requires <code>instagram_business_manage_insights</code>. Existing connections do not gain new scopes automatically—reconnect to consent. Provider APIs can return partial or unavailable metrics due to permissions, account type, quotas, or provider data delays.</div>
        </section>

        <section className="panel-card analytics-accounts-card"><div className="panel-heading"><div><h2>Top-performing media</h2><p>Recent videos and posts with provider metrics available for the connected account.</p></div><span className="muted-label">LATEST SNAPSHOTS</span></div>
          {topMedia.filter((item) => platform === "all" || item.platform === platform).filter((item) => accountId === "all" || item.account_id === accountId).length === 0 ? <p className="analytics-empty">No media-level insights are available yet. Refresh provider insights after the required permissions are granted.</p> : <div className="analytics-table-wrap"><table className="analytics-table analytics-media-table"><thead><tr><th>Media</th><th>Account</th><th>Published</th><th>Views</th><th>Reach</th><th>Likes</th><th>Comments</th><th>Interactions</th><th>Snapshot</th></tr></thead><tbody>{topMedia.filter((item) => platform === "all" || item.platform === platform).filter((item) => accountId === "all" || item.account_id === accountId).map((item) => <tr key={`${item.account_id}:${item.media_id}`}><td><div className="table-account">{item.permalink ? <a className="analytics-media-link" href={item.permalink} target="_blank" rel="noreferrer noopener">{item.title}</a> : <strong>{item.title}</strong>}<small>{item.platform} · {item.media_type} · {item.metrics_basis === "current_cumulative" ? "cumulative counts" : "provider lifetime/current metrics"}</small></div></td><td>{item.account_name}</td><td>{item.published_at ? item.published_at.slice(0, 10) : "—"}</td><td>{formatNumber(item.metrics.views)}</td><td>{formatNumber(item.metrics.reach)}</td><td>{formatNumber(item.metrics.likes)}</td><td>{formatNumber(item.metrics.comments)}</td><td>{formatNumber(item.metrics.total_interactions)}</td><td>{item.metrics_date}</td></tr>)}</tbody></table></div>}
          <div className="analytics-note">Only the latest media records are sampled (configurable). YouTube counts are current cumulative statistics; Instagram metrics depend on media type, permissions, and API availability. These snapshots are not retroactive daily history.</div>
        </section>

        <section className="panel-card analytics-accounts-card"><div className="panel-heading"><div><h2>Account activity report</h2><p>Comment handling and synchronization quality by connected account.</p></div><span className="muted-label">{accounts.length} ACCOUNT(S)</span></div>
          {accounts.length === 0 ? <p className="analytics-empty">No connected accounts match the current filters.</p> : <div className="analytics-table-wrap"><table className="analytics-table"><thead><tr><th>Account</th><th>Status</th><th>Incoming</th><th>Replies</th><th>Resolved</th><th>Likes</th><th>Comment sync</th><th>Insights sync</th></tr></thead><tbody>{accounts.map((account) => <tr key={account.account_id}><td><div className="table-account"><strong>{account.display_name}</strong><small>{account.platform}{account.username ? ` · @${account.username}` : ""}</small></div></td><td><span className={`status-pill ${account.status}`}>{statusLabel(account.status)}</span></td><td>{formatNumber(account.comment_metrics.received)}</td><td>{formatNumber(account.comment_metrics.owner_replies_published)}</td><td>{formatNumber(account.comment_metrics.resolved)}</td><td>{formatNumber(account.comment_metrics.likes_received)}</td><td><span className={`status-pill ${account.last_comment_sync_status}`}>{statusLabel(account.last_comment_sync_status)}</span></td><td><span className={`status-pill ${account.provider_metrics_status}`}>{statusLabel(account.provider_metrics_status)}</span>{account.required_scope && <small className="table-hint">Reconnect for insights</small>}</td></tr>)}</tbody></table></div>}
        </section>

        <section className="panel-card analytics-runs-card"><div className="panel-heading"><div><h2>Recent insight refreshes</h2><p>Background jobs, provider data coverage, and errors.</p></div><span className="muted-label">LATEST 10</span></div>
          {runs.length === 0 ? <p className="analytics-empty">No provider-insight refreshes have run yet. Use “Refresh provider insights” when eligible accounts are connected.</p> : <div className="analytics-run-list">{runs.slice(0, 10).map((run) => <div className="analytics-run-row" key={run.id}><div className={`platform-mark ${run.platform ?? "youtube"}`}>{run.platform === "instagram" ? "IG" : "YT"}</div><div className="analytics-account-label"><strong>{run.account_name}</strong><small>{run.period_start} – {run.period_end} · {run.rows_stored} metric record(s) stored</small></div><span className={`status-pill ${run.status}`}>{statusLabel(run.status)}</span>{run.error_code && <small className="run-error">{statusLabel(run.error_code)}</small>}</div>)}</div>}
        </section>
        <footer className="page-footer"><span>Socialdesk · analytics are workspace scoped</span><span>Operational metrics · provider insight snapshots · CSV reports</span></footer>
      </>}
    </div>
  );
}
