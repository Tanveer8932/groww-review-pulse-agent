# SYNTHETIC TEST FIXTURE — not real review data
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from groww_pulse.preprocessing.dedupe import DedupeItem, deduplicate

BASE = datetime(2026, 9, 1, tzinfo=UTC)


def item(review_id: str, text: str, *, rating: int = 2, day: int = 0) -> DedupeItem[str]:
    return DedupeItem(
        review_id=review_id,
        sort_key=(BASE + timedelta(days=day),),
        rating=rating,
        text=text,
        payload=review_id,
    )


TEXT = "SYNTHETIC FIXTURE the app crashes every time I open the portfolio screen"


def kept_ids(items: list[DedupeItem[str]]) -> list[str]:
    return [i.review_id for i in deduplicate(items).kept]


def test_identical_ids_keep_first() -> None:
    result = deduplicate([item("a", TEXT, day=0), item("a", TEXT + " again", day=1)])
    assert [i.payload for i in result.kept] == ["a"]
    assert result.reason_counts() == {"duplicate_id": 1}


def test_case_and_whitespace_variants_are_exact_duplicates() -> None:
    result = deduplicate([item("b", "  " + TEXT.upper() + "\n", day=2), item("a", TEXT, day=1)])
    assert [i.review_id for i in result.kept] == ["a"]  # earliest kept
    assert result.reason_counts() == {"exact_duplicate": 1}


def test_near_duplicate_same_rating_is_dropped() -> None:
    variant = TEXT.replace("crashes", "crashes,") + "!"
    result = deduplicate([item("a", TEXT), item("b", variant, day=1)])
    assert kept_ids([item("a", TEXT), item("b", variant, day=1)]) == ["a"]
    assert result.reason_counts() == {"near_duplicate": 1}


def test_near_duplicate_with_word_order_change() -> None:
    a = "SYNTHETIC FIXTURE login fails after the update and support never replies at all"
    b = "SYNTHETIC FIXTURE after the update login fails and support never replies at all"
    assert kept_ids([item("a", a), item("b", b, day=1)]) == ["a"]


def test_near_duplicates_with_different_ratings_are_not_merged() -> None:
    variant = TEXT + "!"
    assert kept_ids([item("a", TEXT, rating=1), item("b", variant, rating=5, day=1)]) == ["a", "b"]


def test_subset_review_is_not_merged_into_longer_review() -> None:
    longer = TEXT + " and also the charts take a minute to load during market hours daily"
    assert kept_ids([item("a", TEXT), item("b", longer, day=1)]) == ["a", "b"]


def test_distinct_reviews_are_kept() -> None:
    other = "SYNTHETIC FIXTURE withdrawal to my bank took five working days this month"
    assert kept_ids([item("a", TEXT), item("b", other)]) == ["a", "b"]


def test_ties_break_by_review_id_and_result_is_order_independent() -> None:
    items = [item("z", TEXT), item("y", TEXT.lower()), item("x", TEXT + " ")]
    assert kept_ids(items) == ["x"]
    assert kept_ids(list(reversed(items))) == ["x"]


def test_empty_and_single() -> None:
    assert deduplicate([]).kept == []
    assert kept_ids([item("a", TEXT)]) == ["a"]


def test_chain_is_compared_against_kept_only() -> None:
    a = "SYNTHETIC FIXTURE one two three four five six seven eight nine ten"
    b = a + " eleven"  # near-duplicate of a
    c = b + " twelve"  # near-duplicate of b, and also of a
    result = deduplicate([item("a", a), item("b", b, day=1), item("c", c, day=2)])
    assert [i.review_id for i in result.kept] == ["a"]
    assert result.reason_counts() == {"near_duplicate": 2}
