"""Text-integrity checks that hold a review back from verbatim quoting. Text is never modified.

A review can be fit for theming yet unsafe to quote word for word: an export may have cut
it at Google Play's 500-character limit, or mangled its quoting while being combined from
several files. Quoting such text would put words in a reviewer's mouth that they may not
have written, so these reviews stay in ``reviews.jsonl`` with ``quote_eligible=False`` and
the reasons in ``quote_hold_reasons``. Nothing is dropped here.

The checks run on the **original** raw text (the 500-character limit applies to it), so
:func:`groww_pulse.preprocessing.pipeline.verify_provenance` can re-derive them from the raw
store. Reasons, in a fixed order:

* ``possible_truncation``: the text is within :data:`TRUNCATION_MARGIN_CHARS` of
  :data:`PLAY_REVIEW_MAX_CHARS` and does not end with closing punctuation, or ends with an
  ellipsis (a common exporter truncation marker).
* ``unbalanced_quotes``: an odd number of ASCII double quotes, or unequal counts of curly
  opening and closing double quotes.

Holds are deliberately conservative: a false hold only costs one quote candidate, while a
missed one can misquote a reviewer. An automatic hold is lifted only by importing a
verified, complete text for the same review ID.

**Manual holds.** Some problems no rule can see (a review that starts mid-sentence, or two
texts joined together). The operator lists those in ``data/quote_holds.json``::

    {"holds": [{"review_id": "...", "reason": "starts_mid_sentence",
                "note": "optional free text", "added": "YYYY-MM-DD"}]}

Each entry adds ``manual:<reason>`` to the review's ``quote_hold_reasons``. The file holds
IDs and reason codes only, never review text. A manual hold is lifted by deleting its entry
once the original text has been verified, then re-running ``groww-pulse preprocess``. A
malformed file stops preprocessing and analysis; it is never half-applied.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from groww_pulse.errors import PreprocessingError

#: Google Play's maximum review length, in characters.
PLAY_REVIEW_MAX_CHARS = 500
#: Exporters may strip trailing whitespace or cut at a word boundary just below the limit.
TRUNCATION_MARGIN_CHARS = 10

_CLOSING_CHARS = frozenset(".!?)]\"'”’»")
_ELLIPSES = ("...", "…")

POSSIBLE_TRUNCATION = "possible_truncation"
UNBALANCED_QUOTES = "unbalanced_quotes"

#: Manual hold registry, relative to ``DATA_DIR``.
MANUAL_HOLDS_FILE = Path("quote_holds.json")
MANUAL_PREFIX = "manual:"
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ENTRY_KEYS = frozenset({"review_id", "reason", "note", "added"})


def looks_truncated(text: str) -> bool:
    stripped = text.rstrip()
    if len(stripped) < PLAY_REVIEW_MAX_CHARS - TRUNCATION_MARGIN_CHARS:
        return False
    return stripped.endswith(_ELLIPSES) or stripped[-1] not in _CLOSING_CHARS


def has_unbalanced_quotes(text: str) -> bool:
    return text.count('"') % 2 == 1 or text.count("“") != text.count("”")


def quote_hold_reasons(raw_text: str) -> list[str]:
    """Reasons ``raw_text`` must not be quoted verbatim (empty list means no hold)."""
    reasons = []
    if looks_truncated(raw_text):
        reasons.append(POSSIBLE_TRUNCATION)
    if has_unbalanced_quotes(raw_text):
        reasons.append(UNBALANCED_QUOTES)
    return reasons


def automatic_hold_reasons(reasons: list[str]) -> list[str]:
    """The rule-derived part of a stored ``quote_hold_reasons`` list."""
    return [r for r in reasons if not r.startswith(MANUAL_PREFIX)]


def load_manual_holds(path: Path) -> dict[str, list[str]]:
    """Read the manual hold registry: review ID -> sorted ``manual:<reason>`` codes.

    A missing file means no manual holds.

    Raises:
        PreprocessingError: if the file is not valid JSON or any entry is malformed.
    """
    if not path.exists():
        return {}
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreprocessingError(f"{path.name} is not readable JSON: {exc}") from None
    if not isinstance(data, dict) or set(data) != {"holds"} or not isinstance(data["holds"], list):
        raise PreprocessingError(f'{path.name} must be an object with one key, "holds" (a list)')
    holds: dict[str, set[str]] = {}
    for i, entry in enumerate(data["holds"]):
        where = f"{path.name} holds[{i}]"
        if not isinstance(entry, dict) or not set(entry) <= _ENTRY_KEYS:
            raise PreprocessingError(f"{where}: expected keys {sorted(_ENTRY_KEYS)}")
        review_id, reason = entry.get("review_id"), entry.get("reason")
        if not isinstance(review_id, str) or not review_id.strip():
            raise PreprocessingError(f"{where}: review_id must be a non-empty string")
        if not isinstance(reason, str) or not _REASON_RE.match(reason):
            raise PreprocessingError(f"{where}: reason must be a snake_case code")
        for key in ("note", "added"):
            if key in entry and not isinstance(entry[key], str):
                raise PreprocessingError(f"{where}: {key} must be a string")
        holds.setdefault(review_id, set()).add(MANUAL_PREFIX + reason)
    return {review_id: sorted(reasons) for review_id, reasons in holds.items()}
