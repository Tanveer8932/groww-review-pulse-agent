# SYNTHETIC TEST FIXTURE — not real review data
"""Quote holds for possibly truncated or malformed review text."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from groww_pulse.config import Settings, load_settings
from groww_pulse.errors import PreprocessingError
from groww_pulse.models import Review
from groww_pulse.preprocessing.integrity import (
    MANUAL_HOLDS_FILE,
    PLAY_REVIEW_MAX_CHARS,
    POSSIBLE_TRUNCATION,
    TRUNCATION_MARGIN_CHARS,
    UNBALANCED_QUOTES,
    has_unbalanced_quotes,
    load_manual_holds,
    looks_truncated,
    quote_hold_reasons,
)
from groww_pulse.preprocessing.pipeline import (
    PreprocessReport,
    load_analysis_reviews,
    review_problems,
    run_preprocessing,
    verify_provenance,
)
from groww_pulse.storage.jsonl_store import JsonlReviewStore
from tests.fixtures.preprocessing import PREFIX, raw_review, write_raw_store
from tests.fixtures.synthetic import AS_OF

SENTENCE = "The withdrawal took five working days and support never replied to my ticket. "
LIMIT = PLAY_REVIEW_MAX_CHARS - TRUNCATION_MARGIN_CHARS


def long_text(length: int) -> str:
    """English text of exactly ``length`` characters (cut wherever that falls)."""
    return (SENTENCE * (length // len(SENTENCE) + 1))[:length]


# ---------------------------------------------------------------- truncation
@pytest.mark.parametrize("length", [LIMIT, PLAY_REVIEW_MAX_CHARS])
def test_text_cut_mid_word_near_the_limit_is_truncated(length: int) -> None:
    text = long_text(length - 1) + "x"
    assert looks_truncated(text)
    assert quote_hold_reasons(text) == [POSSIBLE_TRUNCATION]


def test_text_just_below_the_margin_is_not_judged() -> None:
    assert not looks_truncated(long_text(LIMIT - 2) + "x")


@pytest.mark.parametrize("ending", [".", "!", "?", ")", '"', "”", "’"])
def test_closing_punctuation_near_the_limit_is_complete(ending: str) -> None:
    assert not looks_truncated(long_text(PLAY_REVIEW_MAX_CHARS - 1) + ending)


@pytest.mark.parametrize("ending", ["...", "…"])
def test_ellipsis_near_the_limit_is_truncated(ending: str) -> None:
    assert looks_truncated(long_text(PLAY_REVIEW_MAX_CHARS - len(ending)) + ending)


def test_trailing_whitespace_is_ignored() -> None:
    assert not looks_truncated(long_text(PLAY_REVIEW_MAX_CHARS - 1) + ".   ")
    assert looks_truncated(long_text(PLAY_REVIEW_MAX_CHARS - 1) + "x \n")


def test_short_text_without_punctuation_is_complete() -> None:
    assert quote_hold_reasons("great app for beginners but charges are high") == []


# ---------------------------------------------------------------- quotes
@pytest.mark.parametrize(
    "text",
    [
        'This app should be banned." Fraud everywhere',
        'They said "wait and then "wait again" forever',
        "The “Transfer option is missing",
        "The Transfer” option is missing",
    ],
)
def test_unbalanced_quotes(text: str) -> None:
    assert has_unbalanced_quotes(text)
    assert quote_hold_reasons(text) == [UNBALANCED_QUOTES]


@pytest.mark.parametrize(
    "text",
    [
        'I chose "limit" but it ran as "market" instead',
        "It is stuck on the “Allow Permissions” screen",
        "It doesn't open and I can't log in, it's broken",
    ],
)
def test_balanced_quotes_and_apostrophes(text: str) -> None:
    assert not has_unbalanced_quotes(text)


def test_reasons_are_ordered_and_combined() -> None:
    text = '"' + long_text(PLAY_REVIEW_MAX_CHARS - 2) + "x"
    assert quote_hold_reasons(text) == [POSSIBLE_TRUNCATION, UNBALANCED_QUOTES]


# ---------------------------------------------------------------- pipeline
@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(env_file=None, data_dir=tmp_path / "data", log_level="WARNING")


def _corpus() -> list[object]:
    body_len = PLAY_REVIEW_MAX_CHARS - len(PREFIX) - 1
    return [
        raw_review("clean", "the app crashes every time I open my portfolio after the update"),
        raw_review("cut", long_text(body_len - 1) + "x"),
        raw_review("quotes", 'support said "wait two days and nothing happened after that'),
    ]


def _run(settings: Settings) -> list[Review]:
    write_raw_store(settings.data_dir, _corpus())  # type: ignore[arg-type]
    report = run_preprocessing(settings, as_of=AS_OF, run_id="test-run", allow_synthetic=True)
    assert report.quote_holds == {POSSIBLE_TRUNCATION: 1, UNBALANCED_QUOTES: 1}
    assert report.quote_ineligible == 2
    assert report.as_dict()["quote_holds"] == report.quote_holds
    return load_analysis_reviews(
        settings.data_dir / "reviews.jsonl", settings, allow_synthetic=True
    )


def test_held_reviews_are_kept_for_theming_but_not_quotable(settings: Settings) -> None:
    by_id = {r.review_id: r for r in _run(settings)}
    assert set(by_id) == {"clean", "cut", "quotes"}
    assert by_id["clean"].quote_eligible
    assert by_id["clean"].quote_hold_reasons == []
    assert not by_id["cut"].quote_eligible
    assert by_id["cut"].quote_hold_reasons == [POSSIBLE_TRUNCATION]
    assert not by_id["quotes"].quote_eligible
    assert by_id["quotes"].quote_hold_reasons == [UNBALANCED_QUOTES]


def test_original_text_is_preserved_for_held_reviews(settings: Settings) -> None:
    _run(settings)
    raw = JsonlReviewStore(settings.data_dir / "raw" / "reviews_raw.jsonl").load()
    original = {r.review_id: r.text for r in _corpus()}  # type: ignore[attr-defined]
    assert {rid: raw[rid].text for rid in original} == original


def test_gate_rejects_a_held_review_marked_eligible(settings: Settings) -> None:
    held = next(r for r in _run(settings) if r.review_id == "cut")
    tampered = held.model_copy(update={"quote_eligible": True})
    problems = review_problems(
        tampered,
        min_words_exclusive=settings.min_words_exclusive,
        emoji_policy=settings.emoji_policy,
        lang_min_confidence=settings.lang_min_confidence,
        allow_synthetic=True,
    )
    assert problems == ["has quote holds but is marked quote-eligible"]


def test_provenance_rederives_holds_from_the_raw_store(settings: Settings) -> None:
    reviews = _run(settings)
    raw = JsonlReviewStore(settings.data_dir / "raw" / "reviews_raw.jsonl").load()
    assert verify_provenance(reviews, raw) == []
    cut = next(r for r in reviews if r.review_id == "cut")
    cleared = cut.model_copy(update={"quote_hold_reasons": [], "quote_eligible": True})
    assert verify_provenance([cleared], raw) == ["cut"]


# ---------------------------------------------------------------- manual holds
def _write_holds(settings: Settings, holds: object) -> Path:
    path = settings.data_dir / MANUAL_HOLDS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(holds), encoding="utf-8")
    return path


def _preprocess(settings: Settings) -> PreprocessReport:
    write_raw_store(settings.data_dir, _corpus())  # type: ignore[arg-type]
    return run_preprocessing(settings, as_of=AS_OF, run_id="test-run", allow_synthetic=True)


def _load(settings: Settings) -> dict[str, Review]:
    path = settings.data_dir / "reviews.jsonl"
    return {r.review_id: r for r in load_analysis_reviews(path, settings, allow_synthetic=True)}


def test_no_registry_means_no_manual_holds(tmp_path: Path) -> None:
    assert load_manual_holds(tmp_path / "quote_holds.json") == {}


def test_manual_hold_makes_a_clean_review_ineligible_but_keeps_it(settings: Settings) -> None:
    _write_holds(
        settings,
        {
            "holds": [
                {
                    "review_id": "clean",
                    "reason": "starts_mid_sentence",
                    "note": "opening words missing",
                    "added": "2026-10-14",
                },
                {"review_id": "clean", "reason": "possible_spliced_text"},
                {"review_id": "cut", "reason": "possible_spliced_text"},
                {"review_id": "not-imported", "reason": "starts_mid_sentence"},
            ]
        },
    )
    report = _preprocess(settings)
    by_id = _load(settings)
    assert set(by_id) == {"clean", "cut", "quotes"}
    assert not by_id["clean"].quote_eligible
    assert by_id["clean"].quote_hold_reasons == [
        "manual:possible_spliced_text",
        "manual:starts_mid_sentence",
    ]
    assert by_id["cut"].quote_hold_reasons == [
        POSSIBLE_TRUNCATION,
        "manual:possible_spliced_text",
    ]
    assert report.quote_ineligible == 3
    assert report.quote_holds == {
        POSSIBLE_TRUNCATION: 1,
        UNBALANCED_QUOTES: 1,
        "manual:possible_spliced_text": 2,
        "manual:starts_mid_sentence": 1,
    }
    assert report.settings["manual_holds"]["review_ids"] == 3
    assert report.settings["manual_holds"]["sha256"]
    assert any("1 manual quote hold(s)" in w for w in report.warnings)
    raw = JsonlReviewStore(settings.data_dir / "raw" / "reviews_raw.jsonl").load()
    assert verify_provenance(list(by_id.values()), raw) == []


def test_manual_hold_never_changes_text(settings: Settings) -> None:
    _preprocess(settings)
    before = (settings.data_dir / "reviews.jsonl").read_text("utf-8")
    _write_holds(settings, {"holds": [{"review_id": "clean", "reason": "unverified_text"}]})
    _preprocess(settings)
    texts = {
        r.review_id: (r.text, r.text_sha256, r.original_sha256) for r in _load(settings).values()
    }
    old = {
        r.review_id: (r.text, r.text_sha256, r.original_sha256)
        for r in (Review.model_validate_json(line) for line in before.splitlines())
    }
    assert texts == old


def test_analysis_load_refuses_a_review_missing_its_manual_hold(settings: Settings) -> None:
    _preprocess(settings)  # no registry yet
    _write_holds(settings, {"holds": [{"review_id": "clean", "reason": "starts_mid_sentence"}]})
    with pytest.raises(PreprocessingError, match="manual quote hold not applied"):
        _load(settings)
    _preprocess(settings)
    assert not _load(settings)["clean"].quote_eligible


def test_removing_a_manual_hold_lifts_it_after_rerun(settings: Settings) -> None:
    path = _write_holds(settings, {"holds": [{"review_id": "clean", "reason": "unverified_text"}]})
    _preprocess(settings)
    assert not _load(settings)["clean"].quote_eligible
    path.write_text('{"holds": []}', encoding="utf-8")
    _preprocess(settings)
    assert _load(settings)["clean"].quote_eligible


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"holds": {}}',
        '{"holds": [], "extra": 1}',
        '{"holds": ["clean"]}',
        '{"holds": [{"review_id": "clean"}]}',
        '{"holds": [{"review_id": "", "reason": "x"}]}',
        '{"holds": [{"review_id": "clean", "reason": "Not Snake"}]}',
        '{"holds": [{"review_id": "clean", "reason": "ok", "text": "quoted words"}]}',
        '{"holds": [{"review_id": "clean", "reason": "ok", "note": 5}]}',
    ],
)
def test_malformed_registry_fails_closed_before_writing(settings: Settings, content: str) -> None:
    path = settings.data_dir / MANUAL_HOLDS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(PreprocessingError, match=r"quote_holds\.json"):
        _preprocess(settings)
    assert not (settings.data_dir / "reviews.jsonl").exists()
