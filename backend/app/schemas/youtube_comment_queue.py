from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

class YouTubeHumanCommentCreate(BaseModel):
    """Create a queue item from text authored by the user; no AI generation occurs."""

    account_id: uuid.UUID
    video_url: str = Field(min_length=8, max_length=2000)
    comment_text: str = Field(min_length=1, max_length=10000)
    idempotency_key: str = Field(min_length=8, max_length=100)
    confirm_one_channel_one_video: bool = Field(default=False)

    @field_validator("video_url")
    @classmethod
    def clean_url(cls, value: str) -> str:
        value = value.strip()
        if not value.lower().startswith(("https://www.youtube.com/", "https://youtube.com/", "https://youtu.be/", "https://m.youtube.com/")):
            raise ValueError("Enter a valid HTTPS YouTube video URL")
        return value

    @field_validator("comment_text")
    @classmethod
    def clean_comment_text(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("Write a non-empty human-authored comment")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def clean_idempotency_key(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(ch) < 32 for ch in value):
            raise ValueError("Invalid idempotency key")
        return value


class YouTubeCommentDraftApproval(BaseModel):
    approved_text: str = Field(min_length=1, max_length=10000)
    scheduled_at: datetime
    confirm_target_and_channel: bool = False
    confirm_relevance_and_nonspam: bool = False

    @field_validator("approved_text")
    @classmethod
    def clean_text(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("Final comment text must be non-empty")
        return value

    @field_validator("scheduled_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduled_at must include a timezone offset")
        return value


class YouTubeQuotaResumeRequest(BaseModel):
    confirm_quota_checked: bool = False


class YouTubeQueueOutcomeResolution(BaseModel):
    outcome: Literal["posted", "not_posted"]
    confirm_checked_youtube: bool = False
    provider_comment_id: str | None = Field(default=None, min_length=3, max_length=240)
    resolution_note: str = Field(min_length=12, max_length=500)

    @field_validator("provider_comment_id")
    @classmethod
    def clean_provider_comment_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or any(ord(ch) < 32 for ch in value):
            raise ValueError("Invalid YouTube comment ID")
        return value

    @field_validator("resolution_note")
    @classmethod
    def clean_resolution_note(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 12:
            raise ValueError("Explain what you verified on YouTube")
        return value

    @model_validator(mode="after")
    def validate_resolution(self):
        if self.outcome == "posted" and not self.provider_comment_id:
            raise ValueError("A YouTube comment ID is required when confirming that a comment was posted")
        if self.outcome == "not_posted" and self.provider_comment_id:
            raise ValueError("Do not provide a comment ID when confirming that the comment was not posted")
        return self
