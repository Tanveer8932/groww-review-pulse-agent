# SYNTHETIC TEST FIXTURE — not real review data
"""Quote selection and verification; action-idea validation."""

from __future__ import annotations

import json
from typing import Any

import pytest

from groww_pulse.analysis.actions import (
    ActionsOutput,
    action_problems,
    allowed_numbers,
    generate_actions,
)
from groww_pulse.analysis.llm import LLMClient, TokenBudget
from groww_pulse.analysis.quotes import (
    QUOTE_MAX_WORDS,
    quote_problems,
    select_quotes,
    sentence_spans,
    snippet_spans,
    verify_quotes,
)
from groww_pulse.errors import InsufficientDataError, LLMOutputError, QuoteVerificationError
from groww_pulse.models import Quote, RawReview, Review, Theme
from groww_pulse.preprocessing.normalize import count_words
from tests.fixtures.analysis import make_review, scripted_provider
from tests.fixtures.preprocessing import raw_review

LONG = (
    "SYNTHETIC FIXTURE The order screen is slow. When the market opens the buy button stops "
    "responding for several seconds and the price moves before my order is placed. I have "
    "missed many trades because of this delay and I am now considering another broker for "
    "all of my intraday trading."
)


def theme(theme_id: str, members: list[Review], rank: int = 1) -> Theme:
    return Theme(
        theme_id=theme_id,
        name=f"Theme {theme_id}",
        description="SYNTHETIC theme",
        review_ids=[r.review_id for r in members],
        count=len(members),
        share=0.2,
        avg_rating=2.0,
        low_rating_share=0.5,
        rank=rank,
    )


def text(n: int, topic: str) -> str:
    return f"SYNTHETIC FIXTURE review number {n} says the {topic} keeps failing for me every day"


# ---------------------------------------------------------------- spans
def test_sentence_spans_cover_text_without_separators() -> None:
    spans = sentence_spans(LONG)
    assert len(spans) == 3
    assert LONG[spans[0][0] : spans[0][1]].endswith("slow.")
    assert all(not LONG[s:e].startswith(" ") and not LONG[s:e].endswith(" ") for s, e in spans)


def test_short_text_is_quoted_whole_and_long_text_by_sentences() -> None:
    short = text(1, "login")
    assert snippet_spans(short) == [(0, len(short))]
    spans = snippet_spans(LONG)
    assert spans
    assert (0, len(LONG)) not in spans
    for start, end in spans:
        assert 9 <= count_words(LONG[start:end]) <= QUOTE_MAX_WORDS


def test_single_overlong_sentence_has_no_snippet() -> None:
    run_on = "SYNTHETIC FIXTURE " + " ".join(["slow"] * 40)
    assert snippet_spans(run_on) == []


# ---------------------------------------------------------------- selection
def test_one_verbatim_quote_per_top_theme() -> None:
    a = [make_review(f"a{i}", text(i, "login")) for i in range(3)]
    b = [make_review(f"b{i}", text(i, "withdrawal")) for i in range(3)]
    c = [make_review(f"c{i}", text(i, "order")) for i in range(3)]
    reviews = a + b + c
    quotes = select_quotes([theme("T1", a), theme("T2", b), theme("T3", c)], reviews)
    assert [q.theme_id for q in quotes] == ["T1", "T2", "T3"]
    by_id = {r.review_id: r for r in reviews}
    for q in quotes:
        assert by_id[q.review_id].text[q.start : q.end] == q.text


def test_long_review_quote_is_a_sentence_span_with_ellipsis_flags() -> None:
    long_review = make_review("long", LONG, thumbs_up=50)
    others = [make_review(f"o{i}", text(i, "order screen")) for i in range(2)]
    quotes = select_quotes(
        [theme("T1", [long_review]), theme("T2", others)], [long_review, *others]
    )
    quote = next(q for q in quotes if q.review_id == "long")
    assert LONG[quote.start : quote.end] == quote.text
    assert count_words(quote.text) <= QUOTE_MAX_WORDS
    assert quote.leading_ellipsis == (quote.start > 0)
    assert quote.trailing_ellipsis == (quote.end < len(LONG))
    assert quote.leading_ellipsis or quote.trailing_ellipsis


def test_quote_ineligible_reviews_are_never_chosen() -> None:
    eligible = [make_review(f"e{i}", text(i, "login")) for i in range(3)]
    lenient = [
        make_review(f"x{i}", text(i, "login") + " ❤️", quote_eligible=False, thumbs_up=99)
        for i in range(5)
    ]
    reviews = eligible + lenient
    quotes = select_quotes([theme("T1", reviews)], reviews)
    assert {q.review_id for q in quotes} <= {"e0", "e1", "e2"}


