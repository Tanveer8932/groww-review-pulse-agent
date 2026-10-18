"""PII redaction (architecture §6.3, step 7; FR-9).

Each detected value is replaced by a typed placeholder (``[EMAIL]``, ``[PHONE]``, ...).
Only the matched span changes: every other character of the review, including the
punctuation next to a redacted value, is left exactly as it was. Redaction is idempotent:
placeholders never match any rule, so ``redact(redact(x)) == redact(x)``.

Rules run in a fixed order, so a value claimed by an earlier rule is never re-labelled:

==============  ==========================================================================
Placeholder     Rule
==============  ==========================================================================
``[URL]``       ``http(s)://`` or ``www.`` links (trailing sentence punctuation kept)
``[EMAIL]``     ``local@domain.tld``, and obfuscated ``name at gmail dot com`` /
                ``name[at]gmail[dot]com``
``[UPI_ID]``    ``handle@psp`` (an ``@`` address without a dotted domain)
``[NAME]``      ``@handle`` mentions; the name after "my/his/her ... name is" (capitalised,
                or lowercase when the word is not English); a capitalised name after an
                honorific (Mr, Mrs, Ms, Shri, Smt, Sri), after "myself" or "named", after
                "agent"/"executive"/"representative", or in a closing signature
                ("Regards, Asha Rao" at the end of the review)
``[ADDRESS]``   the rest of the clause after "my address is", "I live/stay/reside at",
                "residing at", or "address:" when it contains a digit; house, flat, plot,
                door, or shop numbers ("flat no 302"); and 6-digit PIN codes after "pin",
                "pincode", "postal code", or "zip code"
``[ACCOUNT_NO]`` (context) an identifier containing a digit after "client/customer/user/
                demat/trading/BO/DP/UCC/folio id/code/no/number", and NSDL DP IDs
                (``IN`` + 14 digits)
``[PAN]``       5 letters, 4 digits, 1 letter (any case)
``[PHONE]``     Indian mobile numbers (6-9 + 9 digits, optional +91/91/0 prefix, common
                groupings), other ``+``-prefixed international numbers, and Indian
                landlines written with an STD code (``022-23456789``, ``080 2345 6789``)
``[AADHAAR]``   12 digits (optionally grouped 4-4-4) starting 2-9 with a valid Verhoeff
                checksum
``[CARD]``      13-19 digits (contiguous or 4-digit groups) with a valid Luhn checksum
``[ACCOUNT_NO]`` any remaining contiguous run of 9-18 digits, or 4-4-4(-4) digit groups
==============  ==========================================================================

Amounts written with thousands separators (``₹10,000``, ``1,00,000``), dates, times,
numbers of 8 digits or fewer, OTPs, order or ticket references without an account
keyword, company names (``Groww``, ``groww.in``), toll-free numbers (``1800 …``), and
city-level locations ("I live in Pune") are never redacted.

Name rules use the English lexicon (:mod:`groww_pulse.preprocessing.lexicon`) to avoid
redacting ordinary words: "my name is not shown" and "an agent named Support" change
nothing. Free-text names with no cue ("Rahul from Pune said") cannot be detected by rules
and are a documented limitation.

:data:`PII_RULES_VERSION` changes whenever a rule changes. Stored ``reviews.jsonl`` files
made with older rules fail the provenance re-derivation, so preprocessing must be re-run.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from groww_pulse.preprocessing.lexicon import DOMAIN_TERMS, FUNCTION_WORDS, is_english_word

PII_RULES_VERSION = "2"

PLACEHOLDERS: tuple[str, ...] = (
    "URL",
    "EMAIL",
    "UPI_ID",
    "NAME",
    "ADDRESS",
    "PAN",
    "PHONE",
    "AADHAAR",
    "CARD",
    "ACCOUNT_NO",
)

# ---------------------------------------------------------------- checksums
_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_valid(digits: str) -> bool:
    check = 0
    for i, ch in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][int(ch)]]
    return check == 0


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        value = int(ch)
        if i % 2 == 1:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


# ---------------------------------------------------------------- rules
# A number must not touch other digits, letters, or decimal/thousands separators that
# would make it part of a larger number ("1,234,567,890" or "12.3456789012").
_NB = r"(?<![\w.,+@])"
_NA = r"(?![\w@]|[.,/:]\d|-\d)"

_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"']*[^\s<>\"'.,!?;:)\]}]")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
_UPI_RE = re.compile(r"(?<![\w.@-])[A-Za-z0-9._-]{2,256}@[A-Za-z][A-Za-z0-9]{1,63}\b(?!\.\w)")
#: Bracketed "[at]"/"(at)" forms with any domain; the spaced "x at y dot com" form only for
#: well-known mail providers, so phrases like "look at this dot com bubble" are left alone.
_EMAIL_OBFUSCATED_RE = re.compile(
    r"(?i)\b[A-Za-z0-9._%+-]+"
    r"(?:\s*(?:\[at\]|\(at\)|\{at\})\s*[A-Za-z0-9-]+\s*(?:\[dot\]|\(dot\)|\{dot\}|\.)\s*"
    r"(?:co\.in|[a-z]{2,6})"
    r"|\s+at\s+(?:gmail|yahoo|outlook|hotmail|rediffmail|icloud|protonmail|ymail|live)"
    r"\s*(?:\s+dot\s+|\.)\s*(?:co\.in|com|in))\b"
)
_HANDLE_RE = re.compile(r"(?<![\w@\[])@[A-Za-z0-9_](?:[A-Za-z0-9_.]{0,28}[A-Za-z0-9_])?")
_NAME_WORD = r"[A-Za-z]+"
_NAME_TOKENS = rf"({_NAME_WORD}(?:[ \t]+{_NAME_WORD}){{0,2}})"
_CAP_TOKENS = r"([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){0,2})"
#: Words allowed between the owner and "name" ("my father's name is").
_NAME_OWNERS = (
    "father|mother|wife|husband|son|daughter|brother|sister|friend|nominee|full|real|first"
    "|last|user"
)
_MY_NAME_RE = re.compile(
    r"(?i:\b(?:my|his|her|our|their)\s+(?:(?:" + _NAME_OWNERS + r")(?:'s)?\s+)?"
    r"name(?:\s+is|'s|\s*:))\s+" + _NAME_TOKENS
)
_HONORIFIC_RE = re.compile(r"\b(?:Mr|Mrs|Ms|Shri|Smt|Sri)\.?[ \t]+" + _CAP_TOKENS)
_CUED_NAME_RE = re.compile(
    r"(?:\b[Mm]yself|\bnamed|(?i:\b(?:agent|executive|representative)))[ \t]+" + _CAP_TOKENS
)
_SIGNATURE_RE = re.compile(
    r"(?i:\b(?:regards|thanks|thank you|sincerely))\s*[,:-]?\s+" + _CAP_TOKENS + r"\W*$"
)
_ADDRESS_CUE_RE = re.compile(
    r"(?i:\bmy\s+(?:home\s+|current\s+|registered\s+|permanent\s+)?address\s+is"
    r"|\bI\s+(?:live|stay|reside)\s+at|\bresiding\s+at|\baddress\s*:)\s*"
    r"([^.!?\n\[\]]{2,120}\b)"
)
_HOUSE_NO_RE = re.compile(
    r"(?i)\b(?:flat|house|h|plot|door|shop|apartment|apt|room)\s*\.?\s*(?:no|number|#)"
    r"\s*\.?\s*[:-]?\s*[A-Za-z]?\d[\w/-]*"
)
_PIN_CODE_RE = re.compile(
    r"(?i:\b(?:pin\s*code|pincode|postal\s+code|zip\s*code|pin))\s*(?:no\.?|number)?\s*"
    r"[:-]?\s*([1-9]\d{2}\s?\d{3})(?![\w@])"
)
_ACCOUNT_CONTEXT_RE = re.compile(
    r"(?i:\b(?:(?:client|customer|user|demat|trading|bo|dp|folio)\s*(?:id|code|no\.?"
    r"|number|account(?:\s+(?:no\.?|number))?)|ucc)(?:\s+is)?)\s*[:#-]?\s*"
    r"(?=[A-Za-z0-9]*\d)([A-Za-z0-9]{5,20})\b"
)
_NSDL_DP_RE = re.compile(r"\bIN\d{6}[\s-]?\d{8}\b")
_PAN_RE = re.compile(r"(?i)(?<![\w\[])[A-Z]{5}[0-9]{4}[A-Z](?![\w\]])")
_PHONE_IN_RE = re.compile(
    _NB + r"(?:(?:\+|00)?91[\s-]?|0)?"
    r"(?:[6-9]\d{9}|[6-9]\d{4}[\s-]\d{5}|[6-9]\d{2}[\s-]\d{3}[\s-]\d{4})" + _NA
)
_PHONE_INTL_RE = re.compile(r"(?<![\w+])\+\d{1,3}[\s-]?\d(?:[\s-]?\d){6,12}" + _NA)
_LANDLINE_RE = re.compile(_NB + r"0\d{2,4}[\s-]\d{3,4}[\s-]?\d{3,4}" + _NA)
_AADHAAR_RE = re.compile(_NB + r"[2-9]\d{3}([\s-]?)\d{4}\1\d{4}" + _NA)
_CARD_RE = re.compile(
    _NB + r"(?:\d{13,19}|\d{4}([\s-])\d{4}\1\d{4}\1\d{1,7}|\d{4}([\s-])\d{6}\2\d{5})" + _NA
)
_ACCOUNT_RE = re.compile(_NB + r"(?:\d{9,18}|\d{4}([\s-])\d{4}\1\d{4}(?:\1\d{4})?)" + _NA)


def _always(_: re.Match[str]) -> bool:
    return True


def _has_digit(match: re.Match[str]) -> bool:
    return any(ch.isdigit() for ch in match.group(1))


def _landline_ok(match: re.Match[str]) -> bool:
    return len(_digits(match.group(0))) in (10, 11)


def _is_name_token(token: str, *, english_ok: bool) -> bool:
    """Could ``token`` be part of a person's name? (see the name rules above)."""
    lower = token.lower()
    if lower in FUNCTION_WORDS or lower in DOMAIN_TERMS:
        return False
    if token[0].isupper():
        return english_ok or not is_english_word(token)
    return not is_english_word(token)


