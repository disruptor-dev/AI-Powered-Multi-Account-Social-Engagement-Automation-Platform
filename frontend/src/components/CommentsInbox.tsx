"use client";

import { useCallback, useEffect, useState } from "react";
import { explainApiError, fetchCsrfToken } from "@/lib/csrf";
import YouTubeCommentQueue from "@/components/YouTubeCommentQueue";

type Platform = "youtube" | "instagram";
type AccountStatus = "connected" | "reauthorization_required";
type InboxSummary = {
  open_comments: number;
  unanswered_comments: number;
  resolved_comments: number;
  all_comments: number;
  accounts_needing_attention: number;
  last_sync_at: string | null;
};
type SyncAccount = {
  id: string;
  platform: Platform;
  display_name: string;
  username: string | null;
  status: AccountStatus;
  last_sync_status: "never" | "queued" | "running" | "success" | "partial" | "error";
  last_sync_started_at: string | null;
  last_sync_completed_at: string | null;
  last_sync_error_code: string | null;
  last_sync_media_count: number;
  last_sync_comment_count: number;
  can_publish_replies: boolean;
};
type CommentMedia = {
  id: string;
  platform_media_id: string;
  type: string;
  title: string;
  description: string | null;
  permalink: string | null;
  thumbnail_url: string | null;
  published_at: string | null;
  source_type: "account_video" | "external_video" | "account_media" | string;
};
type InboxComment = {
  id: string;
  platform: Platform;
  account_id: string;
  account_name: string;
  account_username: string | null;
  account_status: string;
  platform_comment_id: string;
  parent_platform_comment_id: string | null;
  parent_comment_id: string | null;
  parent_author_name: string | null;
  author_id: string | null;
  author_name: string;
  author_username: string | null;
  author_avatar_url: string | null;
  text: string;
  like_count: number;
  reply_count: number;
  is_reply: boolean;
  is_owner_comment: boolean;
  has_owner_reply: boolean;
  workflow_status: "open" | "resolved";
  published_at: string | null;
  platform_updated_at: string | null;
  media: CommentMedia;
  can_reply: boolean;
};
type Pagination = { page: number; page_size: number; total: number; pages: number; has_more: boolean };
type ExternalJob = { runId: string; videoUrl: string; accountId: string; accountName: string; status: string };
type ExternalJobApi = { id: string; status: string; error_code: string | null; account_id: string; account_name: string; comments_inserted: number; comments_updated: number; comments_skipped: number; media_processed: number };
type PublishItemStatus = "queued" | "publishing" | "published" | "failed" | "unknown";
type PublishBatchItem = { id: string; account_id: string; account_name: string; text: string; status: PublishItemStatus; error_code: string | null; provider_comment_id: string | null; published_at: string | null };
type PublishBatch = { id: string; status: "queued" | "running" | "success" | "partial" | "error"; video_id: string; video_url: string; video_title: string | null; video_thumbnail_url: string | null; total_count: number; published_count: number; failed_count: number; unknown_count: number; error_code: string | null; estimated_quota_units: number; created_at: string | null; started_at: string | null; finished_at: string | null; items: PublishBatchItem[]; duplicate_request?: boolean };
type AiDraftStatus = "queued" | "generating" | "draft" | "approved" | "rejected" | "published" | "failed";
type AiTone = "helpful" | "friendly" | "professional" | "empathetic" | "concise";
type AiLanguage = "match_comment" | "en" | "hi" | "hinglish" | "es" | "fr" | "de";
type AiDraft = { id: string; comment_id: string; target_account_id: string | null; automation_rule_id: string | null; status: AiDraftStatus; tone: AiTone; language: AiLanguage; custom_instructions: string | null; draft_text: string | null; provider: string; model: string; error_code: string | null; created_at: string | null; updated_at: string | null; generated_at: string | null; reviewed_at: string | null; published_at: string | null };
type CsvParseResult = { items: string[]; error: string | null };
type ApiErrorBody = { detail?: unknown };
type Props = {
  apiBaseUrl: string;
  onGoAccounts: () => void;
  onSummaryChange: (summary: InboxSummary) => void;
  canEdit: boolean;
  canAdmin: boolean;
};

const EMPTY_SUMMARY: InboxSummary = {
  open_comments: 0,
  unanswered_comments: 0,
  resolved_comments: 0,
  all_comments: 0,
  accounts_needing_attention: 0,
  last_sync_at: null,
};


function parseCommaSeparatedComments(input: string): CsvParseResult {
  const items: string[] = [];
  let token = "";
  let inQuotes = false;
  let quoteClosed = false;
  let lastWasSeparator = false;
  const flush = () => {
    items.push(token.trim());
    token = "";
    quoteClosed = false;
    lastWasSeparator = true;
  };

  for (let i = 0; i < input.length; i += 1) {
    const char = input[i];
    if (inQuotes) {
      if (char === "\"" && input[i + 1] === "\"") {
        token += "\"";
        i += 1;
      } else if (char === "\"") {
        inQuotes = false;
        quoteClosed = true;
      } else {
        token += char;
      }
      lastWasSeparator = false;
      continue;
    }

    const separator = char === "," || char === "\n" || char === "\r";
    if (char === "\"") {
      if (token.trim().length > 0 || quoteClosed) return { items: [], error: "Use quotation marks around the whole comment when it contains commas or line breaks." };
      token = "";
      inQuotes = true;
      lastWasSeparator = false;
      continue;
    }
    if (separator) {
      flush();
      if (char === "\r" && input[i + 1] === "\n") i += 1;
      continue;
    }
    if (quoteClosed && !/\s/.test(char)) return { items: [], error: "After a quoted comment, add a comma or a new line before the next comment." };
    if (!quoteClosed) token += char;
    if (!/\s/.test(char)) lastWasSeparator = false;
  }
  if (inQuotes) return { items: [], error: "A quoted comment is missing its closing quotation mark." };
  if (token.trim() || input.trim()) {
    items.push(token.trim());
  } else if (lastWasSeparator) {
    items.push("");
  }
  if (items.some((item) => !item)) return { items, error: "Each selected channel needs exactly one non-empty comment. Remove extra commas or empty lines." };
  return { items, error: null };
}

function humanPublishStatus(status: PublishBatch["status"] | PublishItemStatus): string {
  const names: Record<string, string> = {
    queued: "Queued", running: "Publishing", success: "All published", partial: "Partially published", error: "Batch failed",
    publishing: "Publishing", published: "Published", failed: "Failed", unknown: "Outcome unknown",
  };
  return names[status] ?? status;
}

