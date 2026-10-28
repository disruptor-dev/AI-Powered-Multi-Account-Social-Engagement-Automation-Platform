"use client";

import { useCallback, useEffect, useState } from "react";
import { explainApiError, fetchCsrfToken } from "@/lib/csrf";

type Platform = "youtube" | "instagram";
type Tone = "helpful" | "friendly" | "professional" | "empathetic" | "concise";
type Language = "match_comment" | "en" | "hi" | "hinglish" | "es" | "fr" | "de";
type Account = { id: string; platform: Platform; display_name: string; status: string };
type Conditions = {
  platform: "all" | Platform;
  account_ids: string[];
  keywords_any: string[];
  keywords_all: string[];
  exclude_keywords: string[];
  author_contains: string | null;
  min_likes: number;
  only_unanswered: boolean;
  only_open: boolean;
  include_replies: boolean;
  exclude_owner_comments: boolean;
};
type DraftSettings = { tone: Tone; language: Language; custom_instructions: string | null };
type Rule = { id: string; name: string; description: string | null; enabled: boolean; priority: number; conditions: Conditions; draft_settings: DraftSettings; last_run_at: string | null; action: string };
type Run = { id: string; rule_id: string | null; status: string; trigger: string; scanned_count: number; matched_count: number; drafts_queued_count: number; skipped_count: number; failed_count: number; error_code: string | null; queued_at: string | null; finished_at: string | null };
type Execution = { id: string; comment_id: string; ai_draft_id: string | null; status: string; error_code: string | null; created_at: string | null };
type Props = { apiBaseUrl: string; onGoInbox: () => void; canEdit: boolean };
type ApiErrorBody = { detail?: unknown };

const emptyConditions = (): Conditions => ({ platform: "all", account_ids: [], keywords_any: [], keywords_all: [], exclude_keywords: [], author_contains: null, min_likes: 0, only_unanswered: true, only_open: true, include_replies: false, exclude_owner_comments: true });
const emptyDraftSettings = (): DraftSettings => ({ tone: "helpful", language: "match_comment", custom_instructions: null });
const splitWords = (value: string) => value.split(",").map((item) => item.trim()).filter(Boolean).slice(0, 20);