def _name_span(*, english_ok: bool) -> Callable[[re.Match[str]], tuple[int, int] | None]:
    """Span of the leading name-like tokens in group 1, or ``None`` if there are none.

    Whether lowercase tokens can occur is decided by each rule's regex: only the
    "my name is" rule admits them.
    """

    def span(match: re.Match[str]) -> tuple[int, int] | None:
        end = None
        for token in re.finditer(r"\S+", match.group(1)):
            if not _is_name_token(token.group(0), english_ok=english_ok):
                break
            end = match.start(1) + token.end()
        return None if end is None else (match.start(1), end)

    return span


def _aadhaar_ok(match: re.Match[str]) -> bool:
    return verhoeff_valid(_digits(match.group(0)))


def _card_ok(match: re.Match[str]) -> bool:
    digits = _digits(match.group(0))
    return 13 <= len(digits) <= 19 and luhn_valid(digits)


@dataclass(frozen=True)
class _Rule:
    label: str
    pattern: re.Pattern[str]
    accept: Callable[[re.Match[str]], bool] = _always
    group: int = 0  # which group is replaced (0 = the whole match)
    #: Overrides ``group``: the absolute span to replace, or ``None`` to leave the match.
    span: Callable[[re.Match[str]], tuple[int, int] | None] | None = None


