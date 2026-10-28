"use client";

import { FormEvent, useState } from "react";
import { explainApiError, fetchCsrfToken } from "@/lib/csrf";

type Props = {
  apiBaseUrl: string;
  token: string;
  onCompleted: () => void;
};

export default function PasswordResetPanel({ apiBaseUrl, token, onCompleted }: Props) {
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    if (password !== confirmation) {
      setError("The passwords do not match.");
      return;
    }
    if (password.length < 12) {
      setError("Use at least 12 characters for the new password.");
      return;
    }
    setBusy(true);
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/auth/password-reset/confirm`, {
        method: "POST",
        credentials: "include",
        cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ token, password }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Password reset failed (${response.status})`));
      setPassword("");
      setConfirmation("");
      setDone(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reset the password. Request a new link and try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-shell">
      <section className="login-card">
        <div className="brand-lockup login-brand"><div className="brand-mark">S</div><div><div className="brand-name">socialdesk</div><div className="brand-caption">SECURE ACCOUNT RECOVERY</div></div></div>
        {!done ? <>
          <p className="eyebrow">PASSWORD RESET</p>
          <h1>Choose a new password.</h1>
          <p className="page-subtitle">Use at least 12 characters. This one-time link expires and cannot be reused.</p>
          <form className="login-form" onSubmit={(event) => void handleSubmit(event)}>
            <label htmlFor="reset-password">New password</label>
            <input id="reset-password" type="password" autoComplete="new-password" minLength={12} maxLength={256} required value={password} onChange={(event) => setPassword(event.target.value)} />
            <label htmlFor="reset-password-confirm">Confirm new password</label>
            <input id="reset-password-confirm" type="password" autoComplete="new-password" minLength={12} maxLength={256} required value={confirmation} onChange={(event) => setConfirmation(event.target.value)} />
            {error && <div className="inline-warning login-error" role="alert">{error}</div>}
            <button className="primary-button login-submit" type="submit" disabled={busy}>{busy ? "Updating password…" : "Update password"}</button>
          </form>
        </> : <>
          <p className="eyebrow">PASSWORD UPDATED</p><h1>You’re all set.</h1>
          <p className="page-subtitle">Your password changed and existing member sessions have been revoked. Sign in with the new password.</p>
          <button className="primary-button login-submit" type="button" onClick={onCompleted}>Return to sign in</button>
        </>}
        {!done && <button className="link-button" type="button" onClick={onCompleted}>Back to sign in</button>}
      </section>
    </main>
  );
}
