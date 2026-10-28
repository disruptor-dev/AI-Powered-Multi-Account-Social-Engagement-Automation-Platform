from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Tone = Literal["helpful", "friendly", "professional", "empathetic", "concise"]
Language = Literal["match_comment", "en", "hi", "hinglish", "es", "fr", "de"]


class AIReplyDraftRequest(BaseModel):
    tone: Tone = "helpful"
    language: Language = "match_comment"
    custom_instructions: str | None = Field(default=None, max_length=600)

    @field_validator("custom_instructions")
    @classmethod
    def clean_instructions(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            return None
        return value


class AIReplyDraftUpdate(BaseModel):
    draft_text: str | None = Field(default=None, min_length=1, max_length=5000)
    status: Literal["draft", "approved", "rejected"] | None = None

    @field_validator("draft_text")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("Draft text cannot be empty or contain unsupported characters")
        return value

    @model_validator(mode="after")
    def validate_update(self) -> "AIReplyDraftUpdate":
        if self.status is None and self.draft_text is None:
            raise ValueError("Provide a draft_text or status to update")
        if self.status == "approved" and self.draft_text is None:
            # The API may approve the already-saved text; the route checks it exists.
            return self
        return self


class AutomationConditions(BaseModel):
    platform: Literal["all", "youtube", "instagram"] = "all"
    account_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    keywords_any: list[str] = Field(default_factory=list, max_length=20)
    keywords_all: list[str] = Field(default_factory=list, max_length=20)
    exclude_keywords: list[str] = Field(default_factory=list, max_length=20)
    author_contains: str | None = Field(default=None, max_length=120)
    min_likes: int = Field(default=0, ge=0, le=1_000_000)
    only_unanswered: bool = True
    only_open: bool = True
    include_replies: bool = False
    exclude_owner_comments: bool = True

    @field_validator("keywords_any", "keywords_all", "exclude_keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            item = " ".join(raw.split()).strip().casefold()
            if not item:
                continue
            if len(item) > 100:
                raise ValueError("Each keyword must be at most 100 characters")
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    @field_validator("author_contains")
    @classmethod
    def normalize_author(cls, value: str | None) -> str | None:
        return " ".join(value.split()).casefold() if value and value.strip() else None

    @model_validator(mode="after")
    def avoid_conflicting_keywords(self) -> "AutomationConditions":
        excluded = set(self.exclude_keywords)
        if set(self.keywords_any).issubset(excluded) and self.keywords_any:
            raise ValueError("A keyword cannot be both required and excluded")
        return self


class AutomationDraftSettings(BaseModel):
    tone: Tone = "helpful"
    language: Language = "match_comment"
    custom_instructions: str | None = Field(default=None, max_length=600)

    @field_validator("custom_instructions")
    @classmethod
    def clean_instructions(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        return value or None


class AutomationRuleCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool = True
    priority: int = Field(default=100, ge=1, le=1000)
    conditions: AutomationConditions = Field(default_factory=AutomationConditions)
    draft_settings: AutomationDraftSettings = Field(default_factory=AutomationDraftSettings)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Rule name is required")
        return value

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if value and value.strip() else None


class AutomationRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=1, le=1000)
    conditions: AutomationConditions | None = None
    draft_settings: AutomationDraftSettings | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            raise ValueError("Rule name is required")
        return value


class AutomationRunRequest(BaseModel):
    confirm_draft_only: bool = Field(default=False, description="Confirm this rule only generates drafts and never publishes automatically")