_RULES: tuple[_Rule, ...] = (
    _Rule("URL", _URL_RE),
    _Rule("EMAIL", _EMAIL_RE),
    _Rule("EMAIL", _EMAIL_OBFUSCATED_RE),
    _Rule("UPI_ID", _UPI_RE),
    _Rule("NAME", _HANDLE_RE),
    _Rule("NAME", _MY_NAME_RE, span=_name_span(english_ok=True)),
    _Rule("NAME", _HONORIFIC_RE, span=_name_span(english_ok=True)),
    _Rule("NAME", _CUED_NAME_RE, span=_name_span(english_ok=False)),
    _Rule("NAME", _SIGNATURE_RE, span=_name_span(english_ok=False)),
    _Rule("ADDRESS", _ADDRESS_CUE_RE, _has_digit, group=1),
    _Rule("ADDRESS", _HOUSE_NO_RE),
    _Rule("ADDRESS", _PIN_CODE_RE, group=1),
    _Rule("ACCOUNT_NO", _ACCOUNT_CONTEXT_RE, group=1),
    _Rule("ACCOUNT_NO", _NSDL_DP_RE),
    _Rule("PAN", _PAN_RE),
    _Rule("PHONE", _PHONE_IN_RE),
    _Rule("PHONE", _PHONE_INTL_RE),
    _Rule("PHONE", _LANDLINE_RE, _landline_ok),
    _Rule("AADHAAR", _AADHAAR_RE, _aadhaar_ok),
    _Rule("CARD", _CARD_RE, _card_ok),
    _Rule("ACCOUNT_NO", _ACCOUNT_RE),
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _apply(rule: _Rule, text: str, counts: dict[str, int]) -> str:
    if rule.span is not None:
        return _apply_span_rule(rule, rule.span, text, counts)

    def replace(match: re.Match[str]) -> str:
        if not rule.accept(match):
            return match.group(0)
        counts[rule.label] = counts.get(rule.label, 0) + 1
        whole, start, end = match.group(0), match.start(rule.group), match.end(rule.group)
        offset = match.start(0)
        return whole[: start - offset] + f"[{rule.label}]" + whole[end - offset :]

    return rule.pattern.sub(replace, text)


def _apply_span_rule(
    rule: _Rule,
    span: Callable[[re.Match[str]], tuple[int, int] | None],
    text: str,
    counts: dict[str, int],
) -> str:
    """Like ``pattern.sub``, but the next search resumes right after the replaced span.

    A cue rule's match may run past the name it redacts (it looks ahead at up to three
    words), and a second cue can start inside that look-ahead ("MY NAME IS A My name is
    B"). Resuming after the replaced span, not after the match, finds it in one pass,
    which keeps redaction idempotent.
    """
    parts: list[str] = []
    copied = position = 0
    while match := rule.pattern.search(text, position):
        found = span(match) if rule.accept(match) else None
        if found is None:
            position = match.start(1)  # group 1 always starts after the cue
            continue
        start, end = found
        parts += [text[copied:start], f"[{rule.label}]"]
        counts[rule.label] = counts.get(rule.label, 0) + 1
        copied = position = end
    parts.append(text[copied:])
    return "".join(parts)


def redact(text: str) -> RedactionResult:
    """Replace every detected PII value with its placeholder; return text and counts."""
    counts: dict[str, int] = {}
    for rule in _RULES:
        text = _apply(rule, text, counts)
    return RedactionResult(text=text, counts=dict(sorted(counts.items())))


def scan_pii(text: str) -> dict[str, int]:
    """Counts of PII values still present in ``text`` (empty means clean)."""
    return redact(text).counts
