"use client";

import { FormEvent, useState } from "react";
import { explainApiError, fetchCsrfToken } from "@/lib/csrf";

type Props = {
  apiBaseUrl: string;
  csrfToken: string;
  onLoggedIn: (session: { email: string; workspace_id: string; workspace_name: string; role?: "owner" | "admin" | "editor" | "viewer"; user_id?: string; full_name?: string }) => void;
};

export default function LoginPanel({ apiBaseUrl, csrfToken, onLoggedIn }: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [recovery, setRecovery] = useState(false);
  const [recoveryEmail, setRecoveryEmail] = useState("");
  const [recoveryNotice, setRecoveryNotice] = useState("");

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      // Refresh the double-submit token immediately before the unsafe request so a
      // long-open tab does not submit an expired/stale in-memory CSRF token.
      const freshCsrfToken = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/auth/login`, {
        method: "POST",
        credentials: "include",
        cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": freshCsrfToken },
        body: JSON.stringify({ email, password }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Login failed (${response.status})`));
      setPassword("");
      onLoggedIn(body);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to log in. Check that the backend is running.");
    } finally {
      setBusy(false);
    }
  }

  async function requestReset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setRecoveryNotice("");
    try {
      const freshCsrfToken = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/auth/password-reset/request`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": freshCsrfToken },
        body: JSON.stringify({ email: recoveryEmail }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Request failed (${response.status})`));
      setRecoveryNotice(typeof body.message === "string" ? body.message : "If an eligible account exists, instructions will be emailed shortly.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not submit the request. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-shell">
      <section className="login-card">
        <div className="brand-lockup login-brand">
          <div className="brand-mark">S</div>
          <div><div className="brand-name">socialdesk</div><div className="brand-caption">ENGAGEMENT HUB</div></div>
        </div>
        {recovery ? <>
          <p className="eyebrow">ACCOUNT RECOVERY</p>
          <h1>Reset your password.</h1>
          <p className="page-subtitle">Enter your member account email. For privacy, the confirmation message is the same whether or not the account is eligible.</p>
          <form className="login-form" onSubmit={(event) => void requestReset(event)}>
            <label htmlFor="recovery-email">Email address</label>
            <input id="recovery-email" type="email" autoComplete="email" required maxLength={320} value={recoveryEmail} onChange={(event) => setRecoveryEmail(event.target.value)} placeholder="you@example.com" />
            {error && <div className="inline-warning login-error" role="alert">{error}</div>}
            {recoveryNotice && <div className="login-notice" role="status">{recoveryNotice}</div>}
            <button className="primary-button login-submit" type="submit" disabled={busy || !csrfToken}>{busy ? "Submitting…" : "Send reset link"}</button>
          </form>
          <button className="link-button" type="button" onClick={() => { setRecovery(false); setError(""); setRecoveryNotice(""); }}>Back to sign in</button>
        </> : <>
        <p className="eyebrow">PRIVATE WORKSPACE</p>
        <h1>Welcome back.</h1>
        <p className="page-subtitle">Sign in to access your workspace and assigned role.</p>
        <form className="login-form" onSubmit={handleSubmit}>
          <label htmlFor="admin-email">Email address</label>
          <input id="admin-email" type="email" autoComplete="username" required maxLength={320} value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
          <label htmlFor="admin-password">Password</label>
          <input id="admin-password" type="password" autoComplete="current-password" required maxLength={256} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Your workspace password" />
          {error && <div className="inline-warning login-error" role="alert">{error}</div>}
          <button className="primary-button login-submit" type="submit" disabled={busy || !csrfToken}>{busy ? "Signing in…" : "Sign in securely"}</button>
        </form>
        <button className="link-button" type="button" onClick={() => { setRecovery(true); setRecoveryEmail(email); setError(""); }}>Forgot your password?</button>
        <div className="login-security"><span>⌑</span> Passwords are verified on the backend. Session cookies are HTTP-only.</div>
        {!csrfToken && <p className="form-hint">Preparing secure session… If this stays here, check the API connection.</p>}
        </>}
      </section>
    </main>
  );
}
