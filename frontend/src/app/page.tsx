"use client";

import { useCallback, useEffect, useState } from "react";
import LoginPanel from "@/components/LoginPanel";
import AccountsPanel from "@/components/AccountsPanel";
import CommentsInbox from "@/components/CommentsInbox";
import AutomationRulesPanel from "@/components/AutomationRulesPanel";
import TeamManagementPanel from "@/components/TeamManagementPanel";
import InviteAcceptancePanel from "@/components/InviteAcceptancePanel";
import PasswordResetPanel from "@/components/PasswordResetPanel";
import WorkspaceSwitcher from "@/components/WorkspaceSwitcher";
import AnalyticsDashboard from "@/components/AnalyticsDashboard";
import { explainApiError, fetchCsrfToken } from "@/lib/csrf";

type ApiState = "checking" | "online" | "offline";
type AuthState = "checking" | "signed-in" | "signed-out";
type OwnerSession = { email: string; workspace_id: string; workspace_name: string; role?: "owner" | "admin" | "editor" | "viewer"; user_id?: string; full_name?: string };
type AccountCounts = { total: number; youtube: number; instagram: number };
type DashboardSummary = {
  open_comments: number;
  unanswered_comments: number;
  resolved_comments: number;
  all_comments: number;
  accounts_needing_attention: number;
  last_sync_at: string | null;
};
type DashboardView = "overview" | "inbox" | "automation" | "team" | "analytics";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export default function HomePage() {
  const [apiState, setApiState] = useState<ApiState>("checking");
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [csrfToken, setCsrfToken] = useState("");
  const [owner, setOwner] = useState<OwnerSession | null>(null);
  const [inviteToken, setInviteToken] = useState("");
  const [resetToken, setResetToken] = useState("");
  const [notice, setNotice] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [accountCounts, setAccountCounts] = useState<AccountCounts>({ total: 0, youtube: 0, instagram: 0 });
  const [summary, setSummary] = useState<DashboardSummary>({ open_comments: 0, unanswered_comments: 0, resolved_comments: 0, all_comments: 0, accounts_needing_attention: 0, last_sync_at: null });
  const [summaryLoaded, setSummaryLoaded] = useState(false);
  const [view, setView] = useState<DashboardView>("overview");
  const [aiDraftSummary, setAiDraftSummary] = useState<{ total: number; pending_review: number; active: number }>({ total: 0, pending_review: 0, active: 0 });
  const [automationRuleCount, setAutomationRuleCount] = useState(0);

  useEffect(() => {
    let alive = true;
    async function initialise() {
      try {
        const health = await fetch(`${API_BASE_URL}/health`, { cache: "no-store" });
        if (alive) setApiState(health.ok ? "online" : "offline");
      } catch {
        if (alive) setApiState("offline");
      }
      try {
        const csrfResponse = await fetch(`${API_BASE_URL}/api/v1/auth/csrf`, {
          credentials: "include", cache: "no-store",
        });
        const csrfBody = await csrfResponse.json().catch(() => ({}));
        if (alive && csrfResponse.ok && typeof csrfBody.csrf_token === "string") setCsrfToken(csrfBody.csrf_token);

        const meResponse = await fetch(`${API_BASE_URL}/api/v1/auth/me`, {
          credentials: "include", cache: "no-store",
        });
        if (!alive) return;
        if (meResponse.ok) {
          setOwner(await meResponse.json());
          setAuthState("signed-in");
        } else {
          setAuthState("signed-out");
        }
      } catch {
        if (alive) setAuthState("signed-out");
      }

      if (alive && typeof window !== "undefined") {
        const params = new URLSearchParams(window.location.search);
        const invitation = params.get("invite");
        const reset = params.get("reset");
        if (invitation) setInviteToken(invitation);
        if (reset) setResetToken(reset);
        if (invitation || reset) window.history.replaceState({}, "", window.location.pathname);
        const connection = params.get("connection");
        const platform = params.get("platform");
        const count = params.get("count");
        const code = params.get("code");
        if (connection === "success") {
          setNotice(`${platform === "youtube" ? "YouTube" : "Instagram"} connection successful${count ? ` · ${count} account(s) saved` : ""}.`);
          setRefreshKey((previous) => previous + 1);
        } else if (connection === "error") {
          const known: Record<string, string> = {
            invalid_or_expired_state: "The connection link expired or was already used. Please try connecting again.",
            authorization_cancelled: "Authorization was cancelled in the provider window.",
            authorization_code_missing: "The provider did not return an authorization code.",
            configuration_missing: "Backend OAuth configuration is incomplete. Check the Phase 2 setup guide.",
            token_exchange_failed: "The provider could not exchange the authorization code. Check app configuration and redirect URI.",
            provider_unavailable: "The provider API could not be reached. Try again shortly.",
            profile_fetch_failed: "Authorization succeeded, but account profile access failed. Check requested permissions.",
            no_youtube_channel_found: "No accessible YouTube channel was returned for this Google account.",
            account_limit_reached: "This workspace has reached its account limit.",
            no_account_found: "The provider did not return a supported account.",
          };
          setNotice(known[code ?? ""] ?? `Could not connect ${platform ?? "the account"}. Review the provider configuration and try again.`);
        }
        if (connection) window.history.replaceState({}, "", window.location.pathname);
      }
    }
    void initialise();
    return () => { alive = false; };
  }, []);

  const onCountChange = useCallback((counts: AccountCounts) => setAccountCounts(counts), []);
  const onSummaryChange = useCallback((next: DashboardSummary) => { setSummary(next); setSummaryLoaded(true); }, []);

  useEffect(() => {
    if (authState !== "signed-in") return;
    let alive = true;
    fetch(`${API_BASE_URL}/api/v1/comments/summary`, { credentials: "include", cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) return null;
        return await response.json() as DashboardSummary;
      })
      .then((body) => { if (alive && body) { setSummary(body); setSummaryLoaded(true); } })
      .catch(() => undefined);
    fetch(`${API_BASE_URL}/api/v1/comments/ai/summary`, { credentials: "include", cache: "no-store" })
      .then(async (response) => response.ok ? await response.json() as { total: number; pending_review: number; active: number } : null)
      .then((body) => { if (alive && body) setAiDraftSummary(body); })
      .catch(() => undefined);
    fetch(`${API_BASE_URL}/api/v1/automation/rules`, { credentials: "include", cache: "no-store" })
      .then(async (response) => response.ok ? await response.json() as { count?: number } : null)
      .then((body) => { if (alive && body) setAutomationRuleCount(Number(body.count ?? 0)); })
      .catch(() => undefined);
    return () => { alive = false; };
  }, [authState, refreshKey]);

  useEffect(() => {
    if (typeof window !== "undefined" && window.location.hash === "#inbox") setView("inbox");
    if (typeof window !== "undefined" && window.location.hash === "#automation") setView("automation");
    if (typeof window !== "undefined" && window.location.hash === "#team") setView("team");
    if (typeof window !== "undefined" && window.location.hash === "#analytics") setView("analytics");
  }, []);

  function navigate(nextView: DashboardView) {
    setView(nextView);
    if (typeof window !== "undefined") window.history.replaceState({}, "", nextView === "inbox" ? "#inbox" : nextView === "automation" ? "#automation" : nextView === "team" ? "#team" : nextView === "analytics" ? "#analytics" : "#overview");
  }

  function goToAccounts() {
    navigate("overview");
    if (typeof window !== "undefined") {
      window.history.replaceState({}, "", "#accounts");
      window.setTimeout(() => document.getElementById("accounts")?.scrollIntoView({ behavior: "smooth", block: "start" }), 60);
    }
  }

  async function logout() {
    try {
      const freshCsrfToken = await fetchCsrfToken(API_BASE_URL);
      const response = await fetch(`${API_BASE_URL}/api/v1/auth/logout`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "X-CSRF-Token": freshCsrfToken },
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Logout failed (${response.status})`));
      setOwner(null);
      setAuthState("signed-out");
      setNotice("You have signed out.");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Could not sign out. Check your connection and retry.");
    }
  }

  if (inviteToken) {
    return <InviteAcceptancePanel
      apiBaseUrl={API_BASE_URL}
      token={inviteToken}
      onAccepted={(session) => {
        setOwner(session);
        setAuthState("signed-in");
        setInviteToken("");
        setNotice(`Invitation accepted. You joined ${session.workspace_name} as ${session.role ?? "member"}.`);
        if (typeof window !== "undefined") window.history.replaceState({}, "", window.location.pathname);
      }}
      onBackToLogin={() => {
        setInviteToken("");
        if (typeof window !== "undefined") window.history.replaceState({}, "", window.location.pathname);
        if (!owner) setAuthState("signed-out");
      }}
    />;
  }

  if (authState === "checking") {
    return <main className="login-shell"><section className="login-card boot-card"><div className="brand-lockup login-brand"><div className="brand-mark">S</div><div><div className="brand-name">socialdesk</div><div className="brand-caption">ENGAGEMENT HUB</div></div></div><div className="loading-mark" /><h1>Preparing your workspace…</h1><p className="page-subtitle">Checking the API and secure session.</p>{apiState === "offline" && <div className="inline-warning">The API is unreachable. Start the project with <code>docker compose up --build</code>.</div>}</section></main>;
  }

  if (resetToken && (authState === "signed-out" || !owner)) {
    return <PasswordResetPanel apiBaseUrl={API_BASE_URL} token={resetToken} onCompleted={() => { setResetToken(""); setAuthState("signed-out"); setNotice("Password updated. Sign in with your new password."); }} />;
  }

  if (authState === "signed-out" || !owner) {
    return <LoginPanel apiBaseUrl={API_BASE_URL} csrfToken={csrfToken} onLoggedIn={(session) => { setOwner(session); setAuthState("signed-in"); setNotice("Signed in successfully."); }} />;
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup"><div className="brand-mark">S</div><div><div className="brand-name">socialdesk</div><div className="brand-caption">ENGAGEMENT HUB</div></div></div>
        <WorkspaceSwitcher apiBaseUrl={API_BASE_URL} currentWorkspaceId={owner.workspace_id} currentWorkspaceName={owner.workspace_name} currentRole={owner.role ?? "viewer"} />
        <p className="nav-label">WORKSPACE</p>
        <nav className="navigation" aria-label="Main navigation">
          <a className={`nav-item ${view === "overview" ? "active" : ""}`} href="#overview" onClick={(event) => { event.preventDefault(); navigate("overview"); }}><span className="nav-icon">◫</span> Overview</a>
          <a className="nav-item" href="#accounts" onClick={(event) => { event.preventDefault(); goToAccounts(); }}><span className="nav-icon">◎</span> Accounts <span className="soon">CONNECTED</span></a>
          <a className={`nav-item ${view === "inbox" ? "active" : ""}`} href="#inbox" onClick={(event) => { event.preventDefault(); navigate("inbox"); }}><span className="nav-icon">▤</span> Unified inbox <span className="soon">LIVE</span></a>
          <a className={`nav-item ${view === "automation" ? "active" : ""}`} href="#automation" onClick={(event) => { event.preventDefault(); navigate("automation"); }}><span className="nav-icon">⌘</span> Automation <span className="soon">PHASE 4</span></a>
          <a className={`nav-item ${view === "team" ? "active" : ""}`} href="#team" onClick={(event) => { event.preventDefault(); navigate("team"); }}><span className="nav-icon">♙</span> Team & access <span className="soon">PHASE 5</span></a>
          <a className={`nav-item ${view === "analytics" ? "active" : ""}`} href="#analytics" onClick={(event) => { event.preventDefault(); navigate("analytics"); }}><span className="nav-icon">▥</span> Analytics <span className="soon">REPORTS</span></a>
        </nav>
        <div className="sidebar-bottom">
          <div className="phase-card"><div className="phase-card-top"><span className="phase-dot" /> ACCOUNT INTEGRATIONS</div><strong>Phase 8 of 8</strong><div className="progress-track"><div className="progress-fill phase-two-fill" style={{ width: "87.5%" }} /></div><p>Workspace-scoped analytics, provider insights, trends, and CSV reporting.</p></div>
          <div className="user-row"><div className="avatar">{owner.email.slice(0, 2).toUpperCase()}</div><div className="user-copy"><strong>{owner.email}</strong><small>{owner.role ?? "member"}</small></div><button type="button" className="logout-link" onClick={() => void logout()}>Log out</button></div>
        </div>
      </aside>

      <section className="main-panel" id="overview">
        <header className="topbar"><div className="breadcrumb">Workspace <span>/</span> <strong>{view === "inbox" ? "Unified inbox" : view === "automation" ? "Automation rules" : view === "team" ? "Team & access" : view === "analytics" ? "Analytics & reporting" : "Overview"}</strong></div><div className="topbar-right"><span className={`api-pill ${apiState}`}><span className="status-dot" /> API {apiState === "checking" ? "checking" : apiState === "online" ? "connected" : "unavailable"}</span><div className="top-avatar">{owner.email.slice(0, 2).toUpperCase()}</div></div></header>
        <div className="page-content">
          {notice && <div className="notice-banner connection-notice"><div className="notice-icon">✦</div><div className="notice-copy"><strong>{notice}</strong><p>OAuth credentials and tokens are handled by the backend and are never shown in the dashboard.</p></div><button className="notice-dismiss" type="button" onClick={() => setNotice("")} aria-label="Dismiss message">×</button></div>}
          {view === "inbox" ? <CommentsInbox apiBaseUrl={API_BASE_URL} onGoAccounts={goToAccounts} onSummaryChange={onSummaryChange} canEdit={owner.role !== "viewer"} canAdmin={owner.role === "owner" || owner.role === "admin"} /> : view === "automation" ? <AutomationRulesPanel apiBaseUrl={API_BASE_URL} onGoInbox={() => navigate("inbox")} canEdit={owner.role !== "viewer"} /> : view === "team" ? <TeamManagementPanel apiBaseUrl={API_BASE_URL} currentEmail={owner.email} currentRole={owner.role ?? "owner"} /> : view === "analytics" ? <AnalyticsDashboard apiBaseUrl={API_BASE_URL} canRefresh={owner.role !== "viewer"} /> : <>
          <div className="welcome-row"><div><p className="eyebrow">YOUR COMMAND CENTER</p><h1>A clearer view of every conversation.</h1><p className="page-subtitle">Connect accounts, synchronize comments, and review conversations from one workspace.</p></div><div className="date-chip"><span>✧</span> Phase 8 · Analytics & reporting</div></div>
          <AccountsPanel apiBaseUrl={API_BASE_URL} refreshKey={refreshKey} canManage={owner.role === "owner" || owner.role === "admin"} onCountChange={onCountChange} />
          <div className="section-heading dashboard-section-heading"><div><h2>Workspace snapshot</h2><p>Live counts from your synchronized comments and connected accounts.</p></div><span className="muted-label">COMMENT ACTIVITY</span></div>
          <div className="metric-grid">
            <article className="metric-card"><div className="metric-top"><span className="metric-label">Connected accounts</span><span className="metric-icon violet">◎</span></div><div className="metric-value">{accountCounts.total}</div><div className="metric-foot">{accountCounts.youtube} YouTube · {accountCounts.instagram} Instagram</div></article>
            <article className="metric-card"><div className="metric-top"><span className="metric-label">Open comments</span><span className="metric-icon blue">▤</span></div><div className="metric-value">{summaryLoaded ? summary.open_comments.toLocaleString() : "—"}</div><div className="metric-foot">{summaryLoaded ? `${summary.unanswered_comments.toLocaleString()} unanswered` : "Loading inbox metrics"}</div></article>
            <article className="metric-card"><div className="metric-top"><span className="metric-label">AI drafts</span><span className="metric-icon green">✧</span></div><div className="metric-value">{aiDraftSummary.total.toLocaleString()}</div><div className="metric-foot">{aiDraftSummary.pending_review.toLocaleString()} awaiting review · {aiDraftSummary.active.toLocaleString()} generating</div></article>
            <article className="metric-card"><div className="metric-top"><span className="metric-label">Automation rules</span><span className="metric-icon amber">⌘</span></div><div className="metric-value">{automationRuleCount.toLocaleString()}</div><div className="metric-foot">Draft-only rules with explicit human approval</div></article>
          </div>
          <div className="lower-grid">
            <section className="panel-card system-card"><div className="panel-heading"><div><h2>System status</h2><p>Check that foundation services respond.</p></div><span className="live-label"><span className="status-dot" /> LIVE CHECK</span></div><div className="service-row"><div className="service-symbol api-symbol">API</div><div className="service-text"><strong>FastAPI backend</strong><small>{API_BASE_URL}</small></div><span className={`service-status ${apiState}`}>{apiState === "checking" ? "Checking" : apiState === "online" ? "Online" : "Offline"}</span></div><div className="service-row"><div className="service-symbol db-symbol">DB</div><div className="service-text"><strong>PostgreSQL</strong><small>Checked by API readiness endpoint</small></div><a className="text-link" href={`${API_BASE_URL}/ready`} target="_blank" rel="noreferrer">Check readiness ↗</a></div><div className="service-row"><div className="service-symbol redis-symbol">R</div><div className="service-text"><strong>Redis</strong><small>Background-job foundation</small></div><a className="text-link" href={`${API_BASE_URL}/ready`} target="_blank" rel="noreferrer">Check readiness ↗</a></div>{apiState === "offline" && <div className="inline-warning">The API could not be reached. Start the project with <code>docker compose up --build</code> and refresh this page.</div>}</section>
            <section className="panel-card next-card"><div className="panel-heading"><div><h2>What comes next</h2><p>Build in small, verifiable steps.</p></div><span className="sparkle">✦</span></div><div className="next-step"><span className="step-number">08</span><div><strong>Review channel analytics</strong><p>Track reply performance, provider insights and activity trends</p></div><button type="button" className="step-arrow-button" onClick={() => navigate("analytics")}>↗</button></div><div className="next-step"><span className="step-number">08</span><div><strong>Export a report</strong><p>Download date-filtered workspace reports as CSV</p></div><button type="button" className="step-arrow-button" onClick={() => navigate("analytics")}>↗</button></div><div className="note-bottom"><span>ⓘ</span> Provider analytics require additional consent. Missing historical data is not inferred or fabricated.</div></section>
          </div>
          <footer className="page-footer"><span>socialdesk · workspace collaboration</span><span><a href={`${API_BASE_URL}/docs`} target="_blank" rel="noreferrer">API documentation ↗</a><span className="footer-separator">·</span> Phase 7 / 8</span></footer>
          </>}
        </div>
      </section>
    </main>
  );
}
