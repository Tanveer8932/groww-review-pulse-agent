"""Validators for a rendered pulse (architecture §6.5; Phase 4).

Every check returns a :class:`CheckResult`. A pulse is saved only if all checks pass; the
names and details of failed checks become the :class:`ReportValidationError` message, so a
refusal always says exactly why. Messages carry counts, placeholder types, and positions,
never review text or detected personal data.

Checks:

* ``word_limit``: at most :data:`MAX_WORDS` words (the shared ``count_words``) in the full
  plain-text and Markdown files, audit marker included.
* ``three_three_three``: exactly 3 themes, 3 quotes, and 3 actions, both in the verified
  content and as rendered lines.
* ``quotes_verbatim``: each quote appears exactly once in each rendering, exactly as stored,
  with the quotation marks enclosing only the verbatim text and any ellipsis outside them.
* ``numbers_computed``: every number outside the quotes is one of the computed values
  (window, counts, ranks, percentages, ratings, dates, ISO week).
* ``no_pii``: a PII scan of every component and of both full renderings finds nothing. A
  hit fails the run; quotes are never altered to make the scan pass.
* ``no_synthetic``: no synthetic-fixture marker or banner appears unless synthetic data was
  explicitly allowed (tests only).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from groww_pulse.preprocessing.normalize import count_words
from groww_pulse.preprocessing.pii import scan_pii
from groww_pulse.preprocessing.pipeline import SYNTHETIC_MARKER_RE
from groww_pulse.report.render import (
    SYNTHETIC_BANNER,
    PulseContent,
    RenderedPulse,
    pct,
    quote_display,
)

MAX_WORDS = 250
THEMES_REQUIRED = QUOTES_REQUIRED = ACTIONS_REQUIRED = 3

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
#: Longer numbers are not echoed in error messages: they may be personal data.
_MAX_ECHOED_DIGITS = 6
_SECTIONS = ("Top themes", "What users say", "Action ideas")


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def _result(name: str, problems: list[str], ok_detail: str) -> CheckResult:
    return CheckResult(name, not problems, "; ".join(problems) if problems else ok_detail)


# ---------------------------------------------------------------- individual checks
def check_word_limit(rendered: RenderedPulse, limit: int = MAX_WORDS) -> CheckResult:
    words = count_words(rendered.full_text)
    md_words = count_words(rendered.full_markdown)
    problems = []
    if words > limit:
        problems.append(f"plain text has {words} words; the limit is {limit}")
    if md_words > limit:
        problems.append(f"Markdown has {md_words} words; the limit is {limit}")
    return _result("word_limit", problems, f"{words} words (limit {limit})")


def _section_lines(body: str, heading: str) -> list[str]:
    lines = body.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip("*") == heading)
    except StopIteration:
        return []
    out: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip("*_") in _SECTIONS or (out and not line.strip()):
            break
        if line.strip():
            out.append(line)
    return out


def check_counts(content: PulseContent, rendered: RenderedPulse) -> CheckResult:
    problems = []
    expected = {
        "themes": (len(content.themes), THEMES_REQUIRED),
        "quotes": (len(content.quotes), QUOTES_REQUIRED),
        "actions": (len(content.actions), ACTIONS_REQUIRED),
    }
    for label, (got, want) in expected.items():
        if got != want:
            problems.append(f"expected exactly {want} {label}, got {got}")
    for body_name, body in (("text", rendered.body_text), ("markdown", rendered.body_markdown)):
        for heading, want in zip(
            _SECTIONS, (THEMES_REQUIRED, QUOTES_REQUIRED, ACTIONS_REQUIRED), strict=True
        ):
            got = len(_section_lines(body, heading))
            if got != want:
                problems.append(f"{body_name}: '{heading}' has {got} lines, expected {want}")
    return _result("three_three_three", problems, "3 themes, 3 quotes, 3 actions")


def check_quotes_verbatim(content: PulseContent, rendered: RenderedPulse) -> CheckResult:
    problems = []
    for i, quote in enumerate(content.quotes, 1):
        shown = quote_display(quote)  # the verbatim text, ellipses outside the quotation marks
        for body_name, body in (("text", rendered.body_text), ("markdown", rendered.body_markdown)):
            found = body.count(shown)
            if found != 1:
                problems.append(f"quote {i}: appears {found} times verbatim in the {body_name}")
    return _result("quotes_verbatim", problems, "3 quotes rendered verbatim")


def _normalize(token: str) -> str:
    return f"{float(token):g}"


def computed_numbers(content: PulseContent) -> set[str]:
    """Every number the template may print, normalized (``07`` == ``7``, ``2.0`` == ``2``)."""
    sources: list[str] = [
        content.iso_week,
        content.window_start.isoformat(),
        content.window_end.isoformat(),
        str(content.n_analyzed),
        str(content.n_collected),
    ]
    for rank, theme in enumerate(content.themes, 1):
        sources += [str(rank), str(theme.count), pct(theme.share), pct(theme.low_rating_share)]
        if theme.avg_rating is not None:
            sources.append(f"{theme.avg_rating:.1f}")
    for quote in content.quotes:
        sources += [str(quote.rating), quote.created_at.date().isoformat()]
    return {_normalize(n) for source in sources for n in _NUMBER_RE.findall(source)}


def check_numbers(content: PulseContent, rendered: RenderedPulse) -> CheckResult:
    allowed = computed_numbers(content)
    problems = []
    for body_name, body in (("text", rendered.body_text), ("markdown", rendered.body_markdown)):
        for quote in content.quotes:  # quotes are verified verbatim separately
            body = body.replace(quote_display(quote), "")
        invented = sorted({n for n in _NUMBER_RE.findall(body) if _normalize(n) not in allowed})
        if invented:
            listed = ", ".join(_safe_number(n) for n in invented)
            problems.append(f"{body_name} has numbers that were not computed: {listed}")
    return _result("numbers_computed", problems, f"{len(allowed)} computed values")


def _safe_number(token: str) -> str:
    """Echo short numbers; describe long digit runs (a phone or account number) by length."""
    digits = sum(ch.isdigit() for ch in token)
    return token if digits <= _MAX_ECHOED_DIGITS else f"<{digits}-digit number>"


def _pii_hits(parts: Iterable[tuple[str, str]]) -> list[str]:
    hits = []
    for where, text in parts:
        if found := scan_pii(text):
            hits.append(f"{where} matches {', '.join(sorted(found))}")
    return hits


def check_pii(content: PulseContent, rendered: RenderedPulse) -> CheckResult:
    parts: list[tuple[str, str]] = [
        *((f"theme {i} name", t.name) for i, t in enumerate(content.themes, 1)),
        *((f"quote {i}", q.text) for i, q in enumerate(content.quotes, 1)),
        *((f"action {i}", f"{a.action} {a.owner_area}") for i, a in enumerate(content.actions, 1)),
        ("full plain text", rendered.full_text),
        ("full Markdown", rendered.full_markdown),
    ]
    return _result("no_pii", _pii_hits(parts), "no personal data detected")


def check_synthetic(rendered: RenderedPulse, *, allow_synthetic: bool) -> CheckResult:
    problems = []
    if not allow_synthetic:
        for body_name, body in (("text", rendered.full_text), ("markdown", rendered.full_markdown)):
            if SYNTHETIC_MARKER_RE.search(body) or SYNTHETIC_BANNER in body:
                problems.append(f"the {body_name} contains synthetic test fixture data")
    detail = "synthetic data explicitly allowed (tests only)" if allow_synthetic else "none"
    return _result("no_synthetic", problems, detail)


def validate_rendered(
    content: PulseContent, rendered: RenderedPulse, *, allow_synthetic: bool = False
) -> list[CheckResult]:
    """Run every check; the pulse is valid only if all results pass."""
    return [
        check_word_limit(rendered),
        check_counts(content, rendered),
        check_quotes_verbatim(content, rendered),
        check_numbers(content, rendered),
        check_pii(content, rendered),
        check_synthetic(rendered, allow_synthetic=allow_synthetic),
    ]
