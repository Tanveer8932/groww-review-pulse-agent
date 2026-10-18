# SYNTHETIC TEST FIXTURE — not real review data
"""Word counting, normalization, emoji policy, and language detection."""

from __future__ import annotations

import pytest

from groww_pulse.config import EmojiPolicy
from groww_pulse.preprocessing.emoji_policy import (
    apply_emoji_policy,
    contains_emoji,
    is_emoji_only,
)
from groww_pulse.preprocessing.language import (
    detect_english,
    has_non_latin_letters,
    hinglish_marker_count,
)
from groww_pulse.preprocessing.normalize import count_words, dedupe_key, normalize_text

# ---------------------------------------------------------------- word count


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("   ", 0),
        ("one two three four five six seven eight", 8),
        ("one two three four five six seven eight nine", 9),
        ("well-known app", 2),  # hyphenated word is one word
        ("don't won’t can't", 3),  # straight and curly apostrophes
        ("!!! ... :) -- ?", 0),  # bare punctuation is not a word
        ("great,app.fast!ok", 4),  # punctuation separates words
        ("Rs 10,000 lost", 4),  # "10" and "000" are separate tokens
        ("[EMAIL] is bad", 3),  # placeholders count as one word
        ("snake_case word", 2),
    ],
)
def test_count_words(text: str, expected: int) -> None:
    assert count_words(text) == expected


def test_eight_words_versus_nine() -> None:
    eight = "the app froze when I opened my portfolio"
    nine = eight + " today"
    assert count_words(eight) == 8
    assert count_words(nine) == 9


def test_normalize_text_only_touches_whitespace_and_invisibles() -> None:
    raw = "  Good​ app\n\n\tbut slow\x07 now﻿  "
    assert normalize_text(raw) == "Good app but slow now"


def test_normalize_text_nfc_and_keeps_case_and_punctuation() -> None:
    decomposed = "Café is OK!!"
    assert normalize_text(decomposed) == "Café is OK!!"


def test_normalize_keeps_zero_width_joiner_for_emoji_sequences() -> None:
    family = "\U0001f468‍\U0001f469‍\U0001f467"
    assert normalize_text(f"family {family}") == f"family {family}"


def test_dedupe_key_is_case_and_whitespace_insensitive() -> None:
    assert dedupe_key("App  IS\nslow") == dedupe_key("app is slow")


# ---------------------------------------------------------------- emoji

EMOJI_CASES = {
    "emoji_only": "\U0001f44d\U0001f44d\U0001f525",
    "single_inside_text": "Great app for investing \U0001f44d works well every day",
    "zwj_family": "My family \U0001f468‍\U0001f469‍\U0001f467 uses this app daily",
    "skin_tone": "Thumbs up \U0001f44d\U0001f3fd for the new charts screen today",
    "flag": "Best app in \U0001f1ee\U0001f1f3 for mutual funds and stocks",
    "heart_text_presentation": "I ❤ this app for SIP investing every month",
}


@pytest.mark.parametrize("name", sorted(EMOJI_CASES))
def test_emoji_cases_detected(name: str) -> None:
    assert contains_emoji(EMOJI_CASES[name])


@pytest.mark.parametrize("name", sorted(EMOJI_CASES))
def test_strict_policy_drops_every_emoji_case(name: str) -> None:
    decision = apply_emoji_policy(EMOJI_CASES[name], EmojiPolicy.STRICT)
    assert not decision.keep
    assert decision.reason == "contains_emoji"


@pytest.mark.parametrize("name", sorted(set(EMOJI_CASES) - {"emoji_only"}))
def test_lenient_policy_keeps_text_with_emoji_but_not_quotable(name: str) -> None:
    decision = apply_emoji_policy(EMOJI_CASES[name], EmojiPolicy.LENIENT)
    assert decision.keep
    assert not decision.quote_eligible


def test_lenient_policy_drops_emoji_only() -> None:
    assert is_emoji_only(EMOJI_CASES["emoji_only"])
    decision = apply_emoji_policy(EMOJI_CASES["emoji_only"], EmojiPolicy.LENIENT)
    assert (decision.keep, decision.reason) == (False, "emoji_only")


@pytest.mark.parametrize(
    "text",
    [
        "Nice app :) but the login takes long :( sometimes",
        "Rated ★★★★ because charts are fine → but slow",
        "Charges ₹20 per order ✓ which is fair enough",
        "Plain text review with no symbols at all here",
    ],
)
@pytest.mark.parametrize("policy", list(EmojiPolicy))
def test_text_symbols_are_not_emoji(text: str, policy: EmojiPolicy) -> None:
    assert not contains_emoji(text)
    decision = apply_emoji_policy(text, policy)
    assert decision.keep
    assert decision.quote_eligible


