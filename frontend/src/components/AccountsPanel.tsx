"use client";

import { useCallback, useEffect, useState } from "react";
import { explainApiError, fetchCsrfToken } from "@/lib/csrf";

type SocialAccount = {
  id: string;
  platform: "youtube" | "instagram";
  platform_account_id: string;
  display_name: string;
  username: string | null;
  account_type: string | null;
  avatar_url: string | null;
  status: "connected" | "reauthorization_required" | "disconnected";
  access_token_expires_at: string | null;
  connected_at: string;
  last_error_code: string | null;
};

type AccountResponse = {
  items: SocialAccount[];
  platform_setup: { youtube: boolean; instagram: boolean };
  token_encryption_ready: boolean;
  account_limit: number;
};

type Props = { apiBaseUrl: string; refreshKey: number; canManage: boolean; onCountChange: (counts: { total: number; youtube: number; instagram: number }) => void };

function friendlyExpiry(value: string | null): string {
  if (!value) return "Expiry not provided by platform";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Expiry unknown";
  return `Token expiry ${date.toLocaleDateString()}`;
}

export default function AccountsPanel({ apiBaseUrl, refreshKey, canManage, onCountChange }: Props) {
  const [data, setData] = useState<AccountResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const loadAccounts = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetch(`${apiBaseUrl}/api/v1/accounts`, { credentials: "include", cache: "no-store" });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || `Unable to load accounts (${response.status})`);
      setData(body as AccountResponse);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load connected accounts.");
    } finally {
      setLoading(false);
    }
  }, [apiBaseUrl]);

  useEffect(() => { void loadAccounts(); }, [loadAccounts, refreshKey]);

  async function connect(platform: "youtube" | "instagram") {
    setAction(`connect-${platform}`);
    setError(""); setMessage("");
    try {
      const freshCsrfToken = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/accounts/${platform}/connect`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "X-CSRF-Token": freshCsrfToken },
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Could not start ${platform} connection (${response.status})`));
      if (typeof body.authorization_url !== "string" || !body.authorization_url.startsWith("https://")) throw new Error("Backend returned an invalid authorization URL.");
      window.location.assign(body.authorization_url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the connection.");
    } finally {
      setAction("");
    }
  }

  async function disconnect(account: SocialAccount) {
    if (!window.confirm(`Disconnect ${account.display_name}? This removes the saved token from Socialdesk.`)) return;
    setAction(account.id); setError(""); setMessage("");
    try {
      const freshCsrfToken = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/accounts/${account.id}`, {
        method: "DELETE", credentials: "include", cache: "no-store",
        headers: { "X-CSRF-Token": freshCsrfToken },
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Disconnect failed (${response.status})`));
      setMessage(`${account.display_name} was disconnected from this workspace.`);
      await loadAccounts();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not disconnect this account.");
    } finally { setAction(""); }
  }

  async function refreshToken(account: SocialAccount) {
    setAction(account.id); setError(""); setMessage("");
    try {
      const freshCsrfToken = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/accounts/${account.id}/refresh`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "X-CSRF-Token": freshCsrfToken },
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Token refresh failed (${response.status})`));
      setMessage(`Access token refreshed for ${account.display_name}.`);
      await loadAccounts();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not refresh this token.");
    } finally { setAction(""); }
  }

  const connected = data?.items.filter((item) => item.status !== "disconnected") ?? [];
  const youtubeCount = connected.filter((item) => item.platform === "youtube").length;
  const instagramCount = connected.filter((item) => item.platform === "instagram").length;

  useEffect(() => {
    onCountChange({ total: connected.length, youtube: youtubeCount, instagram: instagramCount });
  }, [connected.length, youtubeCount, instagramCount, onCountChange]);

  return (
    <section className="panel-card accounts-panel" id="accounts">
      <div className="panel-heading accounts-heading">
        <div><h2>Connected accounts</h2><p>Connect official accounts. Tokens stay on the backend and are never shown here.</p></div>
        <span className="account-count">{connected.length}/{data?.account_limit ?? 50}</span>
      </div>
      {canManage && <div className="connect-actions">
        <button className="connect-button youtube-connect" type="button" onClick={() => void connect("youtube")} disabled={Boolean(action) || !data?.token_encryption_ready || !data?.platform_setup.youtube}>
          <span className="platform-logo youtube-logo">▶</span><span><strong>Connect YouTube</strong><small>{data?.platform_setup.youtube ? "Channel authorization" : "Add Google OAuth credentials"}</small></span><span className="button-arrow">↗</span>
        </button>
        <button className="connect-button instagram-connect" type="button" onClick={() => void connect("instagram")} disabled={Boolean(action) || !data?.token_encryption_ready || !data?.platform_setup.instagram}>
          <span className="platform-logo instagram-logo">◎</span><span><strong>Connect Instagram</strong><small>{data?.platform_setup.instagram ? "Business / Creator account" : "Add Meta OAuth credentials"}</small></span><span className="button-arrow">↗</span>
        </button>
      </div>}
      {!canManage && <p className="accounts-footnote">Your role is read-only for account connections. Ask a workspace owner or admin to connect, refresh, or disconnect accounts.</p>}
      {data && !data.token_encryption_ready && <div className="inline-warning">Set a valid TOKEN_ENCRYPTION_KEY in the backend environment before connecting accounts.</div>}
      {error && <div className="inline-warning" role="alert">{error}</div>}
      {message && <div className="inline-success" role="status">{message}</div>}
      <div className="account-platform-summary">
        <span><b className="summary-dot youtube-dot" /> YouTube <strong>{youtubeCount}</strong></span>
        <span><b className="summary-dot instagram-dot" /> Instagram <strong>{instagramCount}</strong></span>
      </div>
      {loading ? <div className="empty-accounts">Loading connected accounts…</div> : connected.length === 0 ? (
        <div className="empty-accounts"><span className="empty-account-icon">◎</span><strong>No accounts connected yet</strong><p>Choose a platform above. You will be redirected to its official authorization page.</p></div>
      ) : (
        <div className="account-list">
          {connected.map((account) => (
            <article className="account-row" key={account.id}>
              <div className={`account-platform-mark ${account.platform}`}>{account.platform === "youtube" ? "▶" : "◎"}</div>
              <div className="account-main"><strong>{account.display_name}</strong><small>{account.username ? `${account.platform === "instagram" ? "@" : ""}${account.username} · ` : ""}{account.platform === "youtube" ? "YouTube channel" : `Instagram ${account.account_type ?? "professional"}`}</small><small>{friendlyExpiry(account.access_token_expires_at)}</small></div>
              <span className={`account-status ${account.status}`}>{account.status === "connected" ? "Connected" : "Reconnect needed"}</span>
              {canManage && <div className="account-actions"><button className="subtle-button" type="button" onClick={() => void refreshToken(account)} disabled={Boolean(action)} title="Refresh access token">{action === account.id ? "Working…" : "Refresh"}</button><button className="icon-button danger-button" type="button" onClick={() => void disconnect(account)} disabled={Boolean(action)} aria-label={`Disconnect ${account.display_name}`} title="Disconnect">×</button></div>}
            </article>
          ))}
        </div>
      )}
      <p className="accounts-footnote">Disconnect removes credentials stored here; it does not revoke the app grant at Google/Instagram. Use the provider's connected-app settings for full revocation.</p>
    </section>
  );
}
