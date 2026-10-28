"use client";

import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { explainApiError, fetchCsrfToken } from "@/lib/csrf";

type Role = "owner" | "admin" | "editor" | "viewer";
type Member = { user_id: string; email: string; full_name: string; role: Role; status: "active" | "suspended"; joined_at: string | null; last_login_at: string | null };
type Invitation = { id: string; email: string; role: Exclude<Role, "owner">; status: string; expires_at: string; created_at: string | null };
type Audit = { id: string; actor_email: string; action: string; entity_type: string; entity_id: string | null; details: Record<string, unknown>; created_at: string | null };
type Capabilities = { role: Role; can_manage_team: boolean; can_view_audit_log: boolean; active_member_count: number; member_limit: number };
type Props = { apiBaseUrl: string; currentEmail: string; currentRole: Role };
type ApiBody = { detail?: unknown } & Record<string, unknown>;

function dateLabel(value: string | null | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

export default function TeamManagementPanel({ apiBaseUrl, currentEmail, currentRole }: Props) {
  const [members, setMembers] = useState<Member[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Exclude<Role, "owner">>("editor");
  const [createdLink, setCreatedLink] = useState("");
  const [activeTab, setActiveTab] = useState<"members" | "invitations" | "audit">("members");
  const [busy, setBusy] = useState(false);
  const [itemBusy, setItemBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const read = useCallback(async (url: string) => {
    const response = await fetch(url, { credentials: "include", cache: "no-store" });
    const body = await response.json().catch(() => ({})) as ApiBody;
    if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Request failed (${response.status})`));
    return body;
  }, []);

  const load = useCallback(async () => {
    const [cap, memberBody] = await Promise.all([
      read(`${apiBaseUrl}/api/v1/team/capabilities`),
      read(`${apiBaseUrl}/api/v1/team/members`),
    ]);
    setCapabilities(cap as unknown as Capabilities);
    setMembers(Array.isArray(memberBody.items) ? memberBody.items as Member[] : []);
    const currentCaps = cap as unknown as Capabilities;
    if (currentCaps.can_manage_team) {
      const inviteBody = await read(`${apiBaseUrl}/api/v1/team/invitations`);
      setInvitations(Array.isArray(inviteBody.items) ? inviteBody.items as Invitation[] : []);
    } else {
      setInvitations([]);
    }
    if (currentCaps.can_view_audit_log) {
      const auditBody = await read(`${apiBaseUrl}/api/v1/team/audit-log?limit=50&offset=0`);
      setAudit(Array.isArray((auditBody as ApiBody & { items?: unknown }).items) ? (auditBody as ApiBody & { items: Audit[] }).items : []);
    } else {
      setAudit([]);
    }
  }, [apiBaseUrl, read]);

  useEffect(() => {
    void load().catch((err: unknown) => setError(err instanceof Error ? err.message : "Unable to load workspace team."));
  }, [load]);

  async function mutate(url: string, method: "POST" | "PATCH" | "DELETE", body?: unknown) {
    const csrf = await fetchCsrfToken(apiBaseUrl);
    const response = await fetch(url, {
      method, credentials: "include", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const result = await response.json().catch(() => ({})) as ApiBody;
    if (!response.ok) throw new Error(explainApiError(response.status, result.detail, `Request failed (${response.status})`));
    return result;
  }

  async function inviteMember(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError(""); setNotice(""); setCreatedLink("");
    try {
      const result = await mutate(`${apiBaseUrl}/api/v1/team/invitations`, "POST", { email, role });
      setCreatedLink(String(result.accept_url ?? ""));
      setEmail("");
      const delivery = String(result.delivery ?? "copy_link");
      setNotice(delivery === "queued"
        ? "Invitation created and queued for email delivery. You can also copy the one-time link as a fallback."
        : delivery === "copy_link_fallback"
          ? "Invitation created, but email delivery could not be queued. Copy the one-time link and deliver it securely."
          : "Invitation created. SMTP is not configured, so copy the one-time link and deliver it securely.");
      await load();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not create invitation."); }
    finally { setBusy(false); }
  }

  async function updateMember(member: Member, patch: { role?: Role; status?: "active" | "suspended" }) {
    setItemBusy(member.user_id); setError(""); setNotice("");
    try {
      await mutate(`${apiBaseUrl}/api/v1/team/members/${member.user_id}`, "PATCH", patch);
      setNotice(`Updated ${member.email}.`); await load();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not update member."); }
    finally { setItemBusy(""); }
  }

  async function removeMember(member: Member) {
    if (!window.confirm(`Remove ${member.email} from this workspace?`)) return;
    setItemBusy(member.user_id); setError(""); setNotice("");
    try {
      await mutate(`${apiBaseUrl}/api/v1/team/members/${member.user_id}`, "DELETE");
      setNotice(`${member.email} was removed from the workspace.`); await load();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not remove member."); }
    finally { setItemBusy(""); }
  }

  async function revokeInvitation(invitation: Invitation) {
    if (!window.confirm(`Revoke the invitation for ${invitation.email}?`)) return;
    setItemBusy(invitation.id); setError(""); setNotice("");
    try {
      await mutate(`${apiBaseUrl}/api/v1/team/invitations/${invitation.id}`, "DELETE");
      setNotice(`Invitation for ${invitation.email} revoked.`); await load();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not revoke invitation."); }
    finally { setItemBusy(""); }
  }

  async function copyInviteLink() {
    if (!createdLink) return;
    try { await navigator.clipboard.writeText(createdLink); setNotice("Invitation link copied to clipboard."); }
    catch { setNotice("Clipboard access was blocked. Select and copy the invitation link manually."); }
  }

  const canManage = capabilities?.can_manage_team ?? (currentRole === "owner" || currentRole === "admin");
  const canAudit = capabilities?.can_view_audit_log ?? (currentRole === "owner" || currentRole === "admin");

  return (
    <div className="team-workspace">
      <div className="welcome-row team-welcome"><div><p className="eyebrow">WORKSPACE GOVERNANCE</p><h1>People, roles, and accountability.</h1><p className="page-subtitle">Invite trusted collaborators and control what each member can see or change.</p></div><div className="date-chip">Role · {currentRole}</div></div>
      {error && <div className="automation-feedback error" role="alert">{error}</div>}
      {notice && <div className="automation-feedback" role="status">{notice}</div>}

      <div className="team-summary-grid">
        <article className="metric-card"><div className="metric-label">Your access</div><div className="metric-value team-metric-value">{currentRole}</div><div className="metric-foot">Signed in as {currentEmail}</div></article>
        <article className="metric-card"><div className="metric-label">Active members</div><div className="metric-value team-metric-value">{capabilities?.active_member_count ?? members.filter((m) => m.status === "active").length}<span> / {capabilities?.member_limit ?? 25}</span></div><div className="metric-foot">Includes the workspace owner</div></article>
        <article className="metric-card"><div className="metric-label">Pending invitations</div><div className="metric-value team-metric-value">{invitations.filter((item) => item.status === "pending").length}</div><div className="metric-foot">Invitation links expire after seven days</div></article>
      </div>

      {canManage && <section className="panel-card team-invite-card">
        <div className="panel-heading"><div><h2>Invite a teammate</h2><p>Invitations are single-use and expire in seven days. When SMTP is configured, the invite is queued for email delivery.</p></div><span className="live-label">SECURE LINK</span></div>
        <form className="team-invite-form" onSubmit={(event) => void inviteMember(event)}>
          <label><span>Email address</span><input type="email" required maxLength={320} value={email} onChange={(event) => setEmail(event.target.value)} placeholder="colleague@company.com" /></label>
          <label><span>Workspace role</span><select value={role} onChange={(event) => setRole(event.target.value as Exclude<Role, "owner">)}><option value="viewer">Viewer — read-only</option><option value="editor">Editor — inbox, drafts and publishing</option>{currentRole === "owner" && <option value="admin">Admin — accounts and team management</option>}</select></label>
          <button type="submit" className="primary-button" disabled={busy || (capabilities ? capabilities.active_member_count >= capabilities.member_limit : false)}>{busy ? "Creating…" : "Create invitation"}</button>
        </form>
        {createdLink && <div className="team-created-link"><label htmlFor="invite-link-output">One-time invitation link</label><div><input id="invite-link-output" value={createdLink} readOnly onFocus={(event) => event.currentTarget.select()} /><button type="button" className="subtle-button" onClick={() => void copyInviteLink()}>Copy link</button></div><small>Share securely with the intended invitee. Anyone with the link can attempt acceptance for that email. Links are single-use and should be shared privately.</small></div>}
      </section>}

      <section className="panel-card team-data-card">
        <div className="team-tabs" role="tablist" aria-label="Workspace administration views">
          <button type="button" className={activeTab === "members" ? "active" : ""} onClick={() => setActiveTab("members")}>Members <span>{members.length}</span></button>
          {canManage && <button type="button" className={activeTab === "invitations" ? "active" : ""} onClick={() => setActiveTab("invitations")}>Invitations <span>{invitations.length}</span></button>}
          {canAudit && <button type="button" className={activeTab === "audit" ? "active" : ""} onClick={() => setActiveTab("audit")}>Audit log <span>{audit.length}</span></button>}
        </div>

        {activeTab === "members" && <div className="team-table-wrap"><table className="team-table"><thead><tr><th>Member</th><th>Role</th><th>Status</th><th>Last sign-in</th>{canManage && <th>Actions</th>}</tr></thead><tbody>
          {members.map((member) => <tr key={member.user_id}><td><strong>{member.full_name}</strong><small>{member.email}</small></td><td>{canManage && member.role !== "owner" && member.email !== currentEmail && (currentRole === "owner" || member.role !== "admin") ? <select aria-label={`Role for ${member.email}`} value={member.role} disabled={itemBusy === member.user_id} onChange={(event) => void updateMember(member, { role: event.target.value as Role })}><option value="viewer">Viewer</option><option value="editor">Editor</option>{currentRole === "owner" && <option value="admin">Admin</option>}</select> : <span className={`team-role ${member.role}`}>{member.role}</span>}</td><td><span className={`team-status ${member.status}`}>{member.status}</span></td><td>{dateLabel(member.last_login_at)}</td>{canManage && <td><div className="team-row-actions">{member.role !== "owner" && member.email !== currentEmail && <><button type="button" className="subtle-button" disabled={itemBusy === member.user_id} onClick={() => void updateMember(member, { status: member.status === "active" ? "suspended" : "active" })}>{member.status === "active" ? "Suspend" : "Reactivate"}</button><button type="button" className="danger-subtle-button" disabled={itemBusy === member.user_id} onClick={() => void removeMember(member)}>Remove</button></>}</div></td>}</tr>)}
          {members.length === 0 && <tr><td colSpan={canManage ? 5 : 4} className="team-empty">No members found.</td></tr>}
        </tbody></table></div>}

        {activeTab === "invitations" && canManage && <div className="team-table-wrap"><table className="team-table"><thead><tr><th>Invitee</th><th>Role</th><th>Status</th><th>Expires</th><th>Action</th></tr></thead><tbody>
          {invitations.map((invitation) => <tr key={invitation.id}><td><strong>{invitation.email}</strong><small>Created {dateLabel(invitation.created_at)}</small></td><td><span className={`team-role ${invitation.role}`}>{invitation.role}</span></td><td><span className={`team-status ${invitation.status}`}>{invitation.status}</span></td><td>{dateLabel(invitation.expires_at)}</td><td>{invitation.status === "pending" && <button type="button" className="danger-subtle-button" disabled={itemBusy === invitation.id} onClick={() => void revokeInvitation(invitation)}>Revoke</button>}</td></tr>)}
          {invitations.length === 0 && <tr><td colSpan={5} className="team-empty">No invitations yet.</td></tr>}
        </tbody></table></div>}

        {activeTab === "audit" && canAudit && <div className="team-audit-list">{audit.map((event) => <article key={event.id} className="team-audit-event"><div className="team-audit-symbol">↳</div><div><strong>{event.action.replaceAll(".", " · ")}</strong><p>{event.actor_email} · {event.entity_type}{event.details && Object.keys(event.details).length ? ` · ${Object.entries(event.details).map(([k, v]) => `${k}: ${String(v)}`).join(" · ")}` : ""}</p><small>{dateLabel(event.created_at)}</small></div></article>)}{audit.length === 0 && <p className="team-empty">Team changes will appear here.</p>}</div>}
      </section>
      <div className="automation-bottom-note"><strong>Role permissions</strong><p>Owner: all actions. Admin: team administration and connected accounts, but cannot modify another admin or the owner. Editor: inbox actions, drafts, and publishing. Viewer: read-only. Suspended members lose access immediately.</p></div>
    </div>
  );
}