function explainPublishError(code: string | null): string {
  if (!code) return "";
  const names: Record<string, string> = {
    account_not_found: "The selected channel is no longer available.",
    account_disconnected: "Reconnect this channel before publishing.",
    comment_publish_scope_missing: "Reconnect this channel and grant YouTube comment access.",
    provider_reauthorization_required: "Reconnect this channel to renew YouTube access.",
    token_refresh_failed: "Channel access could not be refreshed.",
    token_decryption_failed: "Saved access could not be unlocked. Check TOKEN_ENCRYPTION_KEY.",
    access_token_missing: "Reconnect this channel before publishing.",
    provider_permission_denied: "YouTube denied permission to publish this comment.",
    provider_quota_exceeded: "The YouTube API quota has been reached.",
    provider_rate_limited: "YouTube rate-limited this request.",
    provider_unavailable: "YouTube is temporarily unavailable.",
    provider_request_failed: "YouTube rejected this comment. Check video settings and channel permissions.",
    youtube_video_unavailable: "The target video is unavailable or restricted.",
    youtube_comments_unavailable: "Comments may be disabled or unavailable for this video.",
    provider_outcome_unknown: "YouTube may have accepted this comment, but confirmation was lost. Check the video before retrying.",
    publish_queue_unavailable: "The background queue could not accept this batch.",
    publish_partial_failure: "Some channels failed or have an uncertain result.",
    publish_all_failed: "No comments in this batch were published.",
  };
  return names[code] ?? code.replaceAll("_", " ");
}

