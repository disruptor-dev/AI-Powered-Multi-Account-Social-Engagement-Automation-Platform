"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { explainApiError, fetchCsrfToken } from "@/lib/csrf";

type QueueAccount = {
  id: string;
  platform: "youtube" | "instagram";
  display_name: string;
  username: string | null;
  status: string;
  can_publish_replies: boolean;
};

type QueueItem = {
  id: string;
  account_id: string;
  account_name: string;
  account_status: string;
  status: "queued_generation" | "generating" | "pending_approval" | "scheduled" | "publishing" | "posted" | "failed" | "blocked_quota" | "outcome_unknown" | "cancelled";
  draft_source: "human" | "legacy_ai";
  video_id: string;
  video_url: string;
  video_title: string;
  video_description: string;
  thumbnail_url: string | null;
  tone: string;
  language: string;
  custom_instructions: string | null;
  draft_text: string | null;
  approved_text: string | null;
  quality_warnings: string[];
  error_code: string | null;
  provider_comment_id: string | null;
  scheduled_at: string | null;
  created_at: string | null;
  generated_at: string | null;
  approved_at: string | null;
  approved_by_email: string | null;
  started_at: string | null;
  published_at: string | null;
  finished_at: string | null;
  attempt_count: number;
};

type QueueState = { items: QueueItem[]; queue_paused: boolean; pause_reason: string | null; paused_at: string | null };
type Confirmation = { target: boolean; relevance: boolean };
type OutcomeResolutionInput = { providerCommentId: string; note: string; confirmed: boolean };
type Props = { apiBaseUrl: string; accounts: QueueAccount[]; canAdmin: boolean };

const STATUS_LABELS: Record<QueueItem["status"], string> = {
  queued_generation: "Legacy AI item retired",
  generating: "Legacy AI item retired",
  pending_approval: "Pending approval",
  scheduled: "Scheduled",
  publishing: "Publishing",
  posted: "Posted",
  failed: "Failed",
  blocked_quota: "Blocked by quota",
  outcome_unknown: "Outcome unknown",
  cancelled: "Cancelled",
};

