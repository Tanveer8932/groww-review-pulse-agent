"""Preprocessing pipeline: raw store -> filtered, deduplicated, redacted ``reviews.jsonl``.

Steps run in a fixed order (architecture §6.3) and each one records how many reviews it
received, dropped (with a reason), and passed on:

``validate -> window -> length -> emoji -> language -> dedupe -> pii_redaction ->
length_recheck -> output_gate``

The output gate re-checks every record with :func:`review_problems`, the same check that
:func:`load_analysis_reviews` applies when Phase 3 reads the file, so a review with too few
words, non-English text, emoji (strict policy), leftover PII, or a tampered body can never
reach AI analysis.

Synthetic fixtures are refused: if the raw store contains any text carrying the fixture
marker, preprocessing stops instead of producing ``reviews.jsonl`` from it. Only tests pass
``allow_synthetic=True``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from groww_pulse.collection.base import DateWindow
from groww_pulse.config import EmojiPolicy, Settings
from groww_pulse.errors import PreprocessingError
from groww_pulse.models import RawReview, Review
from groww_pulse.preprocessing.dedupe import (
    LENGTH_RATIO_MIN,
    NEAR_DUPLICATE_THRESHOLD,
    DedupeItem,
    deduplicate,
)
from groww_pulse.preprocessing.emoji_policy import apply_emoji_policy, contains_emoji
from groww_pulse.preprocessing.integrity import (
    MANUAL_HOLDS_FILE,
    automatic_hold_reasons,
    load_manual_holds,
    quote_hold_reasons,
)
from groww_pulse.preprocessing.language import (
    METHOD_LEXICAL_RESCUE,
    RESCUE_MIN_CONFIDENCE,
    RESCUE_MIN_COVERAGE,
    RESCUE_MIN_TOKENS,
    detect_english,
    english_confidence_ok,
)
from groww_pulse.preprocessing.normalize import count_words, normalize_text
from groww_pulse.preprocessing.pii import PII_RULES_VERSION, redact, scan_pii
from groww_pulse.storage.jsonl_store import atomic_write_lines

log = logging.getLogger(__name__)

RAW_STORE = Path("raw") / "reviews_raw.jsonl"
OUTPUT_FILE = Path("reviews.jsonl")

#: Marker carried by every synthetic test fixture (see tests/fixtures/synthetic.py).
SYNTHETIC_MARKER_RE = re.compile(r"SYNTHETIC\s+(?:TEST\s+)?FIXTURE", re.IGNORECASE)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redacted_text_for(raw_text: str) -> tuple[str, dict[str, int]]:
    """The exact transformation from raw text to the stored ``Review.text``."""
    result = redact(normalize_text(raw_text))
    return result.text, result.counts


# ---------------------------------------------------------------- stats
@dataclass
class StepStats:
    step: str
    input: int
    dropped: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def output(self) -> int:
        return self.input - self.dropped

    def drop(self, reason: str, n: int = 1) -> None:
        self.dropped += n
        self.reasons[reason] = self.reasons.get(reason, 0) + n

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "input": self.input,
            "dropped": self.dropped,
            "output": self.output,
            "reasons": dict(sorted(self.reasons.items())),
        }


@dataclass
class PreprocessReport:
    run_id: str
    window: DateWindow
    settings: dict[str, Any]
    raw_path: Path
    output_path: Path
    funnel_path: Path | None = None
    raw_lines: int = 0
    steps: list[StepStats] = field(default_factory=list)
    pii_redactions: dict[str, int] = field(default_factory=dict)
    reviews_redacted: int = 0
    quote_ineligible: int = 0
    quote_holds: dict[str, int] = field(default_factory=dict)
    lang_lexical_rescues: int = 0
    output_sha256: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def output_count(self) -> int:
        return self.steps[-1].output if self.steps else 0

    @property
    def empty(self) -> bool:
        return self.output_count == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "window": {"start": self.window.start.isoformat(), "end": self.window.end.isoformat()},
            "settings": self.settings,
            "raw_path": str(self.raw_path),
            "output_path": str(self.output_path),
            "funnel_path": str(self.funnel_path) if self.funnel_path else None,
            "raw_lines": self.raw_lines,
            "input": self.steps[0].input if self.steps else 0,
            "output": self.output_count,
            "total_dropped": sum(s.dropped for s in self.steps),
            "steps": [s.as_dict() for s in self.steps],
            "pii_redactions": dict(sorted(self.pii_redactions.items())),
            "reviews_redacted": self.reviews_redacted,
            "quote_ineligible": self.quote_ineligible,
            "quote_holds": dict(sorted(self.quote_holds.items())),
            "lang_lexical_rescues": self.lang_lexical_rescues,
            "output_sha256": self.output_sha256,
            "empty": self.empty,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------- working record
@dataclass
class _Work:
    raw: RawReview
    text: str  # normalized, not yet redacted
    quote_eligible: bool = True
    lang: str | None = None
    lang_confidence: float = 0.0


def _read_raw(path: Path, stats: StepStats) -> list[RawReview]:
    """Parse the raw store line by line; unreadable lines are counted, never guessed at."""
    records: list[RawReview] = []
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            stats.input += 1
            try:
                records.append(RawReview.model_validate_json(line))
            except ValidationError:
                stats.drop("invalid_record")
    return records


def _validate(records: Iterable[RawReview], app_id: str, stats: StepStats) -> list[_Work]:
    out = []
    for raw in records:
        if sha256_text(raw.text) != raw.original_sha256:
            stats.drop("hash_mismatch")
        elif raw.app_id != app_id:
            stats.drop("wrong_app_id")
        else:
            text = normalize_text(raw.text)
            if text:
                out.append(_Work(raw=raw, text=text))
            else:
                stats.drop("empty_text")
    return out


def _refuse_synthetic(items: Iterable[_Work]) -> None:
    synthetic = sum(1 for w in items if SYNTHETIC_MARKER_RE.search(w.raw.text))
    if synthetic:
        raise PreprocessingError(
            f"The raw store contains {synthetic} synthetic test fixture record(s). Synthetic "
            "data must never be used as production review data: remove the raw store and "
            "import a real, permitted export."
        )


# ---------------------------------------------------------------- output gate
def review_problems(
    review: Review,
    *,
    min_words_exclusive: int,
    emoji_policy: EmojiPolicy,
    lang_min_confidence: float,
    lang_lexical_rescue: bool = True,
    allow_synthetic: bool = False,
) -> list[str]:
    """Every reason ``review`` is unfit for analysis (empty list means fit)."""
    problems = []
    words = count_words(review.text)
    if words != review.word_count:
        problems.append("word_count does not match text")
    if words <= min_words_exclusive:
        problems.append(f"has {words} words; more than {min_words_exclusive} required")
    if review.text != normalize_text(review.text):
        problems.append("text is not normalized")
    if review.lang != "en" or not english_confidence_ok(
        review.text,
        review.lang_confidence or 0.0,
        lang_min_confidence,
        lexical_rescue=lang_lexical_rescue,
    ):
        problems.append("not English above the confidence threshold")
    has_emoji = contains_emoji(review.text)
    if has_emoji and emoji_policy is EmojiPolicy.STRICT:
        problems.append("contains emoji under the strict policy")
    if has_emoji and review.quote_eligible:
        problems.append("contains emoji but is marked quote-eligible")
    if review.quote_hold_reasons and review.quote_eligible:
        problems.append("has quote holds but is marked quote-eligible")
    if leftover := scan_pii(review.text):
        problems.append(f"unredacted PII: {', '.join(sorted(leftover))}")
    if review.text_sha256 != sha256_text(review.text):
        problems.append("text_sha256 missing or does not match text")
    if not allow_synthetic and SYNTHETIC_MARKER_RE.search(review.text):
        problems.append("synthetic test fixture")
    return problems


def _gate_kwargs(settings: Settings) -> dict[str, Any]:
    return {
        "min_words_exclusive": settings.min_words_exclusive,
        "emoji_policy": settings.emoji_policy,
        "lang_min_confidence": settings.lang_min_confidence,
        "lang_lexical_rescue": settings.lang_lexical_rescue,
    }


def load_analysis_reviews(
    path: Path, settings: Settings, *, allow_synthetic: bool = False
) -> list[Review]:
    """Load ``reviews.jsonl`` for analysis, failing closed on any unfit record.

    Raises:
        PreprocessingError: if the file is missing, a line is not a valid ``Review``, an ID
            repeats, any record fails :func:`review_problems`, or a review named in the
            manual hold registry lacks that hold. Messages carry review IDs and line
            numbers only, never review text.
    """
    if not path.exists():
        raise PreprocessingError(f"{path} does not exist; run `groww-pulse preprocess` first")
    manual_holds = load_manual_holds(settings.data_dir / MANUAL_HOLDS_FILE)
    reviews: list[Review] = []
    problems: list[str] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                review = Review.model_validate_json(line)
            except ValidationError as exc:
                problems.append(f"line {line_no}: invalid record ({exc.error_count()} errors)")
                continue
            if review.review_id in seen:
                problems.append(f"line {line_no}: duplicate review_id")
            seen.add(review.review_id)
            for problem in review_problems(
                review, allow_synthetic=allow_synthetic, **_gate_kwargs(settings)
            ):
                problems.append(f"line {line_no} ({review.review_id}): {problem}")
            required = set(manual_holds.get(review.review_id, ()))
            if missing := required - set(review.quote_hold_reasons):
                problems.append(
                    f"line {line_no} ({review.review_id}): manual quote hold not applied "
                    f"({', '.join(sorted(missing))}); re-run `groww-pulse preprocess`"
                )
            reviews.append(review)
    if problems:
        shown = "; ".join(problems[:5])
        more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        raise PreprocessingError(f"{path.name} is not fit for analysis: {shown}{more}")
    return reviews


def verify_provenance(reviews: Iterable[Review], raw_by_id: Mapping[str, RawReview]) -> list[str]:
    """IDs of reviews whose text cannot be re-derived from the raw store (empty means OK).

    A review is verified when its raw record exists, the raw text hashes to
    ``original_sha256``, normalizing and redacting the raw text reproduces ``text``, and
    the raw text yields the same automatic ``quote_hold_reasons`` (manual holds are checked
    against the registry by :func:`load_analysis_reviews`).
    """
    failures = []
    for review in reviews:
        raw = raw_by_id.get(review.review_id)
        if (
            raw is None
            or raw.original_sha256 != review.original_sha256
            or sha256_text(raw.text) != review.original_sha256
            or redacted_text_for(raw.text)[0] != review.text
            or quote_hold_reasons(raw.text) != automatic_hold_reasons(review.quote_hold_reasons)
        ):
            failures.append(review.review_id)
    return failures


# ---------------------------------------------------------------- pipeline
def _serialize(review: Review) -> str:
    return json.dumps(
        review.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def run_preprocessing(
    settings: Settings,
    *,
    as_of: date | None = None,
    weeks: int | None = None,
    run_id: str,
    allow_synthetic: bool = False,
) -> PreprocessReport:
    """Run every step, write ``reviews.jsonl`` and ``runs/{run_id}/funnel.json``.

    An empty or missing raw store yields an empty ``reviews.jsonl`` and a funnel of zeros;
    nothing is ever invented to fill it.
    """
    as_of = as_of or datetime.now(UTC).date()
    window = DateWindow.ending(as_of, weeks or settings.analysis_window_weeks)
    data_dir = settings.data_dir
    report = PreprocessReport(
        run_id=run_id,
        window=window,
        settings={
            "as_of": as_of.isoformat(),
            "window_weeks": weeks or settings.analysis_window_weeks,
            "min_words_exclusive": settings.min_words_exclusive,
            "emoji_policy": settings.emoji_policy.value,
            "lang_min_confidence": settings.lang_min_confidence,
            "lang_rescue": {
                "enabled": settings.lang_lexical_rescue,
                "min_confidence": RESCUE_MIN_CONFIDENCE,
                "min_coverage": RESCUE_MIN_COVERAGE,
                "min_tokens": RESCUE_MIN_TOKENS,
            },
            "pii_rules_version": PII_RULES_VERSION,
            "near_duplicate_threshold": NEAR_DUPLICATE_THRESHOLD,
            "near_duplicate_length_ratio_min": LENGTH_RATIO_MIN,
            "app_id": settings.app_id,
        },
        raw_path=data_dir / RAW_STORE,
        output_path=data_dir / OUTPUT_FILE,
    )

    # Read the manual hold registry first: a malformed file fails before anything is written.
    manual_holds_path = data_dir / MANUAL_HOLDS_FILE
    manual_holds = load_manual_holds(manual_holds_path)
    report.settings["manual_holds"] = {
        "path": str(manual_holds_path),
        "review_ids": len(manual_holds),
        "sha256": (
            hashlib.sha256(manual_holds_path.read_bytes()).hexdigest()
            if manual_holds_path.exists()
            else None
        ),
    }

    # validate
    validate = StepStats("validate", 0)
    report.steps.append(validate)
    if not report.raw_path.exists():
        report.warnings.append("raw store not found; run `groww-pulse import` first")
    raw_records = _read_raw(report.raw_path, validate)
    report.raw_lines = validate.input
    work = _validate(raw_records, settings.app_id, validate)
    if not allow_synthetic:
        _refuse_synthetic(work)

    # window
    step = StepStats("window", len(work))
    report.steps.append(step)
    kept = []
    for w in work:
        if window.contains(w.raw.created_at):
            kept.append(w)
        else:
            step.drop("outside_window")
    work = kept

    # length
    step = StepStats("length", len(work))
    report.steps.append(step)
    kept = []
    for w in work:
        if count_words(w.text) > settings.min_words_exclusive:
            kept.append(w)
        else:
            step.drop("too_few_words")
    work = kept

    # emoji
    step = StepStats("emoji", len(work))
    report.steps.append(step)
    kept = []
    for w in work:
        emoji_decision = apply_emoji_policy(w.text, settings.emoji_policy)
        if emoji_decision.keep:
            w.quote_eligible = emoji_decision.quote_eligible
            kept.append(w)
        else:
            step.drop(emoji_decision.reason or "contains_emoji")
    work = kept

    # language
    step = StepStats("language", len(work))
    report.steps.append(step)
    kept = []
    for w in work:
        lang_decision = detect_english(
            w.text, settings.lang_min_confidence, lexical_rescue=settings.lang_lexical_rescue
        )
        if lang_decision.keep:
            w.lang, w.lang_confidence = lang_decision.lang, lang_decision.confidence
            report.lang_lexical_rescues += lang_decision.method == METHOD_LEXICAL_RESCUE
            kept.append(w)
        else:
            step.drop(lang_decision.reason or "not_english")
    work = kept

    # dedupe
    step = StepStats("dedupe", len(work))
    report.steps.append(step)
    result = deduplicate(
        [
            DedupeItem(
                review_id=w.raw.review_id,
                sort_key=(w.raw.created_at,),
                rating=w.raw.rating,
                text=w.text,
                payload=w,
            )
            for w in work
        ]
    )
    for reason, n in result.reason_counts().items():
        step.drop(reason, n)
    work = [item.payload for item in result.kept]

    # pii_redaction (never drops; counts per type, never values)
    step = StepStats("pii_redaction", len(work))
    report.steps.append(step)
    redacted: list[tuple[_Work, str, dict[str, int]]] = []
    totals: Counter[str] = Counter()
    for w in work:
        text, counts = redacted_text_for(w.raw.text)
        totals.update(counts)
        report.reviews_redacted += bool(counts)
        redacted.append((w, text, counts))
    report.pii_redactions = dict(totals)

    # length_recheck
    step = StepStats("length_recheck", len(redacted))
    report.steps.append(step)
    reviews: list[Review] = []
    holds: Counter[str] = Counter()
    for w, text, counts in redacted:
        words = count_words(text)
        if words <= settings.min_words_exclusive:
            step.drop("too_few_words_after_redaction")
            continue
        raw = w.raw
        hold_reasons = quote_hold_reasons(raw.text) + manual_holds.get(raw.review_id, [])
        holds.update(hold_reasons)
        reviews.append(
            Review(
                review_id=raw.review_id,
                source=raw.source,
                app_id=raw.app_id,
                rating=raw.rating,
                text=text,
                original_sha256=raw.original_sha256,
                text_sha256=sha256_text(text),
                created_at=raw.created_at,
                app_version=raw.app_version,
                thumbs_up=raw.thumbs_up,
                lang=w.lang,
                lang_confidence=w.lang_confidence,
                word_count=words,
                quote_eligible=w.quote_eligible and not hold_reasons,
                quote_hold_reasons=hold_reasons,
                pii_redactions=counts,
                provenance=raw.provenance,
            )
        )

    # output_gate: must never drop; a failure here is a bug, so fail closed.
    step = StepStats("output_gate", len(reviews))
    report.steps.append(step)
    for review in reviews:
        problems = review_problems(
            review, allow_synthetic=allow_synthetic, **_gate_kwargs(settings)
        )
        if problems:
            raise PreprocessingError(
                f"Output gate rejected review {review.review_id}: {'; '.join(problems)}"
            )
    report.quote_ineligible = sum(not r.quote_eligible for r in reviews)
    report.quote_holds = dict(holds)
    if unmatched := len(set(manual_holds) - {r.review_id for r in reviews}):
        report.warnings.append(
            f"{unmatched} manual quote hold(s) name reviews not in reviews.jsonl (dropped or "
            "not imported); they have no effect"
        )

    reviews.sort(key=lambda r: (r.created_at, r.review_id))
    lines = [_serialize(r) for r in reviews]
    atomic_write_lines(report.output_path, lines)
    report.output_sha256 = hashlib.sha256(report.output_path.read_bytes()).hexdigest()
    if report.empty:
        report.warnings.append("no reviews passed preprocessing; reviews.jsonl is empty")

    report.funnel_path = data_dir / "runs" / run_id / "funnel.json"
    atomic_write_lines(report.funnel_path, [json.dumps(report.as_dict(), indent=2)])
    log.info(
        "Preprocessing finished",
        extra={"input": report.steps[0].input, "output": report.output_count},
    )
    return report