def test_redistribution_still_yields_exactly_three() -> None:
    a = [make_review(f"a{i}", text(i, "login")) for i in range(4)]
    b = [make_review("b0", text(0, "withdrawal"), quote_eligible=False)]
    c = [make_review("c0", text(0, "order"))]
    reviews = a + b + c
    quotes = select_quotes([theme("T1", a), theme("T2", b), theme("T3", c)], reviews)
    assert len(quotes) == 3
    assert [q.theme_id for q in quotes] == ["T1", "T3", "T1"]
    assert len({q.review_id for q in quotes}) == 3


def test_fewer_than_three_candidates_fails_closed() -> None:
    a = [make_review(f"a{i}", text(i, "login")) for i in range(2)]
    b = [make_review("b0", text(0, "order"), quote_eligible=False)]
    with pytest.raises(InsufficientDataError, match="exactly 3 verified quotes"):
        select_quotes([theme("T1", a), theme("T2", b)], a + b)


def test_placeholder_quotes_are_deprioritized() -> None:
    clean = make_review("clean", text(1, "support line"))
    redacted = make_review(
        "redacted",
        "SYNTHETIC FIXTURE support line [PHONE] keeps failing for me every day",
        thumbs_up=999,
    )
    extra = [make_review(f"z{i}", text(i, "other topic")) for i in range(2)]
    quotes = select_quotes(
        [theme("T1", [redacted, clean]), theme("T2", extra)], [clean, redacted, *extra]
    )
    assert quotes[0].review_id == "clean"


def test_preferred_word_range_wins_over_similarity() -> None:
    short = make_review("short", "SYNTHETIC FIXTURE login fails login fails login fails")  # 9 words
    good = make_review("good", text(1, "login screen"))  # 15 words
    extra = [make_review(f"z{i}", text(i, "withdrawal")) for i in range(2)]
    quotes = select_quotes([theme("T1", [short, good]), theme("T2", extra)], [short, good, *extra])
    assert quotes[0].review_id == "good"


# ---------------------------------------------------------------- verification
#: A type alias keeps this signature formatted identically by every ruff version in use.
_VerifiedSetup = tuple[list[Quote], dict[str, Review], dict[str, str], dict[str, RawReview]]


def _verified_setup() -> _VerifiedSetup:
    raws = [
        raw_review(f"v{i}", f"review number {i} says the login keeps failing for me every day")
        for i in range(3)
    ]
    reviews = [make_review(r.review_id, r.text) for r in raws]
    quotes = select_quotes([theme("T1", reviews)], reviews)
    assignments = {r.review_id: "T1" for r in reviews}
    return quotes, {r.review_id: r for r in reviews}, assignments, {r.review_id: r for r in raws}


def test_verification_passes_for_selected_quotes() -> None:
    quotes, by_id, assignments, raw = _verified_setup()
    verify_quotes(quotes, by_id, assignments, raw)


def test_tampering_with_one_character_fails() -> None:
    quotes, by_id, assignments, raw = _verified_setup()
    q = quotes[0]
    tampered = q.model_copy(update={"text": q.text[:-1] + ("X" if q.text[-1] != "X" else "Y")})
    with pytest.raises(QuoteVerificationError, match="verbatim span"):
        verify_quotes([tampered, *quotes[1:]], by_id, assignments, raw)


def test_tampered_stored_review_fails_hash_and_provenance() -> None:
    quotes, by_id, assignments, raw = _verified_setup()
    rid = quotes[0].review_id
    by_id[rid] = by_id[rid].model_copy(update={"text": by_id[rid].text.replace("login", "LOGIN")})
    problems = quote_problems(quotes[0], by_id, assignments, raw)
    assert any("text_sha256" in p for p in problems)
    assert any("provenance" in p for p in problems)


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (lambda q, s: (q, s.pop(q.review_id) and s), "not found"),
        (lambda q, s: (q.model_copy(update={"theme_id": "T9"}), s), "not a member"),
        (lambda q, s: (q.model_copy(update={"rating": 5}), s), "rating or date"),
        (lambda q, s: (q.model_copy(update={"leading_ellipsis": True}), s), "ellipsis"),
    ],
)
def test_quote_problems(mutate: Any, problem: str) -> None:
    quotes, by_id, assignments, raw = _verified_setup()
    quote, by_id = mutate(quotes[0], dict(by_id))
    assert any(problem in p for p in quote_problems(quote, by_id, assignments, raw))


def test_missing_raw_store_fails_provenance() -> None:
    quotes, by_id, assignments, _ = _verified_setup()
    with pytest.raises(QuoteVerificationError, match="provenance"):
        verify_quotes(quotes, by_id, assignments, {})


def test_wrong_quote_count_or_duplicates_fail() -> None:
    quotes, by_id, assignments, raw = _verified_setup()
    with pytest.raises(QuoteVerificationError, match="exactly 3"):
        verify_quotes(quotes[:2], by_id, assignments, raw)
    with pytest.raises(QuoteVerificationError, match="more than once"):
        verify_quotes([quotes[0], quotes[0], quotes[1]], by_id, assignments, raw)


