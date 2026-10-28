"use client";

import { FormEvent, useEffect, useState } from "react";
import { explainApiError, fetchCsrfToken } from "@/lib/csrf";

type Role = "owner" | "admin" | "editor" | "viewer";
type WorkspaceItem = {
  id: string;
  name: string;
  slug: string;
  role: Role;
  status: string;
  current: boolean;
  created_at: string | null;
};

type Props = {
  apiBaseUrl: string;
  currentWorkspaceId: string;
  currentWorkspaceName: string;
  currentRole: Role;
};

export default function WorkspaceSwitcher({ apiBaseUrl, currentWorkspaceId, currentWorkspaceName, currentRole }: Props) {
  const [items, setItems] = useState<WorkspaceItem[]>([]);
  const [canCreate, setCanCreate] = useState(false);
  const [limit, setLimit] = useState(25);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [showRename, setShowRename] = useState(false);
  const [name, setName] = useState("");
  const [rename, setRename] = useState(currentWorkspaceName);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    async function load() {
      setLoading(true);
      try {
        const response = await fetch(`${apiBaseUrl}/api/v1/workspaces`, { credentials: "include", cache: "no-store" });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(explainApiError(response.status, body.detail, "Could not load workspaces."));
        if (!alive) return;
        setItems(Array.isArray(body.items) ? body.items as WorkspaceItem[] : []);
        setCanCreate(Boolean(body.can_create));
        setLimit(Number(body.workspace_limit ?? 25));
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : "Could not load workspaces.");
      } finally {
        if (alive) setLoading(false);
      }
    }
    void load();
    return () => { alive = false; };
  }, [apiBaseUrl, currentWorkspaceId]);

  async function mutate(url: string, method: "POST" | "PATCH", payload: unknown) {
    const csrf = await fetchCsrfToken(apiBaseUrl);
    const response = await fetch(url, {
      method,
      credentials: "include",
      cache: "no-store",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(payload),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Request failed (${response.status})`));
    return body;
  }

  async function switchWorkspace(workspaceId: string) {
    if (!workspaceId || workspaceId === currentWorkspaceId) return;
    setBusy(true); setError("");
    try {
      await mutate(`${apiBaseUrl}/api/v1/workspaces/switch`, "POST", { workspace_id: workspaceId });
      // A full reload discards data cached in mounted panels so the next render
      // obtains data only for the newly signed, membership-authorized workspace.
      window.location.reload();
    } catch (e) { setError(e instanceof Error ? e.message : "Could not switch workspace."); }
    finally { setBusy(false); }
  }

  async function createWorkspace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await mutate(`${apiBaseUrl}/api/v1/workspaces`, "POST", { name });
      window.location.reload();
    } catch (e) { setError(e instanceof Error ? e.message : "Could not create workspace."); }
    finally { setBusy(false); }
  }

  async function renameWorkspace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await mutate(`${apiBaseUrl}/api/v1/workspaces/${currentWorkspaceId}`, "PATCH", { name: rename });
      window.location.reload();
    } catch (e) { setError(e instanceof Error ? e.message : "Could not rename workspace."); }
    finally { setBusy(false); }
  }

  return (
    <section className="workspace-picker" aria-label="Workspace selector">
      <label htmlFor="workspace-select">ACTIVE WORKSPACE</label>
      <div className="workspace-picker-select-row">
        <span className="workspace-icon" aria-hidden="true">W</span>
        <select
          id="workspace-select"
          value={currentWorkspaceId}
          disabled={loading || busy || items.length < 2}
          onChange={(event) => void switchWorkspace(event.target.value)}
          aria-label="Switch active workspace"
        >
          {items.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.role}</option>)}
          {!items.some((item) => item.id === currentWorkspaceId) && <option value={currentWorkspaceId}>{currentWorkspaceName} · {currentRole}</option>}
        </select>
      </div>
      <p className="workspace-picker-caption">{loading ? "Loading workspaces…" : `${items.length} of ${limit} available workspaces`}</p>
      {(currentRole === "owner" || currentRole === "admin") && <div className="workspace-picker-actions">
        {canCreate && <button type="button" className="workspace-mini-button" onClick={() => { setShowCreate((v) => !v); setShowRename(false); setError(""); }}>+ New workspace</button>}
        <button type="button" className="workspace-mini-button muted" onClick={() => { setRename(currentWorkspaceName); setShowRename((v) => !v); setShowCreate(false); setError(""); }}>Rename</button>
      </div>}
      {showCreate && <form className="workspace-mini-form" onSubmit={(event) => void createWorkspace(event)}>
        <label htmlFor="new-workspace-name">Workspace name</label>
        <input id="new-workspace-name" value={name} onChange={(event) => setName(event.target.value)} required minLength={2} maxLength={120} placeholder="Client or brand name" />
        <button type="submit" disabled={busy || name.trim().length < 2}>{busy ? "Creating…" : "Create and switch"}</button>
      </form>}
      {showRename && <form className="workspace-mini-form" onSubmit={(event) => void renameWorkspace(event)}>
        <label htmlFor="rename-workspace-name">Rename current workspace</label>
        <input id="rename-workspace-name" value={rename} onChange={(event) => setRename(event.target.value)} required minLength={2} maxLength={120} />
        <button type="submit" disabled={busy || rename.trim().length < 2}>{busy ? "Saving…" : "Save name"}</button>
      </form>}
      {error && <p className="workspace-picker-error" role="alert">{error}</p>}
    </section>
  );
}