function formatDate(value: string | null): string {
  if (!value) return "Not synced yet";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Time unavailable" : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function relativeTime(value: string | null): string {
  if (!value) return "Never synced";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Time unavailable";
  const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60_000));
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function toDateBoundary(value: string, endOfDay = false): string | undefined {
  if (!value) return undefined;
  const date = new Date(`${value}T${endOfDay ? "23:59:59.999" : "00:00:00"}`);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

function humanSyncStatus(status: SyncAccount["last_sync_status"]): string {
  const labels: Record<SyncAccount["last_sync_status"], string> = {
    never: "Not synced", queued: "Queued", running: "Syncing", success: "Up to date", partial: "Partially synced", error: "Sync error",
  };
  return labels[status] ?? "Unknown";
}

function explainSyncError(code: string | null): string {
  if (!code) return "";
  const labels: Record<string, string> = {
    provider_reauthorization_required: "Reconnect this account to renew access.",
    token_decryption_failed: "Saved credentials could not be unlocked. Check TOKEN_ENCRYPTION_KEY.",
    access_token_missing: "Reconnect this account before syncing.",
    token_refresh_failed: "Token refresh failed. Reconnect and try again.",
    provider_permission_denied: "The app lacks permission to read comments for this account or media.",
    provider_quota_exceeded: "The provider API quota was reached. Try again after it resets.",
    provider_rate_limited: "The provider rate-limited this sync. It will retry on a later schedule.",
    provider_unavailable: "The provider API is temporarily unavailable.",
    provider_request_failed: "The provider rejected a request. Check permissions and account settings.",
    comments_unavailable: "Some media does not allow comment reading.",
    provider_queue_unavailable: "The background queue is unavailable.",
    sync_queue_unavailable: "The background queue is unavailable.",
    sync_internal_error: "The sync failed unexpectedly. Review backend logs for the request ID.",
    sync_worker_timeout: "A worker did not finish this sync. A later scheduled run can retry it.",
    account_disconnected: "Reconnect this account to resume syncing.",
    account_not_found: "The account is no longer available.",
    youtube_channel_unavailable: "The authorized Google account no longer exposes this YouTube channel.",
    youtube_uploads_playlist_unavailable: "Could not load the channel's uploads playlist.",
    youtube_video_unavailable: "The video is unavailable to the YouTube API or is private, deleted, or restricted.",
    youtube_comments_disabled: "Comments are disabled or unavailable for this video.",
    external_video_comments_truncated: "The configured top-level comment import cap was reached; increase COMMENT_SYNC_MAX_EXTERNAL_VIDEO_COMMENTS to import more.",
    external_video_replies_truncated: "The configured reply import cap was reached; increase COMMENT_SYNC_MAX_EXTERNAL_VIDEO_REPLIES to import more replies.",
    external_video_requires_youtube: "External video imports require a YouTube channel.",
    reply_not_allowed: "YouTube does not allow this channel to reply to this comment.",
    reply_target_unavailable: "This comment may have been deleted, hidden, or made unavailable.",
    reply_permission_denied: "YouTube denied reply permission for the selected channel.",
    reply_scope_missing: "Reconnect this channel and grant YouTube comment access.",
  };
  return code.split(",").map((entry) => labels[entry] ?? entry.replaceAll("_", " ")).join(" · ");
}

export default function CommentsInbox({ apiBaseUrl, onGoAccounts, onSummaryChange, canEdit, canAdmin }: Props) {
  const [comments, setComments] = useState<InboxComment[]>([]);
  const [accounts, setAccounts] = useState<SyncAccount[]>([]);
  const [summary, setSummary] = useState<InboxSummary>(EMPTY_SUMMARY);
  const [pagination, setPagination] = useState<Pagination>({ page: 1, page_size: 25, total: 0, pages: 0, has_more: false });
  const [platform, setPlatform] = useState<"all" | Platform>("all");
  const [accountId, setAccountId] = useState("all");
  const [workflowStatus, setWorkflowStatus] = useState<"all" | "open" | "resolved">("open");
  const [responseStatus, setResponseStatus] = useState<"all" | "unanswered" | "replied">("all");
  const [includeReplies, setIncludeReplies] = useState(false);
  const [sort, setSort] = useState<"newest" | "oldest">("newest");
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [actionKey, setActionKey] = useState("");
  const [page, setPage] = useState(1);
  const [externalVideoUrl, setExternalVideoUrl] = useState("");
  const [externalAccountId, setExternalAccountId] = useState("");
  const [externalJob, setExternalJob] = useState<ExternalJob | null>(null);
  const [externalMessage, setExternalMessage] = useState("");
  const [replyingTo, setReplyingTo] = useState<string | null>(null);
  const [replyDraft, setReplyDraft] = useState("");
  const [replyAccountId, setReplyAccountId] = useState("");
  const [publishVideoUrl, setPublishVideoUrl] = useState("");
  const [publishCsvText, setPublishCsvText] = useState("");
  const [publishChannelIds, setPublishChannelIds] = useState<string[]>([]);
  const [publishConfirmed, setPublishConfirmed] = useState(false);
  const [publishNonSpamConfirmed, setPublishNonSpamConfirmed] = useState(false);
  const [publishSubmitting, setPublishSubmitting] = useState(false);
  const [publishBatch, setPublishBatch] = useState<PublishBatch | null>(null);
  const [publishError, setPublishError] = useState("");
  const [aiConfigured, setAiConfigured] = useState(false);
  const [aiTone, setAiTone] = useState<AiTone>("helpful");
  const [aiLanguage, setAiLanguage] = useState<AiLanguage>("match_comment");
  const [aiInstructions, setAiInstructions] = useState("");
  const [aiDraftsByComment, setAiDraftsByComment] = useState<Record<string, AiDraft[]>>({});
  const [aiDraftEditors, setAiDraftEditors] = useState<Record<string, string>>({});
  const [replyAiDraftId, setReplyAiDraftId] = useState<string | null>(null);
  const [aiNotice, setAiNotice] = useState("");
  const [aiError, setAiError] = useState("");

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setPage(1);
      setQuery(queryDraft.trim());
    }, 350);
    return () => window.clearTimeout(timer);
  }, [queryDraft]);

  const readJson = useCallback(async (url: string) => {
    const response = await fetch(url, { credentials: "include", cache: "no-store" });
    const body = await response.json().catch(() => ({})) as ApiErrorBody;
    if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Request failed (${response.status})`));
    return body as Record<string, unknown>;
  }, []);

  const loadSyncStatus = useCallback(async () => {
    const [statusBody, summaryBody] = await Promise.all([
      readJson(`${apiBaseUrl}/api/v1/comments/sync-status`),
      readJson(`${apiBaseUrl}/api/v1/comments/summary`),
    ]);
    const nextAccounts = Array.isArray(statusBody.accounts) ? statusBody.accounts as SyncAccount[] : [];
    const nextSummary = { ...EMPTY_SUMMARY, ...(summaryBody as Partial<InboxSummary>) };
    setAccounts(nextAccounts);
    setSummary(nextSummary);
    onSummaryChange(nextSummary);
  }, [apiBaseUrl, onSummaryChange, readJson]);

  const loadComments = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    setError("");
    const params = new URLSearchParams({ page: String(page), page_size: "25", workflow_status: workflowStatus, response_status: responseStatus, include_replies: String(includeReplies), sort });
    if (platform !== "all") params.set("platform", platform);
    if (accountId !== "all") params.set("account_id", accountId);
    if (query) params.set("query", query);
    const sinceBoundary = toDateBoundary(since);
    const untilBoundary = toDateBoundary(until, true);
    if (sinceBoundary) params.set("since", sinceBoundary);
    if (untilBoundary) params.set("until", untilBoundary);
    try {
      const body = await readJson(`${apiBaseUrl}/api/v1/comments?${params.toString()}`);
      const nextComments = Array.isArray(body.items) ? body.items as InboxComment[] : [];
      setComments(nextComments);
      setPagination((body.pagination as Pagination | undefined) ?? { page, page_size: 25, total: 0, pages: 0, has_more: false });
      if (nextComments.length === 0) {
        setAiDraftsByComment({});
      } else {
        const draftParams = new URLSearchParams();
        nextComments.forEach((item) => draftParams.append("comment_ids", item.id));
        try {
          const draftBody = await readJson(`${apiBaseUrl}/api/v1/comments/ai-drafts?${draftParams.toString()}`);
          const grouped: Record<string, AiDraft[]> = {};
          const nextDrafts = Array.isArray(draftBody.items) ? draftBody.items as AiDraft[] : [];
          for (const draft of nextDrafts) (grouped[draft.comment_id] ??= []).push(draft);
          setAiDraftsByComment(grouped);
          setAiDraftEditors((current) => {
            const next = { ...current };
            for (const draft of nextDrafts) if (draft.draft_text && next[draft.id] === undefined) next[draft.id] = draft.draft_text;
            return next;
          });
        } catch {
          // Inbox data still loads if AI drafting is not configured or the draft endpoint is unavailable.
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load comments.");
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [accountId, apiBaseUrl, includeReplies, page, platform, query, readJson, responseStatus, since, sort, until, workflowStatus]);

  const refreshAll = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      await Promise.all([loadSyncStatus(), loadComments(true)]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to refresh the inbox.");
    } finally {
      setRefreshing(false);
    }
  }, [loadComments, loadSyncStatus]);

  useEffect(() => { void loadSyncStatus().catch((err: unknown) => setError(err instanceof Error ? err.message : "Unable to load sync status.")); }, [loadSyncStatus]);
  useEffect(() => { void loadComments(); }, [loadComments]);
  useEffect(() => {
    let active = true;
    void readJson(`${apiBaseUrl}/api/v1/comments/ai/status`).then((body) => { if (active) setAiConfigured(Boolean(body.configured)); }).catch(() => { if (active) setAiConfigured(false); });
    return () => { active = false; };
  }, [apiBaseUrl, readJson]);

  // Poll account/job state so queued and running jobs transition without requiring a reload.
  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadSyncStatus().catch(() => undefined);
      void loadComments(true);
    }, 12_000);
    return () => window.clearInterval(timer);
  }, [loadComments, loadSyncStatus]);
  useEffect(() => {
    const eligibleYouTube = accounts.filter((account) => account.platform === "youtube" && account.status === "connected");
    const eligibleReplyChannels = eligibleYouTube.filter((account) => account.can_publish_replies);
    if ((!externalAccountId || !eligibleYouTube.some((account) => account.id === externalAccountId)) && eligibleYouTube.length > 0) {
      setExternalAccountId(eligibleYouTube[0].id);
    }
    if ((!replyAccountId || !eligibleReplyChannels.some((account) => account.id === replyAccountId)) && eligibleReplyChannels.length > 0) {
      setReplyAccountId(eligibleReplyChannels[0].id);
    }
  }, [accounts, externalAccountId, replyAccountId]);

  const externalRunId = externalJob?.runId ?? "";
  useEffect(() => {
    if (!externalRunId) return;
    let active = true;
    const poll = async () => {
      try {
        const body = await readJson(`${apiBaseUrl}/api/v1/comments/runs/${externalRunId}`) as unknown as ExternalJobApi;
        if (!active) return;
        setExternalJob((current) => current ? { ...current, status: body.status } : current);
        if (["success", "partial", "error"].includes(body.status)) {
          setExternalJob(null);
          if (body.status === "error") {
            setExternalMessage(`Import failed: ${explainSyncError(body.error_code)}. You can retry after checking the video and channel permissions.`);
          } else {
            const partialNote = body.status === "partial" && body.error_code ? ` Some comments/replies may be unavailable: ${explainSyncError(body.error_code)}.` : "";
            setExternalMessage(`Imported ${body.comments_inserted + body.comments_updated} comments from ${body.account_name ? `the video using ${body.account_name}` : "the video"}.${partialNote}`);
            setPlatform("youtube");
            setAccountId(body.account_id);
            setWorkflowStatus("all");
            setResponseStatus("all");
            setIncludeReplies(false);
            setQuery("");
            setQueryDraft("");
            setPage(1);
            await refreshAll(true);
          }
        }
      } catch (err) {
        if (active) setExternalMessage(err instanceof Error ? err.message : "Could not read the import status.");
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 3_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [apiBaseUrl, externalRunId, readJson, refreshAll]);

  async function queueSync(id?: string) {
    const key = id ?? "all";
    setActionKey(`sync-${key}`);
    setError("");
    setMessage("");
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/comments/sync`, {
        method: "POST",
        credentials: "include",
        cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify(id ? { account_id: id } : {}),
      });
      const body = await response.json().catch(() => ({})) as { detail?: unknown; status?: string; queued?: number };
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Could not queue sync (${response.status})`));
      setMessage(body.queued ? `Sync queued for ${body.queued} account${body.queued === 1 ? "" : "s"}. New comments will appear as the worker finishes each account.` : "A sync is already queued or running for the selected account(s).");
      await refreshAll(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start comment synchronization.");
    } finally {
      setActionKey("");
    }
  }

  async function queueExternalVideo() {
    if (!externalVideoUrl.trim() || !externalAccountId || externalJob) return;
    setActionKey("external-video");
    setError("");
    setMessage("");
    setExternalMessage("");
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/comments/external-video`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ account_id: externalAccountId, video_url: externalVideoUrl.trim() }),
      });
      const body = await response.json().catch(() => ({})) as { detail?: unknown; run_id?: string; account_id?: string; account_name?: string; video_url?: string; status?: string };
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Could not import video (${response.status})`));
      if (!body.run_id || !body.account_id) throw new Error("The API did not return the import job ID.");
      setExternalJob({ runId: body.run_id, videoUrl: body.video_url ?? externalVideoUrl.trim(), accountId: body.account_id, accountName: body.account_name ?? "Selected YouTube channel", status: body.status ?? "queued" });
      setExternalVideoUrl(body.video_url ?? externalVideoUrl.trim());
      setExternalMessage("Video accepted. Fetching its comments in the background…");
      setPlatform("youtube");
      setAccountId(body.account_id);
      setWorkflowStatus("all");
      setPage(1);
    } catch (err) {
      setExternalMessage(err instanceof Error ? err.message : "Could not queue the external video import.");
    } finally {
      setActionKey("");
    }
  }

  async function submitMultiChannelComments() {
    const selected = publishChannelIds
      .map((id) => publishAccounts.find((account) => account.id === id))
      .filter((account): account is SyncAccount => Boolean(account));
    const parsed = parseCommaSeparatedComments(publishCsvText);
    if (publishSubmitting || selected.length === 0 || selected.length > 10 || !publishVideoUrl.trim()) return;
    if (parsed.error || parsed.items.length !== selected.length) {
      setPublishError(parsed.error ?? `Enter exactly ${selected.length} comments for the selected channels.`);
      return;
    }
    const distinct = parsed.items.map((text) => text.replace(/\s+/g, " ").trim().toLocaleLowerCase());
    if (new Set(distinct).size !== distinct.length) {
      setPublishError("Each channel needs a distinct comment. Identical or repetitive comments are not accepted.");
      return;
    }
    if (!publishConfirmed || !publishNonSpamConfirmed) {
      setPublishError("Review the preview and confirm both publishing safeguards before continuing.");
      return;
    }

    setPublishSubmitting(true);
    setPublishError("");
    setError("");
    setMessage("");
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/comments/publish-batches`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({
          video_url: publishVideoUrl.trim(),
          idempotency_key: crypto.randomUUID(),
          confirm_publish: true,
          confirm_nonspam: true,
          comments: selected.map((account, index) => ({ account_id: account.id, text: parsed.items[index] })),
        }),
      });
      const body = await response.json().catch(() => ({})) as PublishBatch & { detail?: unknown };
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Could not queue comments (${response.status})`));
      if (!body.id || !Array.isArray(body.items)) throw new Error("The API did not return the channel publishing results.");
      setPublishBatch(body);
      setPublishConfirmed(false);
      setPublishNonSpamConfirmed(false);
      setMessage(body.status === "error" ? "The batch was saved but could not be queued. Review its per-channel status below." : `Publishing queue created for ${body.total_count} channel${body.total_count === 1 ? "" : "s"}. Results will update below.`);
      setPlatform("youtube");
      setAccountId("all");
      setWorkflowStatus("all");
      setPage(1);
    } catch (err) {
      setPublishError(err instanceof Error ? err.message : "Could not queue the selected comments.");
    } finally {
      setPublishSubmitting(false);
    }
  }

  async function copyAiDraft(text: string) {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
      await navigator.clipboard.writeText(text);
      setAiNotice("Draft copied to clipboard.");
    } catch {
      setAiNotice("Clipboard access is unavailable. Select the draft text and copy it manually.");
    }
  }

  async function generateAiDraft(comment: InboxComment) {
    if (!aiConfigured || actionKey || comment.is_owner_comment) return;
    setActionKey(`ai-${comment.id}`); setAiError(""); setAiNotice("");
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/comments/${comment.id}/ai-drafts`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ tone: aiTone, language: aiLanguage, custom_instructions: aiInstructions.trim() || null }),
      });
      const body = await response.json().catch(() => ({})) as AiDraft & { detail?: unknown };
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Could not queue AI draft (${response.status})`));
      setAiDraftsByComment((current) => ({ ...current, [comment.id]: [body, ...(current[comment.id] ?? [])] }));
      setAiNotice("AI draft queued. It will appear here after generation; review all facts before use.");
      setAiDraftEditors((current) => ({ ...current, [body.id]: body.draft_text ?? "" }));
      void loadComments(true);
    } catch (err) { setAiError(err instanceof Error ? err.message : "Could not generate an AI draft."); }
    finally { setActionKey(""); }
  }

  async function updateAiDraft(comment: InboxComment, draft: AiDraft, update: { draft_text?: string; status?: "draft" | "approved" | "rejected" }) {
    if (actionKey) return null;
    setActionKey(`ai-draft-${draft.id}`); setAiError(""); setAiNotice("");
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/comments/ai-drafts/${draft.id}`, {
        method: "PATCH", credentials: "include", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify(update),
      });
      const body = await response.json().catch(() => ({})) as AiDraft & { detail?: unknown };
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Could not update AI draft (${response.status})`));
      setAiDraftsByComment((current) => ({ ...current, [comment.id]: (current[comment.id] ?? []).map((item) => item.id === body.id ? body : item) }));
      if (body.draft_text) setAiDraftEditors((current) => ({ ...current, [body.id]: body.draft_text ?? "" }));
      return body;
    } catch (err) {
      setAiError(err instanceof Error ? err.message : "Could not update the AI draft.");
      return null;
    } finally { setActionKey(""); }
  }

  async function useAiDraft(comment: InboxComment, draft: AiDraft) {
    const text = (aiDraftEditors[draft.id] ?? draft.draft_text ?? "").trim();
    if (!text) { setAiError("Write or generate a draft before using it."); return; }
    const saved = await updateAiDraft(comment, draft, { draft_text: text, status: "approved" });
    if (!saved) return;
    if (comment.platform === "youtube" && !comment.is_reply) {
      setReplyingTo(comment.id);
      setReplyDraft(saved.draft_text ?? text);
      setReplyAiDraftId(saved.id);
      setReplyAccountId(replyAccounts.some((account) => account.id === comment.account_id) ? comment.account_id : replyAccounts[0]?.id ?? "");
      setAiNotice("Draft approved for this version and loaded into the reply composer. Review the channel and text, then explicitly publish.");
    } else {
      setAiNotice("Draft approved and ready to copy. Instagram reply publishing is not enabled in this phase.");
    }
  }

  async function publishReply(comment: InboxComment) {
    const text = replyDraft.trim();
    const targetAccountId = replyAccountId;
    if (!text || !targetAccountId || actionKey) return;
    setActionKey(`reply-${comment.id}`);
    setError("");
    setMessage("");
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      if (replyAiDraftId) {
        const draftUpdate = await fetch(`${apiBaseUrl}/api/v1/comments/ai-drafts/${replyAiDraftId}`, {
          method: "PATCH", credentials: "include", cache: "no-store",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
          body: JSON.stringify({ draft_text: text, status: "approved" }),
        });
        const draftBody = await draftUpdate.json().catch(() => ({})) as AiDraft & { detail?: unknown };
        if (!draftUpdate.ok) throw new Error(explainApiError(draftUpdate.status, draftBody.detail, `Could not approve edited draft (${draftUpdate.status})`));
        setAiDraftsByComment((current) => ({ ...current, [comment.id]: (current[comment.id] ?? []).map((item) => item.id === draftBody.id ? draftBody : item) }));
      }
      const response = await fetch(`${apiBaseUrl}/api/v1/comments/${comment.id}/reply`, {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ account_id: targetAccountId, text, ...(replyAiDraftId ? { ai_draft_id: replyAiDraftId } : {}) }),
      });
      const body = await response.json().catch(() => ({})) as { detail?: unknown; account_name?: string; status?: string };
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Could not publish reply (${response.status})`));
      setMessage(`Reply published on YouTube as ${body.account_name ?? "the selected channel"}.`);
      setReplyDraft("");
      setReplyingTo(null);
      if (replyAiDraftId) setAiDraftsByComment((current) => ({ ...current, [comment.id]: (current[comment.id] ?? []).map((item) => item.id === replyAiDraftId ? { ...item, status: "published", published_at: new Date().toISOString() } : item) }));
      setReplyAiDraftId(null);
      setPlatform("youtube");
      setAccountId(targetAccountId);
      setWorkflowStatus("all");
      setQuery("");
      setQueryDraft("");
      setIncludeReplies(true);
      setPage(1);
      await refreshAll(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not publish the reply.");
    } finally {
      setActionKey("");
    }
  }

  async function setCommentWorkflowStatus(comment: InboxComment, next: "open" | "resolved") {
    setActionKey(comment.id);
    setError("");
    setMessage("");
    try {
      const csrf = await fetchCsrfToken(apiBaseUrl);
      const response = await fetch(`${apiBaseUrl}/api/v1/comments/${comment.id}/status`, {
        method: "PATCH",
        credentials: "include",
        cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ workflow_status: next }),
      });
      const body = await response.json().catch(() => ({})) as ApiErrorBody;
      if (!response.ok) throw new Error(explainApiError(response.status, body.detail, `Could not update comment (${response.status})`));
      setMessage(next === "resolved" ? "Comment marked as resolved." : "Comment reopened.");
      await refreshAll(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update comment status.");
    } finally {
      setActionKey("");
    }
  }

  const publishBatchId = publishBatch?.id ?? "";
  const publishBatchStatus = publishBatch?.status ?? "";
  useEffect(() => {
    if (!publishBatchId || !["queued", "running"].includes(publishBatchStatus)) return;
    let active = true;
    const poll = async () => {
      try {
        const body = await readJson(`${apiBaseUrl}/api/v1/comments/publish-batches/${publishBatchId}`) as unknown as PublishBatch;
        if (!active) return;
        setPublishBatch(body);
        if (["success", "partial", "error"].includes(body.status)) {
          const summaryText = `${body.published_count}/${body.total_count} comments published${body.unknown_count ? ` · ${body.unknown_count} outcome${body.unknown_count === 1 ? "" : "s"} unknown` : ""}.`;
          setMessage(summaryText);
          void refreshAll(true);
        }
      } catch (err) {
        if (active) setPublishError(err instanceof Error ? err.message : "Could not refresh publishing results.");
      }
    };
    const timer = window.setInterval(() => { void poll(); }, 2000);
    void poll();
    return () => { active = false; window.clearInterval(timer); };
  }, [apiBaseUrl, publishBatchId, publishBatchStatus, readJson, refreshAll]);

  const activeAccounts = accounts;
  const youtubeAccounts = accounts.filter((account) => account.platform === "youtube" && account.status === "connected");
  const replyAccounts = accounts.filter((account) => account.platform === "youtube" && account.status === "connected" && account.can_publish_replies);
  const publishAccounts = accounts.filter((account) => account.platform === "youtube" && account.status === "connected" && account.can_publish_replies);
  const selectedPublishAccounts = publishChannelIds.map((id) => publishAccounts.find((account) => account.id === id)).filter((account): account is SyncAccount => Boolean(account));
  const parsedPublishComments = parseCommaSeparatedComments(publishCsvText);
  const publishPreview = selectedPublishAccounts.map((account, index) => ({ account, text: parsedPublishComments.items[index] ?? "" }));
  const publishTextCountsMatch = selectedPublishAccounts.length > 0 && parsedPublishComments.items.length === selectedPublishAccounts.length;
  const publishTextIsDistinct = publishTextCountsMatch && new Set(parsedPublishComments.items.map((text) => text.replace(/\s+/g, " ").trim().toLocaleLowerCase())).size === parsedPublishComments.items.length;
  const publishBatchActive = Boolean(publishBatch && ["queued", "running"].includes(publishBatch.status));
  const connectedCount = accounts.filter((account) => account.status === "connected").length;
  const syncingCount = accounts.filter((account) => account.last_sync_status === "running" || account.last_sync_status === "queued").length;

  return (
    <section className="inbox-view" id="inbox">
      <div className="inbox-header-row">
        <div>
          <p className="eyebrow">PHASE 4 · AI REPLY ASSISTANCE</p>
          <h1>Every conversation, in one place.</h1>
          <p className="page-subtitle">Read YouTube video and Shorts comments plus comments on supported Instagram professional-account media.</p>
        </div>
        <div className="inbox-header-actions">
          <button type="button" className="subtle-button inbox-refresh-button" onClick={() => void refreshAll()} disabled={refreshing || loading}>{refreshing ? "Refreshing…" : "↻ Refresh"}</button>
          {canEdit && <button type="button" className="primary-button inbox-sync-all" onClick={() => void queueSync()} disabled={Boolean(actionKey) || connectedCount === 0}>{actionKey === "sync-all" ? "Queueing…" : syncingCount ? `Sync all · ${syncingCount} active` : "Sync all accounts"}</button>}
        </div>
      </div>

      {!canEdit && <div className="note-bottom"><strong>Read-only access.</strong> You can review comments and saved drafts. Sync, edit, approve, and publish actions require editor access or higher.</div>}
      {error && <div className="inline-warning inbox-message" role="alert">{error}</div>}
      {message && <div className="inline-success inbox-message" role="status">{message}</div>}

      <div className="inbox-stat-grid">
        <article className="inbox-stat-card"><span>Open comments</span><strong>{summary.open_comments}</strong><small>Top-level comments to review</small></article>
        <article className="inbox-stat-card"><span>Unanswered</span><strong>{summary.unanswered_comments}</strong><small>Not yet answered by this account</small></article>
        <article className="inbox-stat-card"><span>Resolved</span><strong>{summary.resolved_comments}</strong><small>Marked complete in this workspace</small></article>
        <article className="inbox-stat-card"><span>Accounts syncing</span><strong>{syncingCount}</strong><small>{connectedCount} connected and ready</small></article>
      </div>

      {canEdit && <section className="panel-card external-video-panel">
        <div className="panel-heading"><div><h2>Open comments from an external YouTube video</h2><p>Paste a public video or Shorts URL—even when your connected channel does not own the video.</p></div><span className="muted-label">YOUTUBE · IMPORT + REPLY</span></div>
        <form className="external-video-form" onSubmit={(event) => { event.preventDefault(); void queueExternalVideo(); }}>
          <label><span>YouTube video URL</span><input type="url" value={externalVideoUrl} onChange={(event) => setExternalVideoUrl(event.target.value)} placeholder="https://www.youtube.com/watch?v=… or https://youtu.be/…" maxLength={2000} required disabled={Boolean(externalJob)} /></label>
          <label><span>Connected channel to use</span><select value={externalAccountId} onChange={(event) => setExternalAccountId(event.target.value)} required disabled={Boolean(externalJob) || youtubeAccounts.length === 0}><option value="">Select a YouTube channel</option>{youtubeAccounts.map((account) => <option value={account.id} key={account.id}>{account.display_name}</option>)}</select></label>
          <button className="primary-button" type="submit" disabled={Boolean(actionKey) || Boolean(externalJob) || !externalVideoUrl.trim() || !externalAccountId || !youtubeAccounts.some((account) => account.id === externalAccountId)}>{externalJob ? `Import ${externalJob.status}…` : actionKey === "external-video" ? "Queueing…" : "Load video comments"}</button>
        </form>
        {(externalMessage || externalJob) && <div className={`external-video-feedback ${externalJob ? "is-running" : ""}`} role="status">{externalJob ? `Importing ${externalJob.videoUrl} as ${externalJob.accountName}. Status: ${externalJob.status}.` : externalMessage}</div>}
        {youtubeAccounts.length === 0 && <p className="external-video-note">Connect a YouTube channel with YouTube comment access first. The importer never uses a different channel identity without an explicit selection.</p>}
        <p className="external-video-note">Only comments available to the YouTube Data API are shown. Private/deleted videos, disabled comments, rate limits, or channel permissions can restrict access.</p>
      </section>}

      {canEdit && <YouTubeCommentQueue apiBaseUrl={apiBaseUrl} accounts={youtubeAccounts} canAdmin={canAdmin} />}

      <section className="panel-card sync-status-panel">
        <div className="panel-heading"><div><h2>Account sync status</h2><p>Sync jobs run in the background; the inbox refreshes while you work.</p></div><span className="muted-label">LATEST · {summary.last_sync_at ? relativeTime(summary.last_sync_at).toUpperCase() : "NO SYNC YET"}</span></div>
        {accounts.length === 0 ? (
          <div className="inbox-empty-inline"><strong>No accounts connected</strong><p>Connect YouTube or Instagram to begin collecting comments.</p><button className="subtle-button" type="button" onClick={onGoAccounts}>Open account connections ↗</button></div>
        ) : (
          <div className="sync-account-list">
            {accounts.map((account) => (
              <article className="sync-account-row" key={account.id}>
                <div className={`account-platform-mark ${account.platform}`}>{account.platform === "youtube" ? "▶" : "◎"}</div>
                <div className="sync-account-copy"><strong>{account.display_name}</strong><small>{account.platform === "youtube" ? "YouTube channel" : `Instagram ${account.username ? `@${account.username}` : "professional account"}`} · Last sync {relativeTime(account.last_sync_completed_at)}</small>{account.last_sync_error_code && <small className="sync-error-copy">{explainSyncError(account.last_sync_error_code)}</small>}</div>
                <span className={`sync-state-pill ${account.last_sync_status}`}>{humanSyncStatus(account.last_sync_status)}</span>
                <div className="sync-account-actions"><small>{account.last_sync_comment_count} comments · {account.last_sync_media_count} posts</small>{canEdit && <button className="subtle-button" type="button" onClick={() => void queueSync(account.id)} disabled={Boolean(actionKey) || account.status !== "connected" || account.last_sync_status === "queued" || account.last_sync_status === "running"} title={account.status !== "connected" ? "Reconnect this account first" : "Synchronize comments"}>{actionKey === `sync-${account.id}` ? "Queueing…" : "Sync now"}</button>}</div>
              </article>
            ))}
          </div>
        )}
        <p className="accounts-footnote">Automatic polling runs on the worker schedule. Provider API permissions and quotas apply. External videos are tracked under the selected channel for inbox and reply context.</p>
      </section>

      <section className="panel-card ai-draft-settings-panel">
        <div className="panel-heading"><div><h2>AI reply copilot</h2><p>Generate contextual drafts from a selected comment. Every draft remains editable and requires explicit review.</p></div><span className={`ai-config-pill ${aiConfigured ? "ready" : "missing"}`}>{aiConfigured ? "READY" : "NOT CONFIGURED"}</span></div>
        {!aiConfigured && <p className="ai-draft-note">Configure <code>AI_API_KEY</code>, <code>AI_BASE_URL</code>, and <code>AI_MODEL</code> in the backend environment. Never expose provider credentials in frontend environment variables.</p>}
        <div className="ai-draft-global-controls">
          <label><span>Reply tone</span><select value={aiTone} onChange={(event) => setAiTone(event.target.value as AiTone)}><option value="helpful">Helpful</option><option value="friendly">Friendly</option><option value="professional">Professional</option><option value="empathetic">Empathetic</option><option value="concise">Concise</option></select></label>
          <label><span>Language</span><select value={aiLanguage} onChange={(event) => setAiLanguage(event.target.value as AiLanguage)}><option value="match_comment">Match comment</option><option value="en">English</option><option value="hi">Hindi</option><option value="hinglish">Hinglish</option><option value="es">Spanish</option><option value="fr">French</option><option value="de">German</option></select></label>
          <label className="ai-draft-instructions"><span>Additional instructions (optional)</span><input value={aiInstructions} onChange={(event) => setAiInstructions(event.target.value)} maxLength={600} placeholder="Keep replies concise; use the product details in the post…" /></label>
        </div>
        <p className="ai-draft-note">AI output can be wrong. The model treats comment text as untrusted content; the app never automatically publishes a generated reply.</p>
        {aiError && <div className="inline-warning ai-draft-feedback" role="alert">{aiError}</div>}{aiNotice && <div className="inline-success ai-draft-feedback" role="status">{aiNotice}</div>}
      </section>

      <section className="panel-card inbox-comments-panel">
        <div className="panel-heading inbox-comments-heading"><div><h2>Comments inbox</h2><p>{pagination.total} result{pagination.total === 1 ? "" : "s"} in the current view</p></div><span className="muted-label">{loading ? "LOADING" : `PAGE ${pagination.pages ? pagination.page : 0} / ${pagination.pages}`}</span></div>
        <div className="inbox-filter-grid">
          <label className="inbox-search-field"><span>Search comments, people or posts</span><input type="search" value={queryDraft} onChange={(event) => setQueryDraft(event.target.value)} placeholder="Search comment text, author, account…" /></label>
          <label><span>Platform</span><select value={platform} onChange={(event) => { setPlatform(event.target.value as "all" | Platform); setPage(1); }}><option value="all">All platforms</option><option value="youtube">YouTube</option><option value="instagram">Instagram</option></select></label>
          <label><span>Account</span><select value={accountId} onChange={(event) => { setAccountId(event.target.value); setPage(1); }}><option value="all">All accounts</option>{activeAccounts.map((account) => <option value={account.id} key={account.id}>{account.display_name}</option>)}</select></label>
          <label><span>Inbox status</span><select value={workflowStatus} onChange={(event) => { setWorkflowStatus(event.target.value as "all" | "open" | "resolved"); setPage(1); }}><option value="open">Open</option><option value="resolved">Resolved</option><option value="all">All statuses</option></select></label>
          <label><span>Reply status</span><select value={responseStatus} onChange={(event) => { setResponseStatus(event.target.value as "all" | "unanswered" | "replied"); setPage(1); }}><option value="all">All comments</option><option value="unanswered">Unanswered</option><option value="replied">Answered by account</option></select></label>
          <label><span>From date</span><input type="date" value={since} max={until || undefined} onChange={(event) => { setSince(event.target.value); setPage(1); }} /></label>
          <label><span>To date</span><input type="date" value={until} min={since || undefined} onChange={(event) => { setUntil(event.target.value); setPage(1); }} /></label>
          <label><span>Sort</span><select value={sort} onChange={(event) => { setSort(event.target.value as "newest" | "oldest"); setPage(1); }}><option value="newest">Newest first</option><option value="oldest">Oldest first</option></select></label>
          <label className="inbox-replies-toggle"><input type="checkbox" checked={includeReplies} onChange={(event) => { setIncludeReplies(event.target.checked); setPage(1); }} /><span>Include thread replies</span></label>
        </div>

        {loading ? <div className="inbox-loading"><span className="loading-mark" /><strong>Loading your conversations…</strong><p>Reading comments from the connected accounts in this workspace.</p></div> : comments.length === 0 ? (
          <div className="inbox-empty-state"><span className="empty-account-icon">▤</span><strong>{accounts.length === 0 ? "Connect an account to get started" : "No comments match these filters"}</strong><p>{accounts.length === 0 ? "Once an account is connected, queue a sync and comments will appear here." : "Try widening your filters, clearing the search, or synchronizing the selected account."}</p>{accounts.length === 0 ? <button type="button" className="primary-button" onClick={onGoAccounts}>Connect an account</button> : <button type="button" className="subtle-button" onClick={() => { setQueryDraft(""); setQuery(""); setPlatform("all"); setAccountId("all"); setWorkflowStatus("open"); setResponseStatus("all"); setIncludeReplies(false); setSince(""); setUntil(""); setPage(1); }}>Clear filters</button>}</div>
        ) : (
          <div className="inbox-comment-list">
            {comments.map((comment) => (
              <article className={`inbox-comment-card ${comment.is_reply ? "is-thread-reply" : ""}`} key={comment.id}>
                <div className="inbox-comment-avatar">{comment.author_avatar_url ? <img src={comment.author_avatar_url} alt="" loading="lazy" referrerPolicy="no-referrer" /> : <span>{(comment.author_name || "?").trim().slice(0, 1).toUpperCase()}</span>}</div>
                <div className="inbox-comment-content">
                  <div className="inbox-comment-meta"><strong>{comment.author_name}</strong>{comment.author_username && <span>@{comment.author_username}</span>}{comment.is_reply && <span className="thread-reply-label">↳ reply{comment.parent_author_name ? ` to ${comment.parent_author_name}` : ""}</span>}<time dateTime={comment.published_at ?? undefined}>{formatDate(comment.published_at)}</time></div>
                  <p className="inbox-comment-text">{comment.text || "(This comment has no text.)"}</p>
                  <div className="inbox-comment-context"><span className={`platform-mini ${comment.platform}`}>{comment.platform === "youtube" ? "▶ YouTube" : "◎ Instagram"}</span><span className="context-separator">·</span><span className="context-account">{comment.account_name}</span><span className="context-separator">·</span>{comment.media.permalink ? <a href={comment.media.permalink} target="_blank" rel="noreferrer" title={comment.media.title}>{comment.media.title || "Open post"} ↗</a> : <span>{comment.media.title}</span>}</div>
                  <div className="inbox-comment-tags"><span className={`comment-status-pill ${comment.workflow_status}`}>{comment.workflow_status === "resolved" ? "Resolved" : "Open"}</span>{!comment.is_reply && <span className={`reply-status-pill ${comment.has_owner_reply || comment.is_owner_comment ? "replied" : "unanswered"}`}>{comment.has_owner_reply || comment.is_owner_comment ? "Answered" : "Needs reply"}</span>}{comment.media.source_type === "external_video" && <span className="reply-status-pill external-video-label">External video</span>}{comment.like_count > 0 && <span className="comment-engagement">♡ {comment.like_count}</span>}{comment.reply_count > 0 && <span className="comment-engagement">↳ {comment.reply_count} replies</span>}</div>
                  {canEdit && !comment.is_owner_comment && <div className="ai-comment-actions"><button type="button" className="subtle-button" onClick={() => void generateAiDraft(comment)} disabled={!aiConfigured || Boolean(actionKey) || comment.is_owner_comment}>{actionKey === `ai-${comment.id}` ? "Queueing draft…" : "✧ Generate AI draft"}</button>{(aiDraftsByComment[comment.id] ?? []).length > 0 && <span>{(aiDraftsByComment[comment.id] ?? []).length} saved draft{(aiDraftsByComment[comment.id] ?? []).length === 1 ? "" : "s"}</span>}</div>}
                  {(aiDraftsByComment[comment.id] ?? []).filter((draft) => draft.status !== "rejected").slice(0, 2).map((draft) => <div className="ai-draft-card" key={draft.id}>
                    <div className="ai-draft-card-heading"><strong>AI suggestion</strong><span className={`ai-draft-status ${draft.status}`}>{draft.status.replaceAll("_", " ")}</span><small>{draft.model}</small></div>
                    {draft.draft_text ? <textarea aria-label="Edit AI reply draft" value={aiDraftEditors[draft.id] ?? draft.draft_text} onChange={(event) => setAiDraftEditors((current) => ({ ...current, [draft.id]: event.target.value }))} rows={3} maxLength={5000} disabled={!canEdit || Boolean(actionKey) || draft.status === "published"} /> : <p className="ai-draft-waiting">{draft.status === "queued" || draft.status === "generating" ? "Generating a suggested reply…" : draft.error_code ? `Draft failed: ${draft.error_code.replaceAll("_", " ")}` : "No draft text is available."}</p>}
                    {draft.error_code && <small className="ai-draft-error">{draft.error_code.replaceAll("_", " ")}</small>}
                    {draft.status !== "published" && draft.draft_text && <div className="ai-draft-actions">{canEdit && <button type="button" className="subtle-button" onClick={() => void updateAiDraft(comment, draft, { draft_text: aiDraftEditors[draft.id] ?? draft.draft_text ?? "" })} disabled={Boolean(actionKey)}>{actionKey === `ai-draft-${draft.id}` ? "Saving…" : "Save edits"}</button>}{canEdit && comment.platform === "youtube" && !comment.is_reply && comment.can_reply && replyAccounts.length > 0 && <button type="button" className="primary-button" onClick={() => void useAiDraft(comment, draft)} disabled={Boolean(actionKey)}>{draft.status === "approved" ? "Use approved draft" : "Approve & use in reply"}</button>}{(!canEdit || comment.platform !== "youtube" || comment.is_reply || !comment.can_reply || replyAccounts.length === 0) && <button type="button" className="subtle-button" onClick={() => { void copyAiDraft(aiDraftEditors[draft.id] ?? draft.draft_text ?? ""); }} disabled={Boolean(actionKey)}>Copy draft</button>}{canEdit && ["draft", "approved"].includes(draft.status) && <button type="button" className="danger-subtle-button" onClick={() => void updateAiDraft(comment, draft, { status: "rejected" })} disabled={Boolean(actionKey)}>Reject</button>}</div>}
                  </div>)}
                  {canEdit && replyingTo === comment.id && <div className="inbox-reply-composer">
                    <label><span>Reply as</span><select value={replyAccountId} onChange={(event) => setReplyAccountId(event.target.value)} disabled={Boolean(actionKey)}>{replyAccounts.map((account) => <option value={account.id} key={account.id}>{account.display_name}</option>)}</select></label>
                    <label><span>Your reply</span><textarea value={replyDraft} onChange={(event) => setReplyDraft(event.target.value)} placeholder="Write a helpful reply…" rows={3} maxLength={10000} disabled={Boolean(actionKey)} /></label>
                    <div className="inbox-reply-composer-actions"><small>{replyDraft.trim().length}/10,000 characters · YouTube may restrict replies on some videos or channels.</small><div><button type="button" className="subtle-button" onClick={() => { setReplyingTo(null); setReplyDraft(""); setReplyAiDraftId(null); }} disabled={Boolean(actionKey)}>Cancel</button><button type="button" className="primary-button" onClick={() => void publishReply(comment)} disabled={Boolean(actionKey) || !replyDraft.trim() || !replyAccountId || replyAccounts.length === 0}>{actionKey === `reply-${comment.id}` ? "Publishing…" : "Publish reply"}</button></div></div>
                  </div>}
                </div>
                <div className="inbox-comment-actions">{canEdit && <button type="button" className="subtle-button" onClick={() => void setCommentWorkflowStatus(comment, comment.workflow_status === "resolved" ? "open" : "resolved")} disabled={Boolean(actionKey)}>{actionKey === comment.id ? "Saving…" : comment.workflow_status === "resolved" ? "Reopen" : "Resolve"}</button>}{canEdit && comment.platform === "youtube" && !comment.is_reply && comment.can_reply && replyAccounts.length > 0 && <button type="button" className="subtle-button reply-open-button" onClick={() => { setReplyingTo((current) => current === comment.id ? null : comment.id); setReplyDraft(""); setReplyAiDraftId(null); setReplyAccountId(replyAccounts.some((account) => account.id === comment.account_id) ? comment.account_id : replyAccounts[0].id); }} disabled={Boolean(actionKey)}>{replyingTo === comment.id ? "Close reply" : "Reply"}</button>}</div>
              </article>
            ))}
          </div>
        )}

        <div className="inbox-pagination"><span>Showing {pagination.total ? (pagination.page - 1) * pagination.page_size + 1 : 0}–{Math.min(pagination.page * pagination.page_size, pagination.total)} of {pagination.total}</span><div><button className="subtle-button" type="button" disabled={loading || page <= 1} onClick={() => setPage((current) => Math.max(1, current - 1))}>← Previous</button><button className="subtle-button" type="button" disabled={loading || !pagination.has_more} onClick={() => setPage((current) => current + 1)}>Next →</button></div></div>
      </section>
    </section>
  );
}
