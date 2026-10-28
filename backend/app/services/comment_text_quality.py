"""Lightweight deterministic text checks shared by human and AI draft flows.

This module never calls a model or generates comment text.
"""
from __future__ import annotations

import re


def video_comment_quality_warnings(text: str) -> list[str]:
    """Flag generic/emoji-heavy text for human review; never auto-publish."""
    normalized = " ".join((text or "").casefold().split())
    warnings: list[str] = []
    generic = re.compile(
        r"\b(great video|nice content|subscribed|i subscribed|great content|amazing video|nice video|"
        r"awesome video|love your content|keep it up)\b",
        re.IGNORECASE,
    )
    if generic.search(normalized):
        warnings.append("generic_or_subscription_phrase")
    emoji_count = len(re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text or ""))
    if emoji_count > 1:
        warnings.append("excessive_emoji")
    if len(normalized) < 20:
        warnings.append("very_short_comment_review_required")
    return warnings