function localDateTimeOffset(minutes = 60): string {
  const date = new Date(Date.now() + minutes * 60_000);
  date.setSeconds(0, 0);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function displayDate(value: string | null): string {
  if (!value) return "Not set";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Time unavailable" : date.toLocaleString();
}

function explainQueueError(code: string | null): string {
  if (!code) return "";
  const labels: Record<string, string> = {
    human_comment_required: "This legacy queue item was retired because the Human YouTube Comment Queue now accepts manual text only. Create a new human-written comment.",
    legacy_ai_draft_disabled: "This older AI-generated item was cancelled. Create a new human-written comment instead.",
    provider_quota_exceeded: "YouTube API quota was exceeded. The queue is paused until an admin verifies quota availability.",
    provider_permission_denied: "YouTube denied the write. Check channel authorization and the video's comment settings.",
    provider_reauthorization_required: "Reconnect the selected channel and grant comment access.",
    comment_publish_scope_missing: "Reconnect the channel and grant YouTube comment access.",
    youtube_video_unavailable: "The target video is unavailable or restricted.",
    youtube_comments_unavailable: "Comments are disabled or unavailable for this video.",
    provider_outcome_unknown: "YouTube may have accepted the comment before the connection failed. Check YouTube manually; this item will not be retried automatically.",
    manual_verified_not_posted: "An administrator checked YouTube and confirmed that the comment was not published.",
    legacy_multi_channel_publishing_disabled: "The legacy multi-channel publisher is disabled. Use the human-approval queue.",
  };
  return labels[code] ?? code.replaceAll("_", " ");
}

export default function YouTubeCommentQueue({ apiBaseUrl, accounts, canAdmin }: Props) {
  const [videoUrl, setVideoUrl] = useState("");
  const [accountId, setAccountId] = useState("");
  const [commentText, setCommentText] = useState("");
  const [items, setItems] = useState<QueueItem[]>([]);
  const [queuePaused, setQueuePaused] = useState(false);
  const [pauseReason, setPauseReason] = useState<string | null>(null);
  const [draftTexts, setDraftTexts] = useState<Record<string, string>>({});
  const [scheduleTimes, setScheduleTimes] = useState<Record<string, string>>({});
  const [confirmations, setConfirmations] = useState<Record<string, Confirmation>>({});
  const [outcomeResolutionInputs, setOutcomeResolutionInputs] = useState<Record<string, OutcomeResolutionInput>>({});
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pollKey, setPollKey] = useState(0);

  const usableAccounts = useMemo(
    () => accounts.filter((account) => account.platform === "youtube" && account.status === "connected" && account.can_publish_replies),
    [accounts],
  );

  const loadQueue = useCallback(async () => {
    try {
      const response = await fetch(`${apiBaseUrl}/api/v1/comments/youtube-queue?limit=50`, {
        credentials: "include", cache: "no-store",
      });
      const body = await response.json().catch(() => ({})) as Partial<QueueState> & { detail?: unknown };
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Could not load the YouTube approval queue (${response.status})`));
      const nextItems = Array.isArray(body.items) ? body.items : [];
      setItems(nextItems);
      setQueuePaused(Boolean(body.queue_paused));
      setPauseReason(body.pause_reason ?? null);
      setDraftTexts((previous) => {
        const next = { ...previous };
        for (const item of nextItems) {
          if (next[item.id] === undefined && (item.draft_text || item.approved_text)) next[item.id] = item.approved_text ?? item.draft_text ?? "";
        }
        return next;
      });
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the YouTube approval queue.");
    }
  }, [apiBaseUrl]);

  useEffect(() => {
    if (!accountId && usableAccounts.length) setAccountId(usableAccounts[0].id);
    if (accountId && !usableAccounts.some((account) => account.id === accountId)) setAccountId(usableAccounts[0]?.id ?? "");
  }, [accountId, usableAccounts]);

  useEffect(() => { void loadQueue(); }, [loadQueue, pollKey]);

  useEffect(() => {
    const active = items.some((item) => item.status === "queued_generation" || item.status === "generating" || item.status === "publishing");
    if (!active) return;
    const timer = window.setInterval(() => { void loadQueue(); }, 4_000);
    return () => window.clearInterval(timer);
  }, [items, loadQueue]);

  async function addHumanComment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!videoUrl.trim() || !accountId || !commentText.trim() || busy) return;
    setBusy("create"); setError(""); setNotice("");
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      const idempotencyKey = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `human-${Date.now()}-${accountId}`;
      const response = await fetch(`${apiBaseUrl}/api/v1/comments/youtube-queue`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({
          account_id: accountId,
          video_url: videoUrl.trim(),
          comment_text: commentText.trim(),
          idempotency_key: idempotencyKey,
          confirm_one_channel_one_video: true,
        }),
      });
      const body = await response.json().catch(() => ({})) as QueueItem & { detail?: unknown; human_authored?: boolean };
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Could not save human comment (${response.status})`));
      if (body.draft_source !== "human" || body.human_authored !== true) throw new Error("The server did not confirm a human-authored queue item. Nothing was scheduled.");
      setNotice(`Your manually written comment was saved as Pending Approval for ${body.account_name || "the selected channel"}. No AI text was generated or inserted.`);
      setVideoUrl(""); setCommentText("");
      setPollKey((value) => value + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the human-written YouTube comment.");
    } finally { setBusy(""); }
  }

  async function approveAndSchedule(item: QueueItem) {
    if (item.draft_source !== "human") {
      setError("This item was not manually authored and cannot be approved. Create a new human-written comment.");
      return;
    }
    const text = (draftTexts[item.id] ?? item.approved_text ?? item.draft_text ?? "").trim();
    const localSchedule = scheduleTimes[item.id] ?? "";
    const confirmed = confirmations[item.id] ?? { target: false, relevance: false };
    if (!text || !localSchedule || !confirmed.target || !confirmed.relevance) {
      setError("Review the final text, select a publication time, and confirm the target and relevance checkboxes.");
      return;
    }
    const time = new Date(localSchedule);
    if (Number.isNaN(time.getTime())) { setError("Choose a valid publication date and time."); return; }
    if (!window.confirm(`Schedule this exact comment from ${item.account_name} on “${item.video_title}” at ${time.toLocaleString()}?`)) return;
    setBusy(item.id); setError(""); setNotice("");
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/comments/youtube-queue/${item.id}/approve`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({
          approved_text: text,
          scheduled_at: time.toISOString(),
          confirm_target_and_channel: confirmed.target,
          confirm_relevance_and_nonspam: confirmed.relevance,
        }),
      });
      const body = await response.json().catch(() => ({})) as QueueItem & { detail?: unknown };
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Could not schedule comment (${response.status})`));
      setNotice(`Approved and scheduled for ${displayDate(body.scheduled_at)}. The worker will publish only this reviewed text at the selected time.`);
      setScheduleTimes((previous) => ({ ...previous, [item.id]: "" }));
      setConfirmations((previous) => ({ ...previous, [item.id]: { target: false, relevance: false } }));
      setPollKey((value) => value + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not approve and schedule this comment.");
    } finally { setBusy(""); }
  }

  async function cancelItem(item: QueueItem) {
    if (!window.confirm(`Cancel the unpublished comment for “${item.video_title}”?`)) return;
    setBusy(item.id); setError(""); setNotice("");
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/comments/youtube-queue/${item.id}/cancel`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "X-CSRF-Token": csrf },
      });
      const body = await response.json().catch(() => ({})) as QueueItem & { detail?: unknown };
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Could not cancel queue item (${response.status})`));
      setNotice("The unpublished queue item was cancelled.");
      setPollKey((value) => value + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not cancel this queue item.");
    } finally { setBusy(""); }
  }

  async function resolveUnknownOutcome(item: QueueItem, outcome: "posted" | "not_posted") {
    const input = outcomeResolutionInputs[item.id] ?? { providerCommentId: "", note: "", confirmed: false };
    if (!canAdmin) { setError("Only a workspace admin can resolve an uncertain YouTube write."); return; }
    if (!input.confirmed || input.note.trim().length < 12 || (outcome === "posted" && !input.providerCommentId.trim())) {
      setError(outcome === "posted"
        ? "Confirm that you checked YouTube, enter the comment ID, and describe what you verified."
        : "Confirm that you checked YouTube and describe why you determined the comment was not posted.");
      return;
    }
    const action = outcome === "posted" ? "mark this item Posted" : "mark this item Failed and release its video/channel reservation";
    if (!window.confirm(`You have checked YouTube directly. Are you sure you want to ${action}? This manual resolution is recorded in the audit log.`)) return;
    setBusy(item.id); setError(""); setNotice("");
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/comments/youtube-queue/${item.id}/resolve-outcome`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({
          outcome,
          confirm_checked_youtube: input.confirmed,
          provider_comment_id: outcome === "posted" ? input.providerCommentId.trim() : null,
          resolution_note: input.note.trim(),
        }),
      });
      const body = await response.json().catch(() => ({})) as QueueItem & { detail?: unknown };
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Could not resolve outcome (${response.status})`));
      setNotice(outcome === "posted" ? "Marked Posted based on your manual YouTube verification." : "Marked Failed based on your manual YouTube verification. The channel can now be assigned a new queue item.");
      setPollKey((value) => value + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not resolve this queue item's outcome.");
    } finally { setBusy(""); }
  }

  async function resumeAfterQuota() {
    if (!window.confirm("Only continue after you have checked the YouTube Data API quota dashboard and confirmed quota is available. Resume future scheduled writes?")) return;
    setBusy("resume-quota"); setError(""); setNotice("");
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/comments/youtube-queue/quota/resume`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ confirm_quota_checked: true }),
      });
      const body = await response.json().catch(() => ({})) as { detail?: unknown };
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Could not resume queue (${response.status})`));
      setNotice("Queue resumed. Items marked Blocked by quota still require an explicit new approval and schedule.");
      setPollKey((value) => value + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not resume the queue.");
    } finally { setBusy(""); }
  }

  return (
    <section className="panel-card youtube-comment-queue-panel" aria-labelledby="youtube-queue-heading">
      <div className="panel-heading">
        <div><h2 id="youtube-queue-heading">Human-approved YouTube comment queue</h2><p>One connected channel and one unique video per queue item. Nothing publishes until you review the final text and choose a publication time.</p></div>
        <span className="muted-label">DRAFT → REVIEW → SCHEDULE</span>
      </div>
      {queuePaused && <div className="youtube-queue-alert" role="alert"><strong>Publishing paused after a YouTube quota error.</strong><p>Check the official API quota dashboard first. Existing items will not be retried automatically; items marked Blocked by quota need another explicit approval and schedule.</p>{canAdmin && <button className="subtle-button" type="button" disabled={busy === "resume-quota"} onClick={() => void resumeAfterQuota()}>{busy === "resume-quota" ? "Checking…" : "Resume after quota check"}</button>}</div>}
      {error && <div className="publish-validation-error" role="alert">{error}</div>}
      {notice && <div className="inline-success inbox-message" role="status">{notice}</div>}
      <form className="youtube-queue-generate-form" onSubmit={(event) => void addHumanComment(event)}>
        <label><span>Target YouTube video URL</span><input type="url" value={videoUrl} onChange={(event) => setVideoUrl(event.target.value)} placeholder="https://www.youtube.com/watch?v=…" maxLength={2000} required /></label>
        <label><span>Channel identity to use</span><select value={accountId} onChange={(event) => setAccountId(event.target.value)} required disabled={!usableAccounts.length}><option value="">Select one connected channel</option>{usableAccounts.map((account) => <option value={account.id} key={account.id}>{account.display_name}{account.username ? ` · @${account.username}` : ""}</option>)}</select></label>
        <label className="youtube-queue-manual-comment"><span>Write your comment (required — human text only)</span><textarea value={commentText} onChange={(event) => setCommentText(event.target.value)} rows={5} maxLength={10000} required placeholder="Write or paste the exact comment you want to publish. The queue will not generate or replace this text with AI." /></label>
        <div className="youtube-queue-form-footer"><p><strong>Manual-only queue:</strong> the text above is saved as Pending Approval. No AI drafting or generation runs here. You can review/edit it, verify the target video, and explicitly schedule publication below.</p><button className="primary-button" type="submit" disabled={busy === "create" || !videoUrl.trim() || !accountId || !commentText.trim() || !usableAccounts.length}>{busy === "create" ? "Saving human comment…" : "Add human comment for approval"}</button></div>
        {!usableAccounts.length && <p className="external-video-note">Connect a YouTube channel with comment permission before adding a human-written comment.</p>}
      </form>

      <div className="youtube-queue-list-heading"><h3>Review queue</h3><button className="subtle-button" type="button" onClick={() => void loadQueue()}>Refresh queue</button></div>
      {items.length === 0 ? <div className="inbox-empty-inline"><strong>No queued comments yet</strong><p>Write a comment above and add it to the queue. It will appear as Pending Approval immediately; this queue never generates AI text.</p></div> : <div className="youtube-queue-list">
        {items.map((item) => {
          const eligibleForApproval = item.status === "pending_approval" || item.status === "blocked_quota";
          const needsApproval = eligibleForApproval && item.draft_source === "human";
          const confirmation = confirmations[item.id] ?? { target: false, relevance: false };
          return <article className={`youtube-queue-item status-${item.status}`} key={item.id}>
            <div className="youtube-queue-item-header"><div><strong>{item.video_title || "YouTube video"}</strong><small>{item.account_name} · <a href={item.video_url} target="_blank" rel="noreferrer">Open target video ↗</a></small></div><span className={`publish-item-status status-${item.status}`}>{STATUS_LABELS[item.status]}</span></div>
            {item.video_description && <details className="youtube-queue-metadata"><summary>Video metadata (reference only)</summary><p>{item.video_description.slice(0, 2500)}{item.video_description.length > 2500 ? "…" : ""}</p></details>}
            {(item.status === "queued_generation" || item.status === "generating") && <p className="youtube-queue-state-note">This legacy AI-generated item is disabled. Create a new manually written comment instead.</p>}
            {item.draft_text && <label className="youtube-queue-draft-field"><span>{needsApproval ? "Your human-written comment — review/edit before approval" : item.status === "scheduled" ? "Approved human-written text" : "Human-written comment"}</span><textarea rows={3} maxLength={10000} value={draftTexts[item.id] ?? item.approved_text ?? item.draft_text} onChange={(event) => setDraftTexts((previous) => ({ ...previous, [item.id]: event.target.value }))} disabled={!needsApproval || busy === item.id || item.draft_source !== "human"} /></label>}
            {item.quality_warnings.length > 0 && <div className="youtube-queue-warning"><strong>Review warnings</strong><span>{item.quality_warnings.join(" · ").replaceAll("_", " ")}</span></div>}
            {item.error_code && <p className="publish-result-error">{explainQueueError(item.error_code)}</p>}
            {eligibleForApproval && item.draft_source !== "human" && <p className="youtube-queue-warning">This legacy item was not written manually and cannot be approved. Create a new human-written comment.</p>}
            {needsApproval && <div className="youtube-queue-approval-area">
              <label><span>Explicit publication time (your local time)</span><input type="datetime-local" min={localDateTimeOffset(2)} value={scheduleTimes[item.id] ?? ""} onChange={(event) => setScheduleTimes((previous) => ({ ...previous, [item.id]: event.target.value }))} /></label>
              <label className="youtube-queue-check"><input type="checkbox" checked={confirmation.target} onChange={(event) => setConfirmations((previous) => ({ ...previous, [item.id]: { ...(previous[item.id] ?? { target: false, relevance: false }), target: event.target.checked } }))} /><span>I verified the selected channel and the exact target video.</span></label>
              <label className="youtube-queue-check"><input type="checkbox" checked={confirmation.relevance} onChange={(event) => setConfirmations((previous) => ({ ...previous, [item.id]: { ...(previous[item.id] ?? { target: false, relevance: false }), relevance: event.target.checked } }))} /><span>This comment is specific to the video, truthful, non-repetitive, and not intended to inflate engagement.</span></label>
              <button className="primary-button" type="button" disabled={busy === item.id || !scheduleTimes[item.id] || !confirmation.target || !confirmation.relevance || !(draftTexts[item.id] ?? item.draft_text ?? "").trim()} onClick={() => void approveAndSchedule(item)}>{busy === item.id ? "Saving schedule…" : item.status === "blocked_quota" ? "Re-approve and schedule" : "Approve & schedule"}</button>
            </div>}
            {item.status === "scheduled" && <p className="youtube-queue-state-note">Scheduled for <strong>{displayDate(item.scheduled_at)}</strong>. The background worker will publish at or after that time, unless the global queue is paused for quota or permission issues.</p>}
            {item.status === "posted" && <p className="youtube-queue-state-success">Published {displayDate(item.published_at)}{item.provider_comment_id ? <> · <a href={`${item.video_url}&lc=${encodeURIComponent(item.provider_comment_id)}`} target="_blank" rel="noreferrer">View comment ↗</a></> : ""}</p>}
            {item.status === "outcome_unknown" && <div className="youtube-queue-warning"><strong>Outcome unknown — manual check required</strong><p>Do not retry this item automatically. Check the selected channel's comments directly on YouTube. After verifying, an admin can record whether the comment exists or was definitely not posted.</p>{canAdmin && <div className="youtube-queue-resolution-form"><label><span>Manual verification note</span><textarea rows={2} maxLength={500} value={(outcomeResolutionInputs[item.id] ?? { providerCommentId: "", note: "", confirmed: false }).note} onChange={(event) => setOutcomeResolutionInputs((previous) => ({ ...previous, [item.id]: { ...(previous[item.id] ?? { providerCommentId: "", note: "", confirmed: false }), note: event.target.value } }))} placeholder="Explain what you checked on the YouTube video/comment page…" /></label><label><span>YouTube comment ID (required only if it was posted)</span><input value={(outcomeResolutionInputs[item.id] ?? { providerCommentId: "", note: "", confirmed: false }).providerCommentId} onChange={(event) => setOutcomeResolutionInputs((previous) => ({ ...previous, [item.id]: { ...(previous[item.id] ?? { providerCommentId: "", note: "", confirmed: false }), providerCommentId: event.target.value } }))} maxLength={240} placeholder="Comment ID from YouTube, if found" /></label><label className="youtube-queue-check"><input type="checkbox" checked={(outcomeResolutionInputs[item.id] ?? { providerCommentId: "", note: "", confirmed: false }).confirmed} onChange={(event) => setOutcomeResolutionInputs((previous) => ({ ...previous, [item.id]: { ...(previous[item.id] ?? { providerCommentId: "", note: "", confirmed: false }), confirmed: event.target.checked } }))} /><span>I checked the selected channel and target video directly on YouTube.</span></label><div className="youtube-queue-resolution-actions"><button className="subtle-button" type="button" disabled={busy === item.id} onClick={() => void resolveUnknownOutcome(item, "posted")}>Confirm posted</button><button className="subtle-button" type="button" disabled={busy === item.id} onClick={() => void resolveUnknownOutcome(item, "not_posted")}>Confirm not posted</button></div></div>}</div>}
            {item.status === "blocked_quota" && <p className="youtube-queue-state-note">This write was not confirmed due to quota exhaustion. After the quota is available, review the text and choose a new explicit schedule.</p>}
            {item.status === "failed" && <p className="youtube-queue-state-note">This item failed and will not be retried automatically. Correct the underlying issue and create a new human-written comment if appropriate.</p>}
            {["queued_generation", "generating", "pending_approval", "scheduled", "blocked_quota", "failed"].includes(item.status) && <div className="youtube-queue-actions"><small>Created {displayDate(item.created_at)}{item.approved_by_email ? ` · Approved by ${item.approved_by_email}` : ""}</small><button className="subtle-button" type="button" disabled={busy === item.id} onClick={() => void cancelItem(item)}>Cancel unpublished item</button></div>}
          </article>;
        })}
      </div>}
      <p className="youtube-queue-policy-note">Manual-only workflow: the Human YouTube Comment Queue never calls the AI drafting service. Only the text you enter is saved and can be scheduled; final approval, user-selected scheduling, unique video assignment, repeat-text safeguards, provider quota pauses, and no blind retries after uncertain writes are enforced.</p>
    </section>
  );
}