export default function AutomationRulesPanel({ apiBaseUrl, onGoInbox, canEdit }: Props) {
  const [rules, setRules] = useState<Rule[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [configured, setConfigured] = useState(false);
  const [formName, setFormName] = useState("");
  const [description, setDescription] = useState("");
  const [platform, setPlatform] = useState<"all" | Platform>("all");
  const [accountIds, setAccountIds] = useState<string[]>([]);
  const [keywordsAny, setKeywordsAny] = useState("");
  const [keywordsAll, setKeywordsAll] = useState("");
  const [excludeKeywords, setExcludeKeywords] = useState("");
  const [authorContains, setAuthorContains] = useState("");
  const [minLikes, setMinLikes] = useState("0");
  const [onlyUnanswered, setOnlyUnanswered] = useState(true);
  const [onlyOpen, setOnlyOpen] = useState(true);
  const [includeReplies, setIncludeReplies] = useState(false);
  const [excludeOwner, setExcludeOwner] = useState(true);
  const [tone, setTone] = useState<Tone>("helpful");
  const [language, setLanguage] = useState<Language>("match_comment");
  const [instructions, setInstructions] = useState("");
  const [priority, setPriority] = useState("100");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [actionId, setActionId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [runs, setRuns] = useState<Record<string, Run>>({});
  const [executions, setExecutions] = useState<Record<string, Execution[]>>({});

  const readJson = useCallback(async (url: string) => {
    const response = await fetch(url, { credentials: "include", cache: "no-store" });
    const body = await response.json().catch(() => ({})) as ApiErrorBody & Record<string, unknown>;
    if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Request failed (${response.status})`));
    return body;
  }, []);

  const load = useCallback(async () => {
    const [ruleBody, accountBody, aiBody] = await Promise.all([
      readJson(`${apiBaseUrl}/api/v1/automation/rules`),
      readJson(`${apiBaseUrl}/api/v1/comments/sync-status`),
      readJson(`${apiBaseUrl}/api/v1/automation/status`),
    ]);
    setRules(Array.isArray(ruleBody.items) ? ruleBody.items as Rule[] : []);
    setAccounts(Array.isArray(accountBody.accounts) ? accountBody.accounts as Account[] : []);
    setConfigured(Boolean(aiBody.ai_drafting_configured));
  }, [apiBaseUrl, readJson]);

  useEffect(() => { void load().catch((err: unknown) => setError(err instanceof Error ? err.message : "Unable to load automation rules.")); }, [load]);

  useEffect(() => {
    const activeRuns = Object.values(runs).filter((run) => ["queued", "running"].includes(run.status));
    if (activeRuns.length === 0) return;
    let active = true;
    const poll = async () => {
      for (const run of activeRuns) {
        try {
          const body = await readJson(`${apiBaseUrl}/api/v1/automation/runs/${run.id}`) as unknown as Run;
          if (active) setRuns((current) => ({ ...current, [body.id]: body }));
        } catch { /* keep the last visible status during transient polling failures */ }
      }
      if (active) {
        const finished = activeRuns.filter((run) => !["queued", "running"].includes(runs[run.id]?.status ?? run.status));
        if (finished.length) void load().catch(() => undefined);
      }
    };
    const timer = window.setInterval(() => void poll(), 3000);
    void poll();
    return () => { active = false; window.clearInterval(timer); };
  }, [apiBaseUrl, load, readJson, runs]);

  async function mutate(url: string, method: "POST" | "PATCH" | "DELETE", body?: unknown) {
    const csrf = await fetchCsrfToken(apiBaseUrl);
    const response = await fetch(url, {
      method, credentials: "include", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (response.status === 204) return {};
    const result = await response.json().catch(() => ({})) as ApiErrorBody & Record<string, unknown>;
    if (!response.ok) throw new Error(explainApiError(response.status, result.detail, `Request failed (${response.status})`));
    return result;
  }

  function resetForm() {
    setEditingId(null); setFormName(""); setDescription(""); setPlatform("all"); setAccountIds([]);
    setKeywordsAny(""); setKeywordsAll(""); setExcludeKeywords(""); setAuthorContains(""); setMinLikes("0");
    setOnlyUnanswered(true); setOnlyOpen(true); setIncludeReplies(false); setExcludeOwner(true);
    setTone("helpful"); setLanguage("match_comment"); setInstructions(""); setPriority("100");
  }

  function editRule(rule: Rule) {
    setEditingId(rule.id); setFormName(rule.name); setDescription(rule.description ?? "");
    setPlatform(rule.conditions.platform ?? "all"); setAccountIds(rule.conditions.account_ids ?? []);
    setKeywordsAny((rule.conditions.keywords_any ?? []).join(", ")); setKeywordsAll((rule.conditions.keywords_all ?? []).join(", "));
    setExcludeKeywords((rule.conditions.exclude_keywords ?? []).join(", ")); setAuthorContains(rule.conditions.author_contains ?? "");
    setMinLikes(String(rule.conditions.min_likes ?? 0)); setOnlyUnanswered(rule.conditions.only_unanswered ?? true);
    setOnlyOpen(rule.conditions.only_open ?? true); setIncludeReplies(rule.conditions.include_replies ?? false);
    setExcludeOwner(rule.conditions.exclude_owner_comments ?? true); setTone(rule.draft_settings.tone ?? "helpful");
    setLanguage(rule.draft_settings.language ?? "match_comment"); setInstructions(rule.draft_settings.custom_instructions ?? "");
    setPriority(String(rule.priority ?? 100)); setNotice(`Editing “${rule.name}”.`); setError("");
    document.getElementById("automation-rule-form")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function saveRule() {
    if (saving || !formName.trim()) return;
    setSaving(true); setError(""); setNotice("");
    const editingRule = editingId ? rules.find((rule) => rule.id === editingId) : undefined;
    const data = {
      name: formName.trim(), description: description.trim() || null, enabled: editingRule?.enabled ?? true,
      priority: Number(priority) || 100,
      conditions: {
        platform, account_ids: accountIds, keywords_any: splitWords(keywordsAny), keywords_all: splitWords(keywordsAll),
        exclude_keywords: splitWords(excludeKeywords), author_contains: authorContains.trim() || null,
        min_likes: Math.max(0, Math.min(1000000, Number(minLikes) || 0)), only_unanswered: onlyUnanswered,
        only_open: onlyOpen, include_replies: includeReplies, exclude_owner_comments: excludeOwner,
      },
      draft_settings: { tone, language, custom_instructions: instructions.trim() || null },
    };
    try {
      const body = await mutate(editingId ? `${apiBaseUrl}/api/v1/automation/rules/${editingId}` : `${apiBaseUrl}/api/v1/automation/rules`, editingId ? "PATCH" : "POST", data);
      setNotice(editingId ? "Automation rule updated." : "Automation rule created. It will generate drafts for review only.");
      resetForm();
      setRules((current) => editingId ? current.map((item) => item.id === body.id ? body as unknown as Rule : item) : [body as unknown as Rule, ...current]);
      await load();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not save the rule."); }
    finally { setSaving(false); }
  }

  async function toggleRule(rule: Rule) {
    setActionId(rule.id); setError(""); setNotice("");
    try {
      const updated = await mutate(`${apiBaseUrl}/api/v1/automation/rules/${rule.id}`, "PATCH", { enabled: !rule.enabled }) as unknown as Rule;
      setRules((current) => current.map((item) => item.id === rule.id ? updated : item));
      setNotice(`Rule ${updated.enabled ? "enabled" : "paused"}.`);
    } catch (err) { setError(err instanceof Error ? err.message : "Could not update this rule."); }
    finally { setActionId(""); }
  }

  async function deleteRule(rule: Rule) {
    if (!window.confirm(`Delete the rule “${rule.name}”? Existing drafts will remain in the inbox.`)) return;
    setActionId(rule.id); setError(""); setNotice("");
    try {
      await mutate(`${apiBaseUrl}/api/v1/automation/rules/${rule.id}`, "DELETE");
      setRules((current) => current.filter((item) => item.id !== rule.id));
      setExecutions((current) => { const next = { ...current }; delete next[rule.id]; return next; });
      if (editingId === rule.id) resetForm();
      setNotice("Rule deleted. Existing AI drafts have been kept for review.");
    } catch (err) { setError(err instanceof Error ? err.message : "Could not delete this rule."); }
    finally { setActionId(""); }
  }

  async function runRule(rule: Rule) {
    setActionId(rule.id); setError(""); setNotice("");
    try {
      const body = await mutate(`${apiBaseUrl}/api/v1/automation/rules/${rule.id}/run`, "POST", { confirm_draft_only: true }) as unknown as Run;
      setRuns((current) => ({ ...current, [body.id]: body }));
      setNotice(`Run queued for “${rule.name}”. Matching comments will receive AI drafts; nothing will be published automatically.`);
      await load();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not run this rule."); }
    finally { setActionId(""); }
  }

  async function loadExecutions(rule: Rule) {
    setActionId(`executions-${rule.id}`); setError("");
    try {
      const body = await readJson(`${apiBaseUrl}/api/v1/automation/rules/${rule.id}/executions`) as { items?: Execution[] };
      setExecutions((current) => ({ ...current, [rule.id]: Array.isArray(body.items) ? body.items : [] }));
    } catch (err) { setError(err instanceof Error ? err.message : "Could not load execution history."); }
    finally { setActionId(""); }
  }

  const activeAccounts = accounts.filter((account) => account.status === "connected" && (platform === "all" || account.platform === platform));

  return (
    <section className="automation-workspace">
      <div className="welcome-row automation-welcome"><div><p className="eyebrow">PHASE 4 · AI ASSISTANCE</p><h1>Turn comment signals into reviewed replies.</h1><p className="page-subtitle">Generate contextual reply drafts and build repeatable rules without enabling unattended publishing.</p></div><div className="date-chip"><span>✧</span> Human review required</div></div>
      <section className="panel-card ai-config-panel"><div className="panel-heading"><div><h2>AI reply service</h2><p>Provider settings are read from the backend environment; the secret never reaches the browser.</p></div><span className={`ai-config-pill ${configured ? "ready" : "missing"}`}>{configured ? "CONFIGURED" : "SETUP REQUIRED"}</span></div>{configured ? <p className="automation-safe-note">AI drafts are suggestions, not facts. Review and edit every draft before replying. Rules cannot publish to YouTube or Instagram automatically.</p> : <div className="inline-warning">Set <code>AI_REPLY_ENABLED=true</code>, <code>AI_API_KEY</code>, <code>AI_BASE_URL</code>, and <code>AI_MODEL</code> in the backend environment, then rebuild/restart the API and worker. Never put the key in a <code>NEXT_PUBLIC_*</code> variable.</div>}</section>

      {(error || notice) && <div className={error ? "automation-feedback error" : "automation-feedback"} role="status">{error || notice}</div>}

      {canEdit && <section className="panel-card automation-form-card" id="automation-rule-form">
        <div className="panel-heading"><div><h2>{editingId ? "Edit automation rule" : "Create automation rule"}</h2><p>Rules inspect recent comments and queue one AI draft for each newly matched comment.</p></div><span className="muted-label">DRAFT-ONLY ACTION</span></div>
        <div className="automation-form-grid">
          <label><span>Rule name</span><input value={formName} onChange={(event) => setFormName(event.target.value)} maxLength={120} placeholder="e.g. Product questions" /></label>
          <label><span>Priority (lower runs first)</span><input type="number" min={1} max={1000} value={priority} onChange={(event) => setPriority(event.target.value)} /></label>
          <label className="automation-wide"><span>Description (optional)</span><input value={description} onChange={(event) => setDescription(event.target.value)} maxLength={500} placeholder="What should this rule help the team handle?" /></label>
          <label><span>Platform</span><select value={platform} onChange={(event) => { setPlatform(event.target.value as "all" | Platform); setAccountIds([]); }}><option value="all">All platforms</option><option value="youtube">YouTube only</option><option value="instagram">Instagram only</option></select></label>
          <label><span>Minimum likes</span><input type="number" min={0} max={1000000} value={minLikes} onChange={(event) => setMinLikes(event.target.value)} /></label>
          <label className="automation-wide"><span>Only selected connected accounts (optional)</span><div className="automation-account-list">{activeAccounts.length === 0 ? <small>No connected accounts match this platform.</small> : activeAccounts.map((account) => <label className="automation-checkbox" key={account.id}><input type="checkbox" checked={accountIds.includes(account.id)} onChange={(event) => setAccountIds((current) => event.target.checked ? [...current, account.id] : current.filter((id) => id !== account.id))} /><span>{account.display_name}<small>{account.platform}</small></span></label>)}</div><small>Leave all unchecked to include any connected account on the selected platform.</small></label>
          <label className="automation-wide"><span>Match any keyword (comma-separated; optional)</span><input value={keywordsAny} onChange={(event) => setKeywordsAny(event.target.value)} placeholder="price, how much, cost" /></label>
          <label className="automation-wide"><span>Require all keywords (comma-separated; optional)</span><input value={keywordsAll} onChange={(event) => setKeywordsAll(event.target.value)} placeholder="tutorial, setup" /></label>
          <label className="automation-wide"><span>Exclude comments containing (comma-separated)</span><input value={excludeKeywords} onChange={(event) => setExcludeKeywords(event.target.value)} placeholder="giveaway, subscribe now" /></label>
          <label className="automation-wide"><span>Author name contains (optional)</span><input value={authorContains} onChange={(event) => setAuthorContains(event.target.value)} placeholder="e.g. verified brand or customer segment" /></label>
          <div className="automation-toggles automation-wide">
            <label className="automation-checkbox"><input type="checkbox" checked={onlyUnanswered} onChange={(event) => setOnlyUnanswered(event.target.checked)} /><span>Only unanswered comments</span></label>
            <label className="automation-checkbox"><input type="checkbox" checked={onlyOpen} onChange={(event) => setOnlyOpen(event.target.checked)} /><span>Only open comments</span></label>
            <label className="automation-checkbox"><input type="checkbox" checked={excludeOwner} onChange={(event) => setExcludeOwner(event.target.checked)} /><span>Exclude comments from my own channels</span></label>
            <label className="automation-checkbox"><input type="checkbox" checked={includeReplies} onChange={(event) => setIncludeReplies(event.target.checked)} /><span>Include thread replies</span></label>
          </div>
          <label><span>Reply tone</span><select value={tone} onChange={(event) => setTone(event.target.value as Tone)}><option value="helpful">Helpful</option><option value="friendly">Friendly</option><option value="professional">Professional</option><option value="empathetic">Empathetic</option><option value="concise">Concise</option></select></label>
          <label><span>Reply language</span><select value={language} onChange={(event) => setLanguage(event.target.value as Language)}><option value="match_comment">Match comment language</option><option value="en">English</option><option value="hi">Hindi</option><option value="hinglish">Hinglish</option><option value="es">Spanish</option><option value="fr">French</option><option value="de">German</option></select></label>
          <label className="automation-wide"><span>Additional guidance (optional)</span><textarea value={instructions} onChange={(event) => setInstructions(event.target.value)} maxLength={600} rows={3} placeholder="Tone preferences or approved information the reply may use. Do not enter secrets or personal data." /></label>
        </div>
        <div className="automation-form-actions"><p>Automatic rule checks run on the worker schedule. Each matched comment can create one draft per rule; replies still require explicit review and publishing.</p><div>{editingId && <button type="button" className="subtle-button" onClick={resetForm} disabled={saving}>Cancel edit</button>}<button type="button" className="primary-button" onClick={() => void saveRule()} disabled={saving || !formName.trim()}>{saving ? "Saving…" : editingId ? "Save changes" : "Create rule"}</button></div></div>
      </section>}

      <section className="panel-card automation-rules-list"><div className="panel-heading"><div><h2>Saved automation rules</h2><p>{rules.length} rule{rules.length === 1 ? "" : "s"} · scheduled checks create AI drafts only</p></div><button className="subtle-button" type="button" onClick={() => void load()} disabled={saving}>Refresh</button></div>
        {rules.length === 0 ? <div className="inbox-empty-state"><strong>No automation rules yet</strong><p>Create a rule above to match comment keywords, platforms, accounts, and reply status.</p></div> : <div className="automation-rule-list">{rules.map((rule) => <article className="automation-rule-card" key={rule.id}>
          <div className="automation-rule-top"><div><strong>{rule.name}</strong><p>{rule.description || "No description"}</p></div><span className={`ai-config-pill ${rule.enabled ? "ready" : "missing"}`}>{rule.enabled ? "ENABLED" : "PAUSED"}</span></div>
          <div className="automation-rule-tags"><span>Priority {rule.priority}</span><span>{rule.conditions.platform}</span><span>{rule.conditions.only_unanswered ? "Unanswered" : "Any reply status"}</span><span>Draft-only</span>{rule.conditions.keywords_any?.length ? <span>Any: {rule.conditions.keywords_any.join(", ")}</span> : null}</div>
          <p className="automation-rule-meta">Last run: {rule.last_run_at ? new Date(rule.last_run_at).toLocaleString() : "Not run yet"}</p>
          <div className="automation-rule-actions">{canEdit && <><button type="button" className="subtle-button" onClick={() => void toggleRule(rule)} disabled={Boolean(actionId)}>{actionId === rule.id ? "Saving…" : rule.enabled ? "Pause" : "Enable"}</button><button type="button" className="subtle-button" onClick={() => editRule(rule)} disabled={Boolean(actionId)}>Edit</button></>}<button type="button" className="subtle-button" onClick={() => void loadExecutions(rule)} disabled={Boolean(actionId)}>{actionId === `executions-${rule.id}` ? "Loading…" : "History"}</button>{canEdit && <><button type="button" className="primary-button" onClick={() => void runRule(rule)} disabled={!configured || !rule.enabled || Boolean(actionId)}>{actionId === rule.id ? "Queueing…" : "Run now"}</button><button type="button" className="danger-subtle-button" onClick={() => void deleteRule(rule)} disabled={Boolean(actionId)}>Delete</button></>}</div>
          {Object.values(runs).filter((run) => run.rule_id === rule.id).slice(-1).map((run) => <div className="automation-run-result" key={run.id}><strong>Run: {run.status.replaceAll("_", " ")}</strong><span>Scanned {run.scanned_count} · matched {run.matched_count} · drafts queued {run.drafts_queued_count} · skipped {run.skipped_count} · failed {run.failed_count}</span>{run.error_code && <small>{run.error_code.replaceAll("_", " ")}</small>}</div>)}
          {executions[rule.id] && <div className="automation-execution-list"><strong>Recent matching comments</strong>{executions[rule.id].length === 0 ? <small>No matching executions recorded yet.</small> : executions[rule.id].map((item) => <div key={item.id}><span>{item.status.replaceAll("_", " ")}</span>{item.ai_draft_id && <button type="button" className="text-link-button" onClick={onGoInbox}>Review in inbox ↗</button>}{item.error_code && <small>{item.error_code.replaceAll("_", " ")}</small>}</div>)}</div>}
        </article>)}</div>}
      </section>
      <div className="automation-bottom-note"><strong>Safety by design</strong><p>Rules can select comments and generate suggestions. They cannot post or reply by themselves. Each draft stays editable and must be explicitly approved; publishing uses the existing channel permission checks and records the result in the inbox.</p>{!canEdit && <p>Your viewer role can inspect rule status and history, but cannot change or run rules.</p>}<button type="button" className="subtle-button" onClick={onGoInbox}>Review AI drafts in inbox →</button></div>
    </section>
  );
}
