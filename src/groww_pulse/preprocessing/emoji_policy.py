"""Emoji policy (architecture §6.3, step 4; FR-7). Text is never modified here.

* ``strict`` (default): any review containing an emoji is excluded.
* ``lenient``: emoji-only reviews are excluded; others are kept for theming but marked
  ``quote_eligible=False`` so they can never be quoted.

"Emoji" means anything the ``emoji`` library recognises, including ZWJ sequences, skin-tone
modifiers, flags, and the emoji-presentation forms of symbols such as © ® ™. Text emoticons
such as ``:)`` and plain symbols such as ★ → ₹ ✓ are not emoji.
"""

from __future__ import annotations

from dataclasses import dataclass

import emoji

from groww_pulse.config import EmojiPolicy
from groww_pulse.preprocessing.normalize import count_words


def contains_emoji(text: str) -> bool:
    return emoji.emoji_count(text) > 0


def is_emoji_only(text: str) -> bool:
    """True if the text has at least one emoji and no words once emoji are removed."""
    return contains_emoji(text) and count_words(emoji.replace_emoji(text, replace="")) == 0


@dataclass(frozen=True)
class EmojiDecision:
    keep: bool
    quote_eligible: bool
    reason: str | None = None


def apply_emoji_policy(text: str, policy: EmojiPolicy) -> EmojiDecision:
    if not contains_emoji(text):
        return EmojiDecision(keep=True, quote_eligible=True)
    if policy is EmojiPolicy.STRICT:
        return EmojiDecision(keep=False, quote_eligible=False, reason="contains_emoji")
    if is_emoji_only(text):
        return EmojiDecision(keep=False, quote_eligible=False, reason="emoji_only")
    return EmojiDecision(keep=True, quote_eligible=False)
