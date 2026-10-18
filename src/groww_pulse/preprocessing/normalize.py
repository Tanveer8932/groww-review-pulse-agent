"""Text normalization and the shared word counter (architecture §6.3, step 1).

``count_words`` is the single definition of a "word" for the whole project: the length
filter here and the 250-word report validator (Phase 4) both use it.
"""

from __future__ import annotations

import re
import unicodedata

#: A word is a run of word characters, apostrophes, or hyphens bounded by word characters,
#: so "don't" and "well-known" are one word each and bare punctuation ("!!!", ":)", "--")
#: is not a word at all.
WORD_RE = re.compile(r"\b[\w'’-]+\b")

# Invisible format characters that carry no meaning in review text. The zero-width joiner
# (U+200D) is deliberately kept: it is part of emoji ZWJ sequences, which the emoji policy
# must still see.
_INVISIBLE = dict.fromkeys(
    [0x200B, 0x200C, 0xFEFF, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]
)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Unicode NFC, invisible/control characters removed, whitespace collapsed, trimmed.

    Only whitespace and invisible characters change; every visible character (including
    case and punctuation) is preserved, so words are never altered.
    """
    text = unicodedata.normalize("NFC", text).translate(_INVISIBLE)
    # Control characters (category Cc) other than whitespace become spaces, then collapse.
    text = "".join(
        " " if unicodedata.category(ch) == "Cc" and not ch.isspace() else ch for ch in text
    )
    return _WHITESPACE_RE.sub(" ", text).strip()


def count_words(text: str) -> int:
    """Number of word tokens in ``text`` (see :data:`WORD_RE`)."""
    return len(WORD_RE.findall(text))


def dedupe_key(text: str) -> str:
    """Case- and whitespace-insensitive form used for exact-duplicate detection."""
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFC", text).casefold()).strip()
