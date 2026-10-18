# SYNTHETIC TEST FIXTURE — not real review data
"""Phase 2 language policy (lexical rescue), the English lexicon, and quote eligibility.

The evaluation sentences come from ``tests/fixtures/language_corpus.py``: hand-written test
text, never real Groww reviews.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groww_pulse.analysis.quotes import select_quotes, verify_quotes
from groww_pulse.config import Settings, load_settings
from groww_pulse.models import Review, Theme
from groww_pulse.preprocessing.language import (
    HINGLISH_MARKERS,
    METHOD_DETECTOR,
    METHOD_LEXICAL_RESCUE,
    RESCUE_MIN_CONFIDENCE,
    detect_english,
    english_confidence_ok,
    lexical_rescue_ok,
)
from groww_pulse.preprocessing.lexicon import (
    ENGLISH_WORDS,
    is_english_word,
    is_neutral_token,
    lexical_coverage,
)
from groww_pulse.preprocessing.pii import redact, scan_pii
from groww_pulse.preprocessing.pipeline import (
    load_analysis_reviews,
    review_problems,
    run_preprocessing,
)
from groww_pulse.storage.jsonl_store import JsonlReviewStore
from tests.fixtures.language_corpus import (
    BORDERLINE_ENGLISH,
    FINANCE_ENGLISH,
    GENERAL_ENGLISH,
    HINGLISH,
    HINGLISH_LOW_MARKER,
    OTHER_LANGUAGES,
)
from tests.fixtures.preprocessing import raw_review, write_raw_store
from tests.fixtures.synthetic import AS_OF

THRESHOLD = 0.80
#: Finance English that scores below 0.80 on the detector alone (measured 2026-10-14).
BELOW_THRESHOLD_ENGLISH = [
    "Nice app for SIP and stocks, the charts are clean and fast to load",
    "Best app for beginners to start investing in stocks and mutual funds",
    "Excellent app, smooth UI, easy KYC, fast IPO application and zero AMC",
]


# ---------------------------------------------------------------- corpus outcomes
@pytest.mark.parametrize("text", FINANCE_ENGLISH + GENERAL_ENGLISH)
def test_english_corpus_is_kept(text: str) -> None:
    decision = detect_english(text, THRESHOLD)
    assert decision.keep, decision
    assert decision.lang == "en"


@pytest.mark.parametrize("text", HINGLISH + HINGLISH_LOW_MARKER)
def test_hinglish_corpus_is_excluded(text: str) -> None:
    decision = detect_english(text, THRESHOLD)
    assert not decision.keep, decision
    assert decision.reason in {"hinglish", "not_english", "low_confidence"}


@pytest.mark.parametrize("text", OTHER_LANGUAGES)
def test_other_languages_are_excluded(text: str) -> None:
    decision = detect_english(text, THRESHOLD)
    assert (decision.keep, decision.reason) == (False, "not_english")


@pytest.mark.parametrize("text", BORDERLINE_ENGLISH)
def test_very_short_text_is_not_rescued(text: str) -> None:
    # Too few ordinary words for the lexical signal; only the detector can keep these.
    assert detect_english(text, THRESHOLD).method != METHOD_LEXICAL_RESCUE


def test_lowering_the_threshold_would_admit_hinglish_but_the_rescue_does_not() -> None:
    """Why the threshold was not simply lowered (Phase 2 evaluation)."""
    hinglish = HINGLISH + HINGLISH_LOW_MARKER
    english = FINANCE_ENGLISH + GENERAL_ENGLISH
    # Lowered threshold: all English kept, but low-marker Hinglish leaks through.
    leaked = [t for t in hinglish if detect_english(t, 0.5, lexical_rescue=False).keep]
    assert leaked == ["Mast trading experience, ho gaya profit, shukriya Groww team members"]
    # Default threshold without the rescue: no Hinglish, but finance English is lost.
    lost = [t for t in english if not detect_english(t, THRESHOLD, lexical_rescue=False).keep]
    assert lost == BELOW_THRESHOLD_ENGLISH
    # Default threshold with the rescue: every English sentence, no Hinglish.
    assert all(detect_english(t, THRESHOLD).keep for t in english)
    assert not any(detect_english(t, THRESHOLD).keep for t in hinglish)


# ---------------------------------------------------------------- lexical rescue
@pytest.mark.parametrize("text", BELOW_THRESHOLD_ENGLISH)
def test_finance_english_below_threshold_is_rescued(text: str) -> None:
    decision = detect_english(text, THRESHOLD)
    assert decision.keep
    assert decision.method == METHOD_LEXICAL_RESCUE
    assert RESCUE_MIN_CONFIDENCE <= decision.confidence < THRESHOLD
    # Disabled rescue restores the plain threshold behaviour.
    off = detect_english(text, THRESHOLD, lexical_rescue=False)
    assert (off.keep, off.reason) == (False, "low_confidence")


def test_confident_english_uses_the_detector() -> None:
    decision = detect_english(GENERAL_ENGLISH[0], THRESHOLD)
    assert decision.method == METHOD_DETECTOR
    assert decision.confidence >= THRESHOLD


def test_rescue_needs_the_confidence_floor() -> None:
    text = BELOW_THRESHOLD_ENGLISH[0]
    assert lexical_rescue_ok(text, RESCUE_MIN_CONFIDENCE)
    assert not lexical_rescue_ok(text, RESCUE_MIN_CONFIDENCE - 0.01)


def test_rescue_needs_enough_ordinary_words() -> None:
    # Acronyms and numbers are neutral, so this has only 3 counted words.
    assert not lexical_rescue_ok("SIP IPO KYC ETF NAV 2024 good nice app fast", 0.79)


def test_rescue_refuses_low_coverage_code_mixed_text() -> None:
    # One Hindi marker (below the hinglish rule) but mostly non-English words.
    text = "Mast trading experience, ho gaya profit, shukriya Groww team members"
    assert lexical_coverage(text).share < 0.85
    assert not lexical_rescue_ok(text, 0.79)


def test_rescue_refuses_two_marker_text_even_with_high_confidence_argument() -> None:
    assert not lexical_rescue_ok("the app is good but withdrawal mein bahut time lagta", 0.79)


def test_single_loanword_does_not_block_rescue() -> None:
    text = "Very nice app bhai, keep it up Groww team, best for investment"
    decision = detect_english(text, THRESHOLD)
    assert decision.keep
    assert decision.method == METHOD_LEXICAL_RESCUE


def test_rescue_is_decided_on_redacted_text() -> None:
    # The pipeline sees raw text, the gate sees redacted text: both must agree.
    raw = "Best app for beginners, mail me at someone@example.com about stocks and mutual funds"
    redacted = redact(raw).text
    assert redacted != raw
    for confidence in (0.55, 0.79):
        assert lexical_rescue_ok(raw, confidence) == lexical_rescue_ok(redacted, confidence)


def test_gate_accepts_rescued_and_rejects_unsupported_low_confidence() -> None:
    text = BELOW_THRESHOLD_ENGLISH[0]
    assert english_confidence_ok(text, 0.7888, THRESHOLD)
    assert not english_confidence_ok(text, 0.7888, THRESHOLD, lexical_rescue=False)
    assert not english_confidence_ok("bahut accha app hai lekin slow", 0.7, THRESHOLD)


# ---------------------------------------------------------------- lexicon
@pytest.mark.parametrize(
    "word",
    ["charges", "loading", "deducted", "stopped", "easily", "don't", "Users", "verification"],
)
def test_inflected_english_words_are_known(word: str) -> None:
    assert is_english_word(word)


@pytest.mark.parametrize("word", ["hai", "nahi", "bahut", "paisa", "lekin", "sangat", "muito"])
def test_non_english_words_are_unknown(word: str) -> None:
    assert not is_english_word(word)


@pytest.mark.parametrize(
    "token", ["SIP", "KYC", "F&O", "IPOs", "2024", "[EMAIL]", "groww", "demat"]
)
def test_domain_terms_numbers_and_placeholders_are_neutral(token: str) -> None:
    assert is_neutral_token(token)


def test_hinglish_markers_are_never_english_words() -> None:
    assert not (HINGLISH_MARKERS & ENGLISH_WORDS)
    assert not any(is_english_word(m) for m in HINGLISH_MARKERS)


def test_coverage_counts() -> None:
    coverage = lexical_coverage("SIP ka paisa kat gaya [UPI_ID] 500 but units are fine")
    assert (coverage.neutral, coverage.english) == (3, 4)
    assert coverage.other == 4
    assert lexical_coverage("").share == 0.0


# ---------------------------------------------------------------- pipeline integration
@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(env_file=None, data_dir=tmp_path / "data", log_level="WARNING")


def _run(settings: Settings) -> dict[str, object]:
    report = run_preprocessing(settings, as_of=AS_OF, run_id="lang-run", allow_synthetic=True)
    return report.as_dict()


def test_pipeline_keeps_rescued_finance_english_and_gate_agrees(settings: Settings) -> None:
    # prefix=False: the fixture marker would change the detector's score for this text.
    write_raw_store(
        settings.data_dir,
        [raw_review(f"rescue-{i}", t, prefix=False) for i, t in enumerate(BELOW_THRESHOLD_ENGLISH)],
    )
    funnel = _run(settings)
    assert funnel["output"] == 3
    assert funnel["lang_lexical_rescues"] == 3
    reviews = load_analysis_reviews(
        settings.data_dir / "reviews.jsonl", settings, allow_synthetic=True
    )
    assert all(r.lang_confidence is not None and r.lang_confidence < THRESHOLD for r in reviews)


def test_pipeline_without_rescue_drops_them(tmp_path: Path) -> None:
    settings = load_settings(
        env_file=None, data_dir=tmp_path / "data", log_level="WARNING", lang_lexical_rescue=False
    )
    write_raw_store(
        settings.data_dir,
        [raw_review(f"r-{i}", t, prefix=False) for i, t in enumerate(BELOW_THRESHOLD_ENGLISH)],
    )
    funnel = _run(settings)
    assert funnel["output"] == 0
    language = next(s for s in funnel["steps"] if s["step"] == "language")  # type: ignore[attr-defined]
    assert language["reasons"] == {"low_confidence": 3}


def test_gate_rejects_rescued_review_when_rescue_is_disabled(settings: Settings) -> None:
    write_raw_store(
        settings.data_dir, [raw_review("rescue-0", BELOW_THRESHOLD_ENGLISH[0], prefix=False)]
    )
    _run(settings)
    review = Review.model_validate_json(
        (settings.data_dir / "reviews.jsonl").read_text("utf-8").splitlines()[0]
    )

    def problems(rescue: bool) -> list[str]:
        return review_problems(
            review,
            min_words_exclusive=8,
            emoji_policy=settings.emoji_policy,
            lang_min_confidence=THRESHOLD,
            lang_lexical_rescue=rescue,
            allow_synthetic=True,
        )

    assert problems(rescue=True) == []
    assert problems(rescue=False) == ["not English above the confidence threshold"]


# ---------------------------------------------------------------- quote eligibility
PII_REVIEWS = [
    ("q-name", "my name is rahul sharma and my withdrawal has been pending for six days now", 1),
    ("q-phone", "support asked me to call 9876543210 but my withdrawal is still pending today", 1),
    ("q-addr", "KYC rejected because my address is 12 MG Road, nobody explains the reason", 2),
    (
        "q-clean-1",
        "withdrawal to my bank account has been pending for a whole week without update",
        1,
    ),
    ("q-clean-2", "orders get rejected at market open and the error message explains nothing", 2),
    ("q-clean-3", "the charts load slowly during volatile sessions and the app freezes often", 2),
]
ORIGINAL_PII = ["rahul", "sharma", "9876543210", "12 MG Road"]


def _themes(reviews: list[Review]) -> list[Theme]:
    groups = [["q-name", "q-phone", "q-clean-1"], ["q-addr", "q-clean-2"], ["q-clean-3"]]
    by_id = {r.review_id: r for r in reviews}
    return [
        Theme(
            theme_id=f"t{i}",
            name=f"Theme {i}",
            description="SYNTHETIC theme",
            review_ids=ids,
            count=len(ids),
            share=len(ids) / len(reviews),
            avg_rating=sum(by_id[r].rating for r in ids) / len(ids),
            rank=i,
        )
        for i, ids in enumerate(groups, 1)
    ]


def test_quotes_from_redacted_reviews_never_expose_pii(settings: Settings) -> None:
    write_raw_store(settings.data_dir, [raw_review(i, t, rating=r) for i, t, r in PII_REVIEWS])
    _run(settings)
    reviews = load_analysis_reviews(
        settings.data_dir / "reviews.jsonl", settings, allow_synthetic=True
    )
    by_id = {r.review_id: r for r in reviews}
    assert {
        k for k in ("NAME", "PHONE", "ADDRESS") if any(k in r.pii_redactions for r in reviews)
    } == {
        "NAME",
        "PHONE",
        "ADDRESS",
    }
    themes = _themes(reviews)
    quotes = select_quotes(themes, reviews)
    assignments = {rid: t.theme_id for t in themes for rid in t.review_ids}
    raw_by_id = JsonlReviewStore(settings.data_dir / "raw" / "reviews_raw.jsonl").load()
    verify_quotes(quotes, by_id, assignments, raw_by_id)  # verbatim + provenance hold

    for quote in quotes:
        assert quote.text == by_id[quote.review_id].text[quote.start : quote.end]
        assert scan_pii(quote.text) == {}
        assert not any(value.lower() in quote.text.lower() for value in ORIGINAL_PII)
    # Clean reviews are preferred over redacted ones within a theme.
    assert quotes[0].review_id == "q-clean-1"
    assert quotes[1].review_id == "q-clean-2"


def test_redacted_review_is_still_quote_eligible_verbatim(settings: Settings) -> None:
    write_raw_store(settings.data_dir, [raw_review(i, t, rating=r) for i, t, r in PII_REVIEWS])
    _run(settings)
    reviews = load_analysis_reviews(
        settings.data_dir / "reviews.jsonl", settings, allow_synthetic=True
    )
    redacted = next(r for r in reviews if r.review_id == "q-phone")
    assert redacted.quote_eligible
    assert "[PHONE]" in redacted.text
    assert "9876543210" not in redacted.text
