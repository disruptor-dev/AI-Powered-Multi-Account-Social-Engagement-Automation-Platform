from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class CommentSyncRequest(BaseModel):
    account_id: uuid.UUID | None = None


class CommentWorkflowStatusUpdate(BaseModel):
    workflow_status: Literal["open", "resolved"] = Field(description="Local inbox workflow state")


class ExternalVideoSyncRequest(BaseModel):
    account_id: uuid.UUID = Field(description="Connected YouTube channel that will own the imported inbox view")
    video_url: str = Field(min_length=8, max_length=2000, description="Public YouTube video, Shorts, or youtu.be URL")

    @field_validator("video_url")
    @classmethod
    def trim_video_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Paste a YouTube video URL")
        return value


class PublishCommentReplyRequest(BaseModel):
    account_id: uuid.UUID = Field(description="Connected YouTube channel to publish as")
    text: str = Field(min_length=1, max_length=10000, description="Reply text")
    ai_draft_id: uuid.UUID | None = Field(default=None, description="Optional previously approved AI draft to mark published after provider confirmation")

    @field_validator("text")
    @classmethod
    def validate_reply_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Reply cannot be empty")
        if "\x00" in value:
            raise ValueError("Reply contains an unsupported character")
        return value


class MultiChannelCommentDraft(BaseModel):
    account_id: uuid.UUID = Field(description="Connected YouTube channel that will publish this distinct comment")
    text: str = Field(min_length=1, max_length=10000, description="One manually reviewed top-level comment")

    @field_validator("text")
    @classmethod
    def validate_comment_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Each selected channel needs a non-empty comment")
        if "\x00" in value:
            raise ValueError("Comment contains an unsupported character")
        return value


class MultiChannelPublishRequest(BaseModel):
    video_url: str = Field(min_length=8, max_length=2000)
    comments: list[MultiChannelCommentDraft] = Field(min_length=1, max_length=10)
    idempotency_key: str = Field(min_length=16, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    confirm_publish: bool = Field(default=False, description="Explicit confirmation that all listed comments should be published")
    confirm_nonspam: bool = Field(default=False, description="Explicit confirmation that comments are relevant, distinct, and not spam")

    @field_validator("video_url")
    @classmethod
    def trim_video_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Paste a YouTube video URL")
        return value

    @field_validator("comments")
    @classmethod
    def validate_distinct_channel_ids(cls, value: list[MultiChannelCommentDraft]) -> list[MultiChannelCommentDraft]:
        account_ids = [str(item.account_id) for item in value]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("Select each YouTube channel only once")
        return value