def test_emoji_policy_never_modifies_text() -> None:
    text = EMOJI_CASES["single_inside_text"]
    before = str(text)
    apply_emoji_policy(text, EmojiPolicy.LENIENT)
    assert text == before


# ---------------------------------------------------------------- language

ENGLISH = [
    "The app keeps crashing whenever I try to place an order in the morning",
    "Worst app ever. Money deducted but order not placed, support is useless",
    "Login OTP not coming since 3 days, please resolve asap my account is blocked",
    "Very good app for beginners, easy to use and a clean interface for mutual funds",
]


@pytest.mark.parametrize("text", ENGLISH)
def test_english_is_kept(text: str) -> None:
    decision = detect_english(text, 0.80)
    assert decision.keep, decision
    assert decision.lang == "en"
    assert decision.confidence >= 0.80


@pytest.mark.parametrize(
    "text",
    [
        "यह ऐप बहुत अच्छा है लेकिन निकासी में बहुत समय लगता है",
        "இந்த செயலி மிகவும் நன்றாக உள்ளது ஆனால் மெதுவாக உள்ளது",
        # mixed script: English with a Devanagari word is not English-only
        "The app is good but withdrawal takes too long बहुत slow",
    ],
)
def test_non_latin_script_is_excluded(text: str) -> None:
    assert has_non_latin_letters(text)
    assert detect_english(text, 0.80).reason == "non_latin_script"


@pytest.mark.parametrize(
    "text",
    [
        "Yeh app bahut accha hai lekin withdrawal mein bahut time lagta hai yaar",
        "App bahut slow hai and customer care koi response nahi deta hai",
        "Bahut badiya app hai mujhe bahut pasand aaya sab log use karo",
        "Paise kat gaye but order place nahi hua please help karo",
    ],
)
def test_hinglish_is_excluded(text: str) -> None:
    assert hinglish_marker_count(text) >= 2
    decision = detect_english(text, 0.80)
    assert not decision.keep
    assert decision.reason == "hinglish"


@pytest.mark.parametrize(
    ("text", "markers"),
    [
        ("Brokerage is lower in 5paisa and 5paisa also has a VTT order type", 0),
        ("5paisa..10rs per order, unlike 5paisa...which is much cheaper overall", 0),
        ("Compared with 5 paisa the brokerage here is higher for every single order", 1),
        ("Paisa2 is not a word; nahi2 neither, so markers need whole tokens here", 0),
    ],
)
def test_markers_inside_alphanumeric_tokens_do_not_count(text: str, markers: int) -> None:
    assert hinglish_marker_count(text) == markers
    assert detect_english(text, 0.80).reason != "hinglish"


@pytest.mark.parametrize(
    "text",
    [
        "Mera paisa 2 din se atka hai, refund kab milega",
        "Withdrawal ka paisa nahi aaya, 3 baar call kiya",
        "Bahut slow hai app... paisa-wasool nahi hai bilkul",
        "kya bakwas app hai!!! 500rs ka loss hua",
    ],
)
def test_hinglish_next_to_digits_and_punctuation_is_still_excluded(text: str) -> None:
    assert hinglish_marker_count(text) >= 2
    assert detect_english(text, 0.80).reason == "hinglish"


def test_single_loanword_is_not_hinglish() -> None:
    text = "Good app but the KYC process is very slow, please fix it soon bhai"
    assert hinglish_marker_count(text) == 1
    assert detect_english(text, 0.80).keep


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("Este aplicativo é muito bom para investir e fácil de usar todos os dias", "pt"),
        ("Esta aplicación es muy buena para invertir y fácil de usar cada día", "es"),
        ("Cette application est très bonne pour investir et facile à utiliser", "fr"),
    ],
)
def test_other_latin_languages_are_excluded(text: str, lang: str) -> None:
    decision = detect_english(text, 0.80)
    assert (decision.keep, decision.reason, decision.lang) == (False, "not_english", lang)


def test_short_borderline_english_falls_below_threshold() -> None:
    decision = detect_english("good app", 0.80)
    assert not decision.keep
    assert decision.reason == "low_confidence"
    assert decision.lang == "en"
    assert decision.confidence < 0.80


def test_threshold_is_configurable() -> None:
    assert detect_english("good app", 0.0).keep
    assert not detect_english(ENGLISH[0], 1.0, lexical_rescue=False).keep
    # With the rescue on, clean English below the threshold is kept by the lexical signal.
    assert detect_english(ENGLISH[0], 1.0).method == "lexical_rescue"