def test_ineligible_or_pii_quote_fails() -> None:
    quotes, by_id, assignments, raw = _verified_setup()
    rid = quotes[0].review_id
    by_id[rid] = by_id[rid].model_copy(update={"quote_eligible": False})
    assert any("quote-eligible" in p for p in quote_problems(quotes[0], by_id, assignments, raw))

    pii_text = "SYNTHETIC FIXTURE call 9876543210 now please because login keeps failing"
    review = make_review("pii", pii_text)
    quote = Quote(review_id="pii", theme_id="T1", text=pii_text, start=0, end=len(pii_text),
                  rating=review.rating, created_at=review.created_at)  # fmt: skip
    problems = quote_problems(quote, {"pii": review}, {"pii": "T1"}, {})
    assert any("personal data" in p for p in problems)


# ---------------------------------------------------------------- actions
TOP = [
    Theme(theme_id="T3", name="Order lag", description="d", count=6, share=6 / 27,
          avg_rating=1.33, low_rating_share=1.0, rank=1),
    Theme(theme_id="T1", name="Login", description="d", count=5, share=5 / 27,
          avg_rating=1.6, low_rating_share=0.8, rank=2),
    Theme(theme_id="T2", name="Withdrawals", description="d", count=4, share=4 / 27,
          avg_rating=2.0, low_rating_share=0.75, rank=3),
]  # fmt: skip


def good_actions() -> list[dict[str, Any]]:
    return [
        {"theme_id": "T3", "action": "Fix order button freezes at market open",
         "owner_area": "Trading Platform", "rationale": "Order lag has 6 reviews, 22.2% of all."},
        {"theme_id": "T1", "action": "Add a backup channel for login codes",
         "owner_area": "Engineering", "rationale": "Login failures block access for users."},
        {"theme_id": "T2", "action": "Show a withdrawal status timeline in the app",
         "owner_area": "Payments", "rationale": "Users cannot tell where their money is."},
    ]  # fmt: skip


def problems_for(actions: list[dict[str, Any]]) -> list[str]:
    output = ActionsOutput.model_validate({"actions": actions})
    return action_problems(output, ["T3", "T1", "T2"], allowed_numbers(TOP))


def test_valid_actions_pass() -> None:
    assert problems_for(good_actions()) == []
    assert {"6", "22.2", "1.3", "100", "1", "2", "3"} <= allowed_numbers(TOP)


def _with(index: int, **changes: str) -> list[dict[str, Any]]:
    actions = good_actions()
    actions[index] = {**actions[index], **changes}
    return actions


@pytest.mark.parametrize(
    ("actions", "problem"),
    [
        (good_actions()[:2], "exactly 3 actions"),
        (good_actions() + good_actions()[:1], "exactly 3 actions"),
        (_with(1, theme_id="T3"), "one action per theme"),
        (_with(0, action="Fix " + "very " * 25 + "slow orders"), "longer than 25 words"),
        (_with(0, rationale="word " * 41), "rationale is longer"),
        (_with(0, action="The order button should be faster"), "imperative verb"),
        (_with(0, action="Users want faster orders"), "imperative verb"),
        (_with(0, action="Cut order latency by 40 percent"), "numbers not in the computed"),
        (_with(0, rationale="Affects 99 reviews"), "numbers not in the computed"),
        (_with(0, action='Fix the "button never works" bug'), "quotation marks"),
        (_with(0, action="Email ops@example.com about orders"), "personal data"),
        (_with(0, owner_area="Marketing"), "owner area"),
        (_with(0, action=""), "is empty"),
        (_with(1, action="Fix order button freezes at market open"), "repeats"),
    ],
)
def test_invalid_actions_are_rejected(actions: list[dict[str, Any]], problem: str) -> None:
    assert any(problem in p for p in problems_for(actions)), problems_for(actions)


def test_generate_actions_repairs_invented_numbers_and_orders_by_rank() -> None:
    bad = [*good_actions()[:2], {**good_actions()[2], "rationale": "Hits 87 percent of users."}]
    fixed = list(reversed(good_actions()))
    provider = scripted_provider([json.dumps({"actions": bad}), json.dumps({"actions": fixed})])
    llm = LLMClient(provider, budget=TokenBudget(1_000_000))
    actions = generate_actions(llm, TOP, [])
    assert [a.theme_id for a in actions] == ["T3", "T1", "T2"]
    assert "87" in provider.calls[1].last_user


def test_generate_actions_fails_after_second_invalid_reply() -> None:
    bad = json.dumps({"actions": good_actions()[:2]})
    llm = LLMClient(scripted_provider([bad, bad]), budget=TokenBudget(1_000_000))
    with pytest.raises(LLMOutputError, match="exactly 3 actions"):
        generate_actions(llm, TOP, [])
