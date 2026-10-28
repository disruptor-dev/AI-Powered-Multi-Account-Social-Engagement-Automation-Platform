"use client";

import { useState } from "react";
import type { FormEvent } from "react";
import { explainApiError, fetchCsrfToken } from "@/lib/csrf";

type Session = {
  email: string;
  workspace_id: string;
  workspace_name: string;
  role?: "owner" | "admin" | "editor" | "viewer";
  user_id?: string;
  full_name?: string;
};

type Props = {
  apiBaseUrl: string;
  token: string;
  onAccepted: (session: Session) => void;
  onBackToLogin: () => void;
};

export default function InviteAcceptancePanel({ apiBaseUrl, token, onAccepted, onBackToLogin }: Props) {
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    if (password.length < 12) {
      setError("Use at least 12 characters for your password.");
      return;
    }
    if (password !== confirmPassword) {
      setError("The passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/team/invitations/accept`, {
        method: "POST",
        credentials: "include",
        cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ token, full_name: fullName, password }),
      });
      const body = await response.json().catch(() => ({})) as Session & { detail?: unknown };
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Invitation acceptance failed (${response.status})`));
      setPassword("");
      setConfirmPassword("");
      onAccepted(body);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to accept this invitation.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-shell">
      <section className="login-card invite-card">
        <div className="brand-lockup login-brand"><div className="brand-mark">S</div><div><div className="brand-name">socialdesk</div><div className="brand-caption">ENGAGEMENT HUB</div></div></div>
        <p className="eyebrow">WORKSPACE INVITATION</p>
        <h1>Join the team.</h1>
        <p className="page-subtitle">Set up your member profile to accept this single-use invitation. You will be signed in once the invitation is accepted.</p>
        <form className="login-form" onSubmit={handleSubmit}>
          <label htmlFor="invite-full-name">Full name</label>
          <input id="invite-full-name" autoComplete="name" required maxLength={160} value={fullName} onChange={(e) => setFullName(e.target.value)} placeholder="Your name" />
          <label htmlFor="invite-password">Password</label>
          <input id="invite-password" type="password" autoComplete="new-password" required minLength={12} maxLength={256} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="At least 12 characters" />
          <label htmlFor="invite-password-confirm">Confirm password</label>
          <input id="invite-password-confirm" type="password" autoComplete="new-password" required minLength={12} maxLength={256} value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} placeholder="Repeat your password" />
          {error && <div className="inline-warning login-error" role="alert">{error}</div>}
          <button className="primary-button login-submit" type="submit" disabled={busy || !token}>{busy ? "Accepting invitation…" : "Accept invitation securely"}</button>
        </form>
        <div className="login-security"><span>⌑</span> Invitation links are single-use and expire after 7 days. Passwords are stored as Argon2 hashes.</div>
        <button type="button" className="text-link-button invite-back" onClick={onBackToLogin}>Back to sign in</button>
      </section>
    </main>
  );
}
