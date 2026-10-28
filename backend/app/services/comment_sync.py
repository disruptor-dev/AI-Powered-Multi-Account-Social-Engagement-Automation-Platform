"""Provider adapters and idempotent persistence for the unified comments inbox.

All external API calls happen in the worker, never in a browser request. Provider
error messages and credentials are deliberately not copied into logs or the database.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import decrypt_secret
from app.models.comment import SocialComment
from app.models.comment_sync_run import CommentSyncRun
from app.models.media_item import MediaItem
from app.models.social_account import SocialAccount
from app.services.account_connections import OAuthFlowError, refresh_social_account

logger = logging.getLogger(__name__)
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
INSTAGRAM_API_ROOT = "https://graph.instagram.com"


class ProviderSyncError(Exception):
    def __init__(self, code: str, *, reauthorization_required: bool = False, retryable: bool = False, outcome_unknown: bool = False) -> None:
        self.code = code
        self.reauthorization_required = reauthorization_required
        self.retryable = retryable
        # True only when the provider may have accepted a write before the connection failed.
        # Such writes must never be retried automatically because the API has no idempotency key.
        self.outcome_unknown = outcome_unknown
        super().__init__(code)


@dataclass
class SyncCounters:
    media_processed: int = 0
    comments_inserted: int = 0
    comments_updated: int = 0
    comments_skipped: int = 0
    warnings: list[str] = field(default_factory=list)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_provider_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _youtube_error_code(payload: Any, http_status: int) -> tuple[str, bool, bool]:
    reason = ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            errors = error.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                reason = str(errors[0].get("reason", "")).lower()
    if reason in {"commentsdisabled", "commentsthreadnotfound", "videoNotFound".lower()}:
        return "comments_unavailable", False, False
    if reason in {"quotaexceeded", "dailylimitexceeded", "ratelimitexceeded"}:
        return "provider_quota_exceeded", False, True
    if reason in {"autherror", "invalidcredentials", "insufficientpermissions", "youtubeSignupRequired".lower()} or http_status == 401:
        return "provider_reauthorization_required", True, False
    if http_status == 403:
        return "provider_permission_denied", False, False
    if http_status == 429:
        return "provider_rate_limited", False, True
    if http_status >= 500 or http_status == 0:
        return "provider_unavailable", False, True
    return "provider_request_failed", False, False


def _instagram_error_code(payload: Any, http_status: int) -> tuple[str, bool, bool]:
    code = 0
    subcode = 0
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error = payload["error"]
        code = _as_int(error.get("code"))
        subcode = _as_int(error.get("error_subcode"))
    if code == 190 or http_status == 401:
        return "provider_reauthorization_required", True, False
    if code in {10, 200} or http_status == 403:
        return "provider_permission_denied", False, False
    if http_status == 429 or code in {4, 17, 32, 613}:
        return "provider_rate_limited", False, True
    if http_status >= 500 or http_status == 0:
        return "provider_unavailable", False, True
    if code == 100:
        return "provider_field_unsupported", False, False
    return "provider_request_failed", False, False


async def _provider_get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    provider: str,
    token: str,
    params: dict[str, Any] | None = None,
    cursor_url: bool = False,
) -> dict[str, Any]:
    params = dict(params or {})
    headers: dict[str, str] = {"Accept": "application/json"}
    if provider == "youtube":
        headers["Authorization"] = f"Bearer {token}"
    else:
        params["access_token"] = token

    # Provider transients are retried quickly; rate-limit responses are not spun on.
    for attempt in range(3):
        try:
            response = await client.get(url, params=params if not cursor_url else None, headers=headers)
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            if attempt < 2:
                await asyncio.sleep(0.4 * (attempt + 1))
                continue
            raise ProviderSyncError("provider_unavailable", retryable=True) from None
        if response.status_code < 400 and not (isinstance(payload, dict) and payload.get("error")):
            if not isinstance(payload, dict):
                raise ProviderSyncError("provider_invalid_response")
            return payload
        code, reauth, retryable = (
            _youtube_error_code(payload, response.status_code)
            if provider == "youtube"
            else _instagram_error_code(payload, response.status_code)
        )
        if retryable and attempt < 2:
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        raise ProviderSyncError(code, reauthorization_required=reauth, retryable=retryable)
    raise ProviderSyncError("provider_unavailable", retryable=True)


async def _paginate_youtube(
    client: httpx.AsyncClient, token: str, path: str, params: dict[str, Any], *, max_pages: int, max_items: int
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    page_token: str | None = None
    for _ in range(max(1, max_pages)):
        request_params = dict(params)
        if page_token:
            request_params["pageToken"] = page_token
        payload = await _provider_get_json(client, f"{YOUTUBE_API}/{path}", provider="youtube", token=token, params=request_params)
        items = payload.get("items", [])
        if isinstance(items, list):
            output.extend(item for item in items if isinstance(item, dict))
        if len(output) >= max_items:
            return output[:max_items]
        page_token = payload.get("nextPageToken")
        if not isinstance(page_token, str) or not page_token:
            break
    return output[:max_items]


async def _paginate_instagram(
    client: httpx.AsyncClient,
    token: str,
    url: str,
    params: dict[str, Any],
    *,
    max_pages: int,
    max_items: int,
    start_after: str | None = None,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    next_url: str | None = None
    next_cursor: str | None = start_after
    original_params = dict(params)
    for _ in range(max(1, max_pages)):
        request_params = dict(original_params)
        if next_cursor:
            request_params["after"] = next_cursor
        if next_url:
            parsed = urlparse(next_url)
            start_host = urlparse(url).hostname
            if parsed.scheme != "https" or parsed.hostname not in {start_host, "graph.instagram.com"}:
                raise ProviderSyncError("provider_pagination_rejected")
            payload = await _provider_get_json(client, next_url, provider="instagram", token=token, cursor_url=True)
        else:
            payload = await _provider_get_json(client, url, provider="instagram", token=token, params=request_params)
        items = payload.get("data", [])
        if isinstance(items, list):
            output.extend(item for item in items if isinstance(item, dict))
        if len(output) >= max_items:
            return output[:max_items]
        paging = payload.get("paging") if isinstance(payload.get("paging"), dict) else {}
        next_url = paging.get("next") if isinstance(paging.get("next"), str) else None
        cursors = paging.get("cursors") if isinstance(paging.get("cursors"), dict) else {}
        next_cursor = cursors.get("after") if isinstance(cursors.get("after"), str) else None
        if not next_url and not next_cursor:
            break
    return output[:max_items]


def _youtube_comment(comment_id: str, snippet: dict[str, Any], parent_id: str | None, account: SocialAccount) -> dict[str, Any]:
    author_channel = snippet.get("authorChannelId")
    author_id = author_channel.get("value") if isinstance(author_channel, dict) else None
    author_name = str(snippet.get("authorDisplayName") or "Unknown user")[:300]
    is_owner = bool(author_id and author_id == account.platform_account_id)
    comment_text = snippet.get("textOriginal") or snippet.get("textDisplay") or ""
    return {
        "platform_comment_id": comment_id,
        "parent_platform_comment_id": parent_id,
        "author_id": str(author_id) if author_id else None,
        "author_name": html.unescape(author_name),
        "author_username": None,
        "author_avatar_url": snippet.get("authorProfileImageUrl"),
        "text": html.unescape(str(comment_text))[:20000],
        "like_count": _as_int(snippet.get("likeCount")),
        "reply_count": _as_int(snippet.get("totalReplyCount")),
        "published_at": parse_provider_datetime(snippet.get("publishedAt")),
        "platform_updated_at": parse_provider_datetime(snippet.get("updatedAt")),
        "is_reply": bool(parent_id),
        "is_owner_comment": is_owner,
        "has_owner_reply": False,
        "raw_metadata": {},
    }


YOUTUBE_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_FORCE_SSL_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


def parse_youtube_video_url(value: str) -> str:
    """Extract an 11-character ID from supported YouTube watch/Shorts/live URLs."""
    candidate = (value or "").strip()
    if not candidate or len(candidate) > 2000:
        raise ValueError("Paste a valid YouTube video URL.")
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        raise ValueError("Paste a valid YouTube video URL.") from None
    allowed_hosts = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
    allowed_port = port in (None, 443 if parsed.scheme == "https" else 80)
    if parsed.scheme not in {"http", "https"} or host not in allowed_hosts or parsed.username or parsed.password or not allowed_port:
        raise ValueError("Only youtube.com and youtu.be video links are supported.")
    video_id: str | None = None
    if host.endswith("youtu.be"):
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif parsed.path.rstrip("/") == "/watch":
        values = parse_qs(parsed.query).get("v", [])
        video_id = values[0] if values else None
    else:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            video_id = parts[1]
    if not video_id or not YOUTUBE_VIDEO_ID_PATTERN.fullmatch(video_id):
        raise ValueError("The URL does not contain a valid YouTube video ID.")
    return video_id


async def _youtube_external_video_snapshot(
    client: httpx.AsyncClient, token: str, account: SocialAccount, settings: Settings, video_id: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch comments for one public video not necessarily owned by the selected channel."""
    video_payload = await _provider_get_json(
        client, f"{YOUTUBE_API}/videos", provider="youtube", token=token,
        params={"part": "snippet,status", "id": video_id, "maxResults": 1},
    )
    video_items = video_payload.get("items", [])
    if not isinstance(video_items, list) or not video_items or not isinstance(video_items[0], dict):
        raise ProviderSyncError("youtube_video_unavailable")
    video = video_items[0]
    snippet = video.get("snippet", {}) if isinstance(video.get("snippet"), dict) else {}
    thumbnails = snippet.get("thumbnails", {}) if isinstance(snippet.get("thumbnails"), dict) else {}
    thumb = thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}
    max_comments = max(1, min(settings.comment_sync_max_external_video_comments, 100000))
    max_replies = max(0, min(settings.comment_sync_max_external_video_replies, 100000))
    max_pages = max(1, (max_comments + 100) // 100)
    media: dict[str, Any] = {
        "platform_media_id": video_id,
        "media_type": "video",  # Shorts and standard videos share the official comment API.
        "source_type": "external_video",
        "title": str(snippet.get("title") or "Untitled video")[:1000],
        "description": str(snippet.get("description") or "")[:50000],
        "permalink": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail_url": thumb.get("url") if isinstance(thumb, dict) else None,
        "published_at": parse_provider_datetime(snippet.get("publishedAt")),
        "raw_metadata": {"video_id": video_id, "external_video": True},
    }
    comments: list[dict[str, Any]] = []
    warnings: list[str] = []
    reply_budget = max_replies
    try:
        # Fetch one extra item so a reached configurable cap can be reported instead
        # of silently telling the UI that the video was completely imported.
        threads = await _paginate_youtube(
            client, token, "commentThreads",
            {"part": "snippet,replies", "videoId": video_id, "maxResults": 100, "order": "time", "textFormat": "plainText"},
            max_pages=max_pages, max_items=max_comments + 1,
        )
        if len(threads) > max_comments:
            warnings.append("external_video_comments_truncated")
            threads = threads[:max_comments]
        seen_ids: set[str] = set()
        for thread in threads:
            thread_snippet = thread.get("snippet", {}) if isinstance(thread.get("snippet"), dict) else {}
            top = thread_snippet.get("topLevelComment", {}) if isinstance(thread_snippet.get("topLevelComment"), dict) else {}
            top_id = str(top.get("id") or "")
            if not top_id:
                continue
            top_snippet = top.get("snippet", {}) if isinstance(top.get("snippet"), dict) else {}
            comment = _youtube_comment(top_id, top_snippet, None, account)
            comment["reply_count"] = _as_int(thread_snippet.get("totalReplyCount"))
            comment["raw_metadata"] = {"can_reply": thread_snippet.get("canReply") if isinstance(thread_snippet.get("canReply"), bool) else None}
            comments.append(comment)
            seen_ids.add(top_id)
            embedded = thread.get("replies", {}).get("comments", []) if isinstance(thread.get("replies"), dict) else []
            thread_reply_ids: set[str] = set()
            for reply in embedded if isinstance(embedded, list) else []:
                if reply_budget <= 0:
                    break
                if not isinstance(reply, dict) or not reply.get("id"):
                    continue
                reply_id = str(reply["id"])
                if reply_id in seen_ids:
                    continue
                reply_snippet = reply.get("snippet", {}) if isinstance(reply.get("snippet"), dict) else {}
                comments.append(_youtube_comment(reply_id, reply_snippet, top_id, account))
                seen_ids.add(reply_id)
                thread_reply_ids.add(reply_id)
                reply_budget -= 1
            if reply_budget > 0 and comment["reply_count"] > len(thread_reply_ids):
                request_limit = min(reply_budget + len(thread_reply_ids), comment["reply_count"])
                if request_limit > 0:
                    try:
                        more = await _paginate_youtube(
                            client, token, "comments",
                            {"part": "snippet", "parentId": top_id, "maxResults": min(100, request_limit), "textFormat": "plainText"},
                            max_pages=max(1, (request_limit + 99) // 100), max_items=request_limit,
                        )
                        for reply in more:
                            reply_id = str(reply.get("id") or "")
                            if not reply_id or reply_id in seen_ids or reply_budget <= 0:
                                continue
                            reply_snippet = reply.get("snippet", {}) if isinstance(reply.get("snippet"), dict) else {}
                            comments.append(_youtube_comment(reply_id, reply_snippet, top_id, account))
                            seen_ids.add(reply_id)
                            thread_reply_ids.add(reply_id)
                            reply_budget -= 1
                    except ProviderSyncError as exc:
                        if exc.code == "comments_unavailable":
                            warnings.append("replies_inaccessible")
                        else:
                            raise
            if len(thread_reply_ids) < comment["reply_count"]:
                warnings.append("external_video_replies_truncated" if reply_budget <= 0 else "replies_inaccessible")
    except ProviderSyncError as exc:
        if exc.code == "comments_unavailable":
            raise ProviderSyncError("youtube_comments_disabled") from None
        if exc.code == "provider_request_failed":
            raise ProviderSyncError("youtube_video_unavailable") from None
        raise
    media["comments"] = comments
    return [media], sorted(set(warnings))


async def _youtube_write_token(
    account: SocialAccount, *, settings: Settings, db: Session | None = None, scope_error_code: str = "comment_publish_scope_missing"
) -> str:
    """Validate the selected connected channel and return its decrypted OAuth access token."""
    if account.platform != "youtube":
        raise ProviderSyncError("publish_platform_unsupported")
    if account.status != "connected":
        raise ProviderSyncError("account_disconnected", reauthorization_required=True)
    if YOUTUBE_FORCE_SSL_SCOPE not in (account.scopes or []):
        raise ProviderSyncError(scope_error_code)
    expiry = account.access_token_expires_at
    if expiry is None or _as_utc(expiry) <= utcnow() + timedelta(minutes=5):
        try:
            await refresh_social_account(account, settings)
            if db is not None:
                db.commit()
        except OAuthFlowError as exc:
            raise ProviderSyncError(exc.code, reauthorization_required=True) from None
        except HTTPException as exc:
            detail = str(exc.detail).lower()
            code = "provider_reauthorization_required" if "reconnect" in detail or "refresh token" in detail else "token_refresh_failed"
            raise ProviderSyncError(code, reauthorization_required=True) from None
    try:
        token = decrypt_secret(account.access_token_encrypted, settings)
    except HTTPException:
        raise ProviderSyncError("token_decryption_failed", reauthorization_required=True) from None
    if not token:
        raise ProviderSyncError("access_token_missing", reauthorization_required=True)
    return token


async def get_youtube_video_details_for_comment_publish(
    account: SocialAccount, *, video_id: str, settings: Settings | None = None, db: Session | None = None
) -> dict[str, Any]:
    """Fetch public target-video context required by commentThreads.insert."""
    settings = settings or get_settings()
    token = await _youtube_write_token(account, settings=settings, db=db, scope_error_code="comment_publish_scope_missing")
    timeout = max(5.0, min(float(settings.comment_sync_request_timeout_seconds), 60.0))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        try:
            response = await client.get(
                f"{YOUTUBE_API}/videos",
                params={"part": "snippet,status", "id": video_id, "maxResults": 1},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise ProviderSyncError("provider_unavailable", retryable=True) from None
    if response.status_code >= 400 or (isinstance(payload, dict) and payload.get("error")):
        code, reauth, retryable = _youtube_error_code(payload, response.status_code)
        if code == "comments_unavailable":
            code = "youtube_video_unavailable"
        raise ProviderSyncError(code, reauthorization_required=reauth, retryable=retryable)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise ProviderSyncError("youtube_video_unavailable")
    snippet = items[0].get("snippet") if isinstance(items[0].get("snippet"), dict) else {}
    channel_id = snippet.get("channelId")
    if not isinstance(channel_id, str) or not channel_id:
        raise ProviderSyncError("provider_invalid_response")
    thumbnails = snippet.get("thumbnails") if isinstance(snippet.get("thumbnails"), dict) else {}
    thumbnail = thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}
    return {
        "channel_id": channel_id,
        "title": str(snippet.get("title") or "External YouTube video")[:1000],
        "description": str(snippet.get("description") or "")[:50000],
        "thumbnail_url": thumbnail.get("url") if isinstance(thumbnail, dict) else None,
        "published_at": parse_provider_datetime(snippet.get("publishedAt")),
    }


async def publish_youtube_top_level_comment(
    account: SocialAccount,
    *,
    video_id: str,
    video_channel_id: str,
    text: str,
    settings: Settings | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Publish one reviewed, new top-level comment as the selected YouTube channel."""
    settings = settings or get_settings()
    if not text or not text.strip() or len(text.strip()) > 10000:
        raise ProviderSyncError("comment_text_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ProviderSyncError("youtube_video_unavailable")
    token = await _youtube_write_token(account, settings=settings, db=db, scope_error_code="comment_publish_scope_missing")
    timeout = max(5.0, min(float(settings.comment_sync_request_timeout_seconds), 60.0))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        try:
            response = await client.post(
                f"{YOUTUBE_API}/commentThreads",
                params={"part": "snippet"},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                json={"snippet": {
                    "channelId": video_channel_id,
                    "videoId": video_id,
                    "topLevelComment": {"snippet": {"textOriginal": text.strip()}},
                }},
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            # The server may have accepted the POST before the connection was lost.
            raise ProviderSyncError("provider_outcome_unknown", retryable=False, outcome_unknown=True) from None
    if response.status_code >= 400 or (isinstance(payload, dict) and payload.get("error")):
        code, reauth, retryable = _youtube_error_code(payload, response.status_code)
        if code == "comments_unavailable":
            code = "youtube_comments_unavailable"
        unknown = response.status_code >= 500
        raise ProviderSyncError(code, reauthorization_required=reauth, retryable=retryable, outcome_unknown=unknown)
    if not isinstance(payload, dict):
        raise ProviderSyncError("provider_outcome_unknown", outcome_unknown=True)
    snippet = payload.get("snippet") if isinstance(payload.get("snippet"), dict) else {}
    top_comment = snippet.get("topLevelComment") if isinstance(snippet.get("topLevelComment"), dict) else {}
    if not isinstance(top_comment.get("id"), str) or not top_comment.get("id"):
        # A 2xx without an ID is insufficient evidence to safely retry the write.
        raise ProviderSyncError("provider_outcome_unknown", outcome_unknown=True)
    return payload


async def publish_youtube_reply(
    account: SocialAccount, *, parent_comment_id: str, text: str, settings: Settings | None = None, db: Session | None = None
) -> dict[str, Any]:
    """Publish a reply through the OAuth identity of the selected connected channel."""
    settings = settings or get_settings()
    if account.platform != "youtube":
        raise ProviderSyncError("reply_platform_unsupported")
    if account.status != "connected":
        raise ProviderSyncError("account_disconnected", reauthorization_required=True)
    if YOUTUBE_FORCE_SSL_SCOPE not in (account.scopes or []):
        raise ProviderSyncError("reply_scope_missing")
    expiry = account.access_token_expires_at
    if expiry is None or _as_utc(expiry) <= utcnow() + timedelta(minutes=5):
        try:
            await refresh_social_account(account, settings)
            if db is not None:
                db.commit()
        except OAuthFlowError as exc:
            raise ProviderSyncError(exc.code, reauthorization_required=True) from None
        except HTTPException as exc:
            detail = str(exc.detail).lower()
            raise ProviderSyncError("provider_reauthorization_required" if "reconnect" in detail or "refresh token" in detail else "token_refresh_failed", reauthorization_required=True) from None
    try:
        token = decrypt_secret(account.access_token_encrypted, settings)
    except HTTPException:
        raise ProviderSyncError("token_decryption_failed", reauthorization_required=True) from None
    if not token:
        raise ProviderSyncError("access_token_missing", reauthorization_required=True)
    if not text.strip() or len(text) > 10000:
        raise ProviderSyncError("reply_text_invalid")
    timeout = max(5.0, min(float(settings.comment_sync_request_timeout_seconds), 60.0))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        try:
            response = await client.post(
                f"{YOUTUBE_API}/comments", params={"part": "snippet"},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                json={"snippet": {"parentId": parent_comment_id, "textOriginal": text.strip()}},
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise ProviderSyncError("provider_unavailable", retryable=True) from None
    if response.status_code >= 400 or (isinstance(payload, dict) and payload.get("error")):
        reason = ""
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            error = payload["error"]
            errors = error.get("errors") if isinstance(error.get("errors"), list) else []
            if errors and isinstance(errors[0], dict):
                reason = str(errors[0].get("reason") or "").lower()
        if reason in {"operationnotsupported", "parentcommentisprivate", "commentsdisabled"}:
            raise ProviderSyncError("reply_not_allowed")
        if reason in {"commentnotfound", "parentcommentnotfound", "videoNotFound".lower()} or response.status_code == 404:
            raise ProviderSyncError("reply_target_unavailable")
        code, reauth, retryable = _youtube_error_code(payload, response.status_code)
        if response.status_code == 403 and reason in {"forbidden", "insufficientpermissions"}:
            raise ProviderSyncError("reply_permission_denied")
        raise ProviderSyncError(code, reauthorization_required=reauth, retryable=retryable)
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
        raise ProviderSyncError("provider_invalid_response")
    # Provider response is consumed by the server only; it is not returned verbatim to clients.
    return payload


async def _youtube_snapshot(
    client: httpx.AsyncClient, token: str, account: SocialAccount, settings: Settings
) -> tuple[list[dict[str, Any]], list[str]]:
    max_media = max(1, min(settings.comment_sync_max_media_per_account, 50))
    max_comments = max(1, settings.comment_sync_max_comments_per_media)
    max_replies = max(0, settings.comment_sync_max_replies_per_media)
    channels = await _provider_get_json(
        client,
        f"{YOUTUBE_API}/channels",
        provider="youtube",
        token=token,
        params={"part": "contentDetails,snippet", "id": account.platform_account_id, "maxResults": 1},
    )
    channel_items = channels.get("items", [])
    if not isinstance(channel_items, list) or not channel_items:
        raise ProviderSyncError("youtube_channel_unavailable", reauthorization_required=False)
    content = channel_items[0].get("contentDetails", {}) if isinstance(channel_items[0], dict) else {}
    related = content.get("relatedPlaylists", {}) if isinstance(content, dict) else {}
    uploads_playlist = related.get("uploads") if isinstance(related, dict) else None
    if not isinstance(uploads_playlist, str) or not uploads_playlist:
        raise ProviderSyncError("youtube_uploads_playlist_unavailable")

    playlist_items = await _paginate_youtube(
        client,
        token,
        "playlistItems",
        {"part": "snippet,contentDetails", "playlistId": uploads_playlist, "maxResults": 50},
        max_pages=max(1, (max_media + 49) // 50),
        max_items=max_media,
    )
    media_refs: list[dict[str, Any]] = []
    for item in playlist_items:
        snippet = item.get("snippet", {}) if isinstance(item.get("snippet"), dict) else {}
        details = item.get("contentDetails", {}) if isinstance(item.get("contentDetails"), dict) else {}
        video_id = details.get("videoId") or (snippet.get("resourceId", {}) if isinstance(snippet.get("resourceId"), dict) else {}).get("videoId")
        if not isinstance(video_id, str) or not video_id:
            continue
        thumbnails = snippet.get("thumbnails", {}) if isinstance(snippet.get("thumbnails"), dict) else {}
        thumb = thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}
        media_refs.append({
            "platform_media_id": video_id,
            "media_type": "video",  # Shorts use the same official commentThreads API.
            "title": str(snippet.get("title") or "Untitled video")[:1000],
            "description": str(snippet.get("description") or "")[:50000],
            "permalink": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail_url": thumb.get("url") if isinstance(thumb, dict) else None,
            "published_at": parse_provider_datetime(details.get("videoPublishedAt") or snippet.get("publishedAt")),
            "raw_metadata": {"video_id": video_id},
        })

    snapshots: list[dict[str, Any]] = []
    warnings: list[str] = []
    for media in media_refs:
        comments: list[dict[str, Any]] = []
        reply_budget = max_replies
        try:
            threads = await _paginate_youtube(
                client,
                token,
                "commentThreads",
                {"part": "snippet,replies", "videoId": media["platform_media_id"], "maxResults": 100, "order": "time", "textFormat": "plainText"},
                max_pages=max(1, (max_comments + 99) // 100),
                max_items=max_comments,
            )
            seen_comment_ids: set[str] = set()
            for thread in threads:
                top = thread.get("snippet", {}).get("topLevelComment", {}) if isinstance(thread.get("snippet"), dict) else {}
                if not isinstance(top, dict) or not top.get("id"):
                    continue
                top_id = str(top["id"])
                top_snippet = top.get("snippet", {}) if isinstance(top.get("snippet"), dict) else {}
                comment = _youtube_comment(top_id, top_snippet, None, account)
                comment["reply_count"] = _as_int(thread.get("snippet", {}).get("totalReplyCount"))
                comments.append(comment)
                seen_comment_ids.add(top_id)
                embedded = thread.get("replies", {}).get("comments", []) if isinstance(thread.get("replies"), dict) else []
                embedded_ids: set[str] = set()
                for reply in embedded if isinstance(embedded, list) else []:
                    if reply_budget <= 0:
                        break
                    if not isinstance(reply, dict) or not reply.get("id"):
                        continue
                    reply_id = str(reply["id"])
                    reply_snippet = reply.get("snippet", {}) if isinstance(reply.get("snippet"), dict) else {}
                    if reply_id not in seen_comment_ids:
                        comments.append(_youtube_comment(reply_id, reply_snippet, top_id, account))
                        seen_comment_ids.add(reply_id)
                        embedded_ids.add(reply_id)
                        reply_budget -= 1
                # The embedded replies field may be partial. Expand a thread only while
                # the remaining per-media budget allows; duplicates returned by comments.list
                # do not consume additional budget.
                if reply_budget > 0 and comment["reply_count"] > len(embedded_ids):
                    request_limit = min(100, reply_budget + len(embedded_ids), comment["reply_count"])
                    if request_limit > 0:
                        try:
                            reply_items = await _paginate_youtube(
                                client,
                                token,
                                "comments",
                                {"part": "snippet", "parentId": top_id, "maxResults": request_limit, "textFormat": "plainText"},
                                max_pages=1,
                                max_items=request_limit,
                            )
                            for reply in reply_items:
                                reply_id = str(reply.get("id") or "")
                                if not reply_id or reply_id in seen_comment_ids or reply_budget <= 0:
                                    continue
                                reply_snippet = reply.get("snippet", {}) if isinstance(reply.get("snippet"), dict) else {}
                                comments.append(_youtube_comment(reply_id, reply_snippet, top_id, account))
                                seen_comment_ids.add(reply_id)
                                reply_budget -= 1
                        except ProviderSyncError as exc:
                            if exc.code == "comments_unavailable":
                                warnings.append("replies_inaccessible")
                            else:
                                raise
                if len(comments) >= max_comments + max_replies:
                    break
        except ProviderSyncError as exc:
            if exc.code == "comments_unavailable":
                warnings.append("comments_unavailable")
                comments = []
            else:
                raise
        media["comments"] = comments
        snapshots.append(media)
    return snapshots, sorted(set(warnings))


def _instagram_comment(item: dict[str, Any], parent_id: str | None, account: SocialAccount) -> dict[str, Any] | None:
    comment_id = item.get("id")
    if not isinstance(comment_id, (str, int)):
        return None
    from_data = item.get("from") if isinstance(item.get("from"), dict) else {}
    username = item.get("username") or from_data.get("username")
    author_id = from_data.get("id")
    author_name = str(username or "Unknown user")[:300]
    is_owner = bool(
        (author_id and str(author_id) == account.platform_account_id)
        or (username and account.username and str(username).casefold() == account.username.casefold())
    )
    return {
        "platform_comment_id": str(comment_id),
        "parent_platform_comment_id": parent_id,
        "author_id": str(author_id) if author_id is not None else None,
        "author_name": author_name,
        "author_username": str(username)[:240] if username else None,
        "author_avatar_url": None,
        "text": str(item.get("text") or "")[:20000],
        "like_count": _as_int(item.get("like_count")),
        "reply_count": _as_int(item.get("reply_count")),
        "published_at": parse_provider_datetime(item.get("timestamp")),
        "platform_updated_at": None,
        "is_reply": bool(parent_id),
        "is_owner_comment": is_owner,
        "has_owner_reply": False,
        "raw_metadata": {},
    }


async def _instagram_snapshot(
    client: httpx.AsyncClient, token: str, account: SocialAccount, settings: Settings
) -> tuple[list[dict[str, Any]], list[str]]:
    version = settings.meta_graph_api_version.strip().strip("/") or "v26.0"
    if not version.startswith("v"):
        version = f"v{version}"
    base = f"{INSTAGRAM_API_ROOT}/{version}"
    media_limit = max(1, settings.comment_sync_max_media_per_account)
    max_comments = max(1, settings.comment_sync_max_comments_per_media)
    max_pages = max(1, settings.comment_sync_max_pages_per_edge)
    media_items = await _paginate_instagram(
        client,
        token,
        f"{base}/{account.platform_account_id}/media",
        {"fields": "id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url,media_url", "limit": min(100, media_limit)},
        max_pages=max_pages,
        max_items=media_limit,
    )
    snapshots: list[dict[str, Any]] = []
    warnings: list[str] = []
    for item in media_items:
        media_id = item.get("id")
        if not isinstance(media_id, (str, int)):
            continue
        caption = str(item.get("caption") or "")
        product_type = str(item.get("media_product_type") or item.get("media_type") or "media").lower()
        media = {
            "platform_media_id": str(media_id),
            "media_type": product_type[:40],
            "title": (caption[:1000] if caption else f"Instagram {product_type}"),
            "description": caption[:50000],
            "permalink": item.get("permalink"),
            "thumbnail_url": item.get("thumbnail_url") or item.get("media_url"),
            "published_at": parse_provider_datetime(item.get("timestamp")),
            "raw_metadata": {"media_type": item.get("media_type"), "media_product_type": item.get("media_product_type")},
        }
        comments: list[dict[str, Any]] = []
        try:
            edge_url = f"{base}/{media_id}/comments"
            try:
                roots = await _paginate_instagram(
                    client,
                    token,
                    edge_url,
                    {"fields": "id,text,timestamp,username,from,like_count,replies{id,text,timestamp,username,from,like_count}", "limit": 100},
                    max_pages=max_pages,
                    max_items=max_comments,
                )
                nested_replies_supported = True
            except ProviderSyncError as exc:
                if exc.code != "provider_field_unsupported":
                    raise
                nested_replies_supported = False
                warnings.append("instagram_replies_not_available")
                roots = await _paginate_instagram(
                    client,
                    token,
                    edge_url,
                    {"fields": "id,text,timestamp,username,from,like_count", "limit": 100},
                    max_pages=max_pages,
                    max_items=max_comments,
                )
            reply_budget = max(0, settings.comment_sync_max_replies_per_media)
            reply_edge_pages = max(1, min(max_pages, 10))
            for root in roots:
                normalized = _instagram_comment(root, None, account)
                reply_data = root.get("replies") if isinstance(root.get("replies"), dict) else {}
                replies = reply_data.get("data", []) if isinstance(reply_data, dict) else []
                if normalized:
                    summary_data = reply_data.get("summary") if isinstance(reply_data, dict) and isinstance(reply_data.get("summary"), dict) else {}
                    normalized["reply_count"] = max(_as_int(summary_data.get("total_count")), len(replies) if isinstance(replies, list) else 0)
                    comments.append(normalized)
                if not nested_replies_supported:
                    continue
                parent_id = str(root.get("id") or "")
                if not parent_id or reply_budget <= 0:
                    continue
                seen_reply_ids: set[str] = set()
                if isinstance(replies, list):
                    for reply in replies:
                        if isinstance(reply, dict):
                            normalized_reply = _instagram_comment(reply, parent_id, account)
                            if normalized_reply:
                                comments.append(normalized_reply)
                                seen_reply_ids.add(normalized_reply["platform_comment_id"])
                                reply_budget -= 1
                                if reply_budget <= 0:
                                    break
                # Fully page the replies edge when the embedded reply connection reports more
                # records than its first page, with a strict per-media budget.
                paging = reply_data.get("paging") if isinstance(reply_data, dict) and isinstance(reply_data.get("paging"), dict) else {}
                has_more = bool(paging.get("next")) or bool((paging.get("cursors") or {}).get("after") if isinstance(paging.get("cursors"), dict) else False)
                if has_more and reply_budget > 0:
                    try:
                        reply_cursors = paging.get("cursors") if isinstance(paging.get("cursors"), dict) else {}
                        after_cursor = reply_cursors.get("after") if isinstance(reply_cursors.get("after"), str) else None
                        more_replies = await _paginate_instagram(
                            client,
                            token,
                            f"{base}/{parent_id}/replies",
                            {"fields": "id,text,timestamp,username,from,like_count", "limit": min(100, reply_budget)},
                            max_pages=reply_edge_pages,
                            max_items=reply_budget if after_cursor else reply_budget + len(seen_reply_ids),
                            start_after=after_cursor,
                        )
                        for reply in more_replies:
                            normalized_reply = _instagram_comment(reply, parent_id, account)
                            if normalized_reply and normalized_reply["platform_comment_id"] not in seen_reply_ids:
                                comments.append(normalized_reply)
                                seen_reply_ids.add(normalized_reply["platform_comment_id"])
                                reply_budget -= 1
                                if reply_budget <= 0:
                                    break
                    except ProviderSyncError as exc:
                        if exc.code in {"provider_permission_denied", "provider_field_unsupported", "provider_request_failed"}:
                            warnings.append("instagram_replies_not_available")
                        else:
                            raise
            # Deduplicate records repeated across nested page payloads.
            by_id: dict[str, dict[str, Any]] = {}
            for comment in comments:
                by_id[comment["platform_comment_id"]] = comment
            comments = list(by_id.values())
        except ProviderSyncError as exc:
            if exc.code == "comments_unavailable":
                warnings.append("comments_unavailable")
                comments = []
            else:
                raise
        media["comments"] = comments
        snapshots.append(media)
    return snapshots, sorted(set(warnings))


def _existing_media(db: Session, account: SocialAccount, media: dict[str, Any]) -> tuple[MediaItem, bool]:
    record = db.scalar(select(MediaItem).where(
        MediaItem.social_account_id == account.id,
        MediaItem.platform_media_id == media["platform_media_id"],
    ))
    is_new = record is None
    if record is None:
        record = MediaItem(
            workspace_id=account.workspace_id,
            social_account_id=account.id,
            platform=account.platform,
            platform_media_id=media["platform_media_id"],
            media_type=media.get("media_type") or "unknown",
            source_type=media.get("source_type") or ("account_media" if account.platform == "instagram" else "account_video"),
            title=media.get("title"),
            description=media.get("description"),
            permalink=media.get("permalink"),
            thumbnail_url=media.get("thumbnail_url"),
            published_at=media.get("published_at"),
            raw_metadata=media.get("raw_metadata") or {},
        )
        db.add(record)
        db.flush()
    else:
        record.media_type = media.get("media_type") or record.media_type
        record.source_type = media.get("source_type") or ("account_media" if account.platform == "instagram" else "account_video")
        record.title = media.get("title")
        record.description = media.get("description")
        record.permalink = media.get("permalink")
        record.thumbnail_url = media.get("thumbnail_url")
        record.published_at = media.get("published_at") or record.published_at
        record.raw_metadata = media.get("raw_metadata") or record.raw_metadata
        record.last_seen_at = utcnow()
    return record, is_new


def _persist_snapshot(db: Session, account: SocialAccount, snapshots: list[dict[str, Any]]) -> SyncCounters:
    counters = SyncCounters()
    for media_data in snapshots:
        media, _ = _existing_media(db, account, media_data)
        counters.media_processed += 1
        incoming_comments = media_data.get("comments", [])
        if not isinstance(incoming_comments, list):
            incoming_comments = []
        comment_ids = [str(item.get("platform_comment_id")) for item in incoming_comments if isinstance(item, dict) and item.get("platform_comment_id")]
        existing: dict[str, SocialComment] = {}
        if comment_ids:
            # Per-media batches avoid giant SQL IN clauses on older SQLite installations.
            for start in range(0, len(comment_ids), 400):
                records = db.scalars(select(SocialComment).where(
                    SocialComment.social_account_id == account.id,
                    SocialComment.media_item_id == media.id,
                    SocialComment.platform_comment_id.in_(comment_ids[start:start + 400]),
                )).all()
                existing.update({record.platform_comment_id: record for record in records})

        by_platform_id: dict[str, SocialComment] = {}
        child_parent_pairs: list[tuple[SocialComment, str]] = []
        owner_reply_parents: set[str] = set()
        for item in incoming_comments:
            if not isinstance(item, dict):
                continue
            platform_comment_id = str(item.get("platform_comment_id") or "")
            if not platform_comment_id:
                continue
            parent_id = item.get("parent_platform_comment_id")
            record = existing.get(platform_comment_id)
            if record is None:
                record = SocialComment(
                    workspace_id=account.workspace_id,
                    social_account_id=account.id,
                    media_item_id=media.id,
                    platform=account.platform,
                    platform_comment_id=platform_comment_id,
                    parent_platform_comment_id=str(parent_id) if parent_id else None,
                    author_id=item.get("author_id"),
                    author_name=(item.get("author_name") or "Unknown user")[:300],
                    author_username=(item.get("author_username") or None),
                    author_avatar_url=item.get("author_avatar_url"),
                    text=item.get("text") or "",
                    like_count=_as_int(item.get("like_count")),
                    reply_count=_as_int(item.get("reply_count")),
                    published_at=item.get("published_at"),
                    platform_updated_at=item.get("platform_updated_at"),
                    is_reply=bool(parent_id),
                    is_owner_comment=bool(item.get("is_owner_comment")),
                    has_owner_reply=bool(item.get("has_owner_reply")),
                    workflow_status="open",
                    raw_metadata=item.get("raw_metadata") or {},
                )
                db.add(record)
                counters.comments_inserted += 1
            else:
                record.parent_platform_comment_id = str(parent_id) if parent_id else None
                record.author_id = item.get("author_id")
                record.author_name = (item.get("author_name") or "Unknown user")[:300]
                record.author_username = item.get("author_username") or None
                record.author_avatar_url = item.get("author_avatar_url")
                record.text = item.get("text") or ""
                record.like_count = _as_int(item.get("like_count"))
                record.reply_count = _as_int(item.get("reply_count"))
                record.published_at = item.get("published_at") or record.published_at
                record.platform_updated_at = item.get("platform_updated_at") or record.platform_updated_at
                record.is_reply = bool(parent_id)
                record.is_owner_comment = bool(item.get("is_owner_comment"))
                record.last_seen_at = utcnow()
                record.raw_metadata = item.get("raw_metadata") or record.raw_metadata
                counters.comments_updated += 1
            by_platform_id[platform_comment_id] = record
            if parent_id:
                child_parent_pairs.append((record, str(parent_id)))
                if bool(item.get("is_owner_comment")):
                    owner_reply_parents.add(str(parent_id))
        db.flush()
        # Map nested replies onto stored parent IDs and preserve local open/resolved states.
        for child, parent_id in child_parent_pairs:
            parent = by_platform_id.get(parent_id) or existing.get(parent_id)
            if parent is None:
                parent = db.scalar(select(SocialComment).where(
                    SocialComment.social_account_id == account.id,
                    SocialComment.media_item_id == media.id,
                    SocialComment.platform_comment_id == parent_id,
                ))
            child.parent_comment_id = parent.id if parent is not None else None
            if parent is not None and child.is_owner_comment:
                parent.has_owner_reply = True
        for parent_platform_id in owner_reply_parents:
            parent = by_platform_id.get(parent_platform_id) or existing.get(parent_platform_id)
            if parent is not None:
                parent.has_owner_reply = True
    db.flush()
    return counters


def _as_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def run_comment_sync(db: Session, run_id: uuid.UUID, account_id: uuid.UUID) -> None:
    """Execute one durable sync run. It is safe to re-run because platform IDs are unique."""
    run = db.get(CommentSyncRun, run_id)
    if run is None:
        logger.warning("Skipped missing comment sync run id=%s", run_id)
        return
    account = db.scalar(select(SocialAccount).where(SocialAccount.id == account_id).with_for_update())
    if account is None or account.workspace_id != run.workspace_id:
        run.status = "error"
        run.error_code = "account_not_found"
        run.finished_at = utcnow()
        db.commit()
        return
    if account.status != "connected":
        run.status = "error"
        run.error_code = "account_disconnected"
        run.finished_at = utcnow()
        account.last_sync_status = "error"
        account.last_sync_error_code = "account_disconnected"
        account.last_sync_completed_at = utcnow()
        db.commit()
        return

    settings = get_settings()
    run.status = "running"
    run.started_at = utcnow()
    account.last_sync_status = "running"
    account.last_sync_started_at = run.started_at
    account.last_sync_error_code = None
    db.commit()

    try:
        expiry = _as_utc(account.access_token_expires_at)
        if expiry is None or expiry <= utcnow() + timedelta(minutes=5):
            try:
                await refresh_social_account(account, settings)
                db.commit()
            except OAuthFlowError as exc:
                raise ProviderSyncError(exc.code, reauthorization_required=True) from None
            except HTTPException as exc:
                db.commit()  # Persist reauthorization_required markers set by the refresh helper.
                detail = str(exc.detail).lower()
                code = "provider_reauthorization_required" if "reconnect" in detail or "refresh token" in detail else "token_refresh_failed"
                raise ProviderSyncError(code, reauthorization_required="reconnect" in detail or "refresh token" in detail) from None
        try:
            token = decrypt_secret(account.access_token_encrypted, settings)
        except HTTPException:
            raise ProviderSyncError("token_decryption_failed", reauthorization_required=True) from None
        if not token:
            raise ProviderSyncError("access_token_missing", reauthorization_required=True)

        timeout = max(5.0, min(float(settings.comment_sync_request_timeout_seconds), 60.0))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            if run.target_platform_media_id:
                if account.platform != "youtube":
                    raise ProviderSyncError("external_video_requires_youtube")
                snapshots, warnings = await _youtube_external_video_snapshot(
                    client, token, account, settings, run.target_platform_media_id
                )
            elif account.platform == "youtube":
                snapshots, warnings = await _youtube_snapshot(client, token, account, settings)
            elif account.platform == "instagram":
                snapshots, warnings = await _instagram_snapshot(client, token, account, settings)
            else:
                raise ProviderSyncError("unsupported_platform")

        counters = _persist_snapshot(db, account, snapshots)
        counters.warnings = warnings
        run.media_processed = counters.media_processed
        run.comments_inserted = counters.comments_inserted
        run.comments_updated = counters.comments_updated
        run.comments_skipped = counters.comments_skipped
        run.status = "partial" if warnings else "success"
        run.error_code = warnings[0] if warnings else None
        run.finished_at = utcnow()
        account.last_sync_status = run.status
        account.last_sync_completed_at = run.finished_at
        account.last_sync_error_code = ",".join(warnings[:3]) if warnings else None
        account.last_sync_media_count = counters.media_processed
        account.last_sync_comment_count = counters.comments_inserted + counters.comments_updated
        account.last_error_code = None
        db.commit()
        logger.info(
            "Comment sync completed: account_id=%s platform=%s status=%s media=%d inserted=%d updated=%d warnings=%s",
            account.id, account.platform, run.status, counters.media_processed,
            counters.comments_inserted, counters.comments_updated, warnings,
        )
    except ProviderSyncError as exc:
        db.rollback()
        run = db.get(CommentSyncRun, run_id)
        account = db.get(SocialAccount, account_id)
        if run is not None:
            run.status = "error"
            run.error_code = exc.code
            run.finished_at = utcnow()
        if account is not None:
            account.last_sync_status = "error"
            account.last_sync_completed_at = utcnow()
            account.last_sync_error_code = exc.code
            if exc.reauthorization_required:
                account.status = "reauthorization_required"
                account.last_error_code = exc.code
        db.commit()
        logger.warning("Comment sync failed: account_id=%s code=%s retryable=%s", account_id, exc.code, exc.retryable)
    except Exception:
        db.rollback()
        run = db.get(CommentSyncRun, run_id)
        account = db.get(SocialAccount, account_id)
        if run is not None:
            run.status = "error"
            run.error_code = "sync_internal_error"
            run.finished_at = utcnow()
        if account is not None:
            account.last_sync_status = "error"
            account.last_sync_completed_at = utcnow()
            account.last_sync_error_code = "sync_internal_error"
        db.commit()
        logger.exception("Unexpected comment sync error: account_id=%s run_id=%s", account_id, run_id)
