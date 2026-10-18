# SYNTHETIC TEST FIXTURE — not real review data
"""Theme discovery/assignment validation, stratified sampling, fallback, and ranking."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from groww_pulse.analysis.llm import LLMClient, TokenBudget
from groww_pulse.analysis.ranking import other_bucket, rank_themes, theme_stats
from groww_pulse.analysis.themes import (
    METHOD_FALLBACK,
    METHOD_LLM,
    OTHER,
    SAMPLE_LIMIT,
    ThemeDefinition,
    assign_themes,
    build_themes,
    discover_themes,
    fallback_cluster,
    stratified_sample,
)
from groww_pulse.config import Settings, load_settings
from groww_pulse.errors import InsufficientDataError, LLMOutputError
from groww_pulse.models import Review
from tests.fixtures.analysis import (
    Call,
    RecordingProvider,
    build_corpus,
    data_blocks,
    keyword_responder,
    make_review,
    scripted_provider,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(env_file=None, data_dir=tmp_path / "data", log_level="WARNING")


@pytest.fixture
def corpus(settings: Settings) -> list[Review]:
    return build_corpus(settings)


def client(provider: Any) -> LLMClient:
    return LLMClient(provider, budget=TokenBudget(10_000_000))


def theme(name: str, ids: list[str]) -> dict[str, Any]:
    return {"name": name, "description": "SYNTHETIC description", "evidence_review_ids": ids}


# ---------------------------------------------------------------- discovery
def test_discovery_assigns_ids_in_order(corpus: list[Review]) -> None:
    llm = client(RecordingProvider(keyword_responder()))
    definitions, sample_size = discover_themes(llm, corpus)
    assert [d.theme_id for d in definitions] == ["T1", "T2", "T3", "T4", "T5"]
    assert sample_size == len(corpus)
    assert all(len(d.evidence_review_ids) >= 2 for d in definitions)


@pytest.mark.parametrize(
    ("themes", "problem"),
    [
        ([theme(f"Theme {c}", ["a", "b"]) for c in "ABCDEF"], "expected 1 to 5 themes"),
        ([], "expected 1 to 5 themes"),
        ([theme("Slow orders", ["a", "b"]), theme("slow  ORDERS", ["a", "b"])], "duplicates"),
        ([theme("Slow orders", ["a", "zzz"])], "not in the sample"),
        ([theme("Slow orders", ["a"])], "distinct evidence ids"),
        ([theme("Other", ["a", "b"])], "catch-all"),
        ([theme("Top 3 bugs", ["a", "b"])], "contains digits"),
        ([theme("", ["a", "b"])], "is empty"),
        ([theme("Email a.b@example.com", ["a", "b"])], "personal data"),
        ([theme("x" * 61, ["a", "b"])], "longer than"),
    ],
)
def test_invalid_discovery_is_rejected_after_repair(themes: list[Any], problem: str) -> None:
    reviews = [
        make_review(rid, f"SYNTHETIC FIXTURE review {rid} about slow orders") for rid in "ab"
    ]
    bad = json.dumps({"themes": themes})
    provider = scripted_provider([bad, bad])
    with pytest.raises(LLMOutputError, match=problem):
        discover_themes(client(provider), reviews)
    assert len(provider.calls) == 2  # one repair retry, then fail


def test_discovery_long_description_rejected() -> None:
    reviews = [make_review(rid, f"SYNTHETIC FIXTURE review {rid}") for rid in "ab"]
    bad = json.dumps(
        {
            "themes": [
                {"name": "Slow", "description": "word " * 41, "evidence_review_ids": ["a", "b"]}
            ]
        }
    )
    with pytest.raises(LLMOutputError, match="description is longer"):
        discover_themes(client(scripted_provider([bad, bad])), reviews)


# ---------------------------------------------------------------- assignment
DEFS = [ThemeDefinition("T1", "Slow orders", "d"), ThemeDefinition("T2", "Login", "d")]


def test_assignment_missing_reviews_default_to_other() -> None:
    reviews = [make_review(f"r{i}", f"SYNTHETIC FIXTURE text {i}") for i in range(3)]
    reply = json.dumps({"assignments": [{"review_id": "r0", "theme_id": "T1"}]})
    assignments, defaulted = assign_themes(client(scripted_provider([reply])), reviews, DEFS)
    assert assignments == {"r0": "T1", "r1": OTHER, "r2": OTHER}
    assert defaulted == 2


@pytest.mark.parametrize(
    ("items", "problem"),
    [
        ([{"review_id": "ghost", "theme_id": "T1"}], "unknown review id"),
        (
            [{"review_id": "r0", "theme_id": "T1"}, {"review_id": "r0", "theme_id": "T2"}],
            "more than once",
        ),
        ([{"review_id": "r0", "theme_id": "T9"}], "unknown theme id"),
    ],
)  # fmt: skip
def test_assignment_rejects_unknown_and_repeated_ids(items: list[Any], problem: str) -> None:
    reviews = [make_review("r0", "SYNTHETIC FIXTURE text")]
    bad = json.dumps({"assignments": items})
    with pytest.raises(LLMOutputError, match=problem):
        assign_themes(client(scripted_provider([bad, bad])), reviews, DEFS)


def test_assignment_batches_and_schema_enums() -> None:
    reviews = [make_review(f"r{i:03d}", f"SYNTHETIC FIXTURE text {i}") for i in range(120)]

    def respond(call: Call) -> str:
        ids = [r["id"] for r in data_blocks(call.messages[0]["content"])["reviews"]]
        return json.dumps({"assignments": [{"review_id": i, "theme_id": "T2"} for i in ids]})

    provider = RecordingProvider(respond)
    assignments, defaulted = assign_themes(client(provider), reviews, DEFS)
    assert len(provider.calls) == 3  # 50 + 50 + 20
    assert defaulted == 0
    assert set(assignments.values()) == {"T2"}
    item = provider.calls[0].schema["properties"]["assignments"]["items"]["properties"]
    assert item["theme_id"]["enum"] == ["T1", "T2", OTHER]
    assert len(item["review_id"]["enum"]) == 50


# ---------------------------------------------------------------- sampling
def test_stratified_sample_is_bounded_deterministic_and_covers_strata() -> None:
    reviews = [
        make_review(f"r{i:04d}", f"SYNTHETIC FIXTURE text {i}", rating=1 + i % 5, day=i % 70)
        for i in range(1000)
    ]
    sample = stratified_sample(reviews)
    assert len(sample) == SAMPLE_LIMIT
    assert sample == stratified_sample(list(reviews))
    assert {r.rating for r in sample} == {1, 2, 3, 4, 5}
    weeks = {r.created_at.isocalendar().week for r in reviews}
    assert {r.created_at.isocalendar().week for r in sample} == weeks
    small = reviews[:10]
    assert stratified_sample(small) == small


# ---------------------------------------------------------------- fallback
def test_fallback_clustering_produces_three_to_five_themes(corpus: list[Review]) -> None:
    definitions, assignments = fallback_cluster(corpus)
    assert 3 <= len(definitions) <= 5
    assert set(assignments) == {r.review_id for r in corpus}
    assert all(d.name.startswith("Fallback: ") for d in definitions)
    assert fallback_cluster(corpus) == (definitions, assignments)  # deterministic


def test_fallback_needs_enough_text() -> None:
    with pytest.raises(InsufficientDataError):
        fallback_cluster([make_review("a", "SYNTHETIC FIXTURE x")])
    tiny = [make_review(f"r{i}", "1 2 3 4 5 6 7 8 9") for i in range(6)]
    with pytest.raises(InsufficientDataError, match="no usable terms"):
        fallback_cluster(tiny)


def test_build_themes_falls_back_when_provider_is_down(corpus: list[Review]) -> None:
    from tests.fixtures.analysis import failing_provider

    result = build_themes(client(failing_provider()), corpus)
    assert result.method == METHOD_FALLBACK
    assert "synthetic outage" in (result.fallback_reason or "")
    assert result.warnings


def test_build_themes_llm_path(corpus: list[Review]) -> None:
    result = build_themes(client(RecordingProvider(keyword_responder())), corpus)
    assert result.method == METHOD_LLM
    assert set(result.assignments) == {r.review_id for r in corpus}
    assert len(result.definitions) <= 5


# ---------------------------------------------------------------- ranking
def _ranking_fixture() -> tuple[list[ThemeDefinition], dict[str, str], list[Review]]:
    """Hand-computed: 10 reviews.

    T1: ratings 1,1,5,5     count 4, low 2 -> score 6
    T2: ratings 1,2,3       count 3, low 2 -> score 5
    T3: ratings 4,1,2       count 3, low 2 -> score 5  (ties T2 on score and count -> T2 first)
    T4: no members          dropped
    other: ratings 1,1      never ranked
    """
    ratings = {"a": 1, "b": 1, "c": 5, "d": 5, "e": 1, "f": 2, "g": 3, "h": 4, "i": 1, "j": 2}
    themes = {"a": "T1", "b": "T1", "c": "T1", "d": "T1", "e": "T2", "f": "T2", "g": "T2",
              "h": "T3", "j": "T3", "i": OTHER}  # fmt: skip
    themes["k"] = "T3"
    ratings["k"] = 1
    reviews = [make_review(rid, f"SYNTHETIC FIXTURE {rid}", rating=r) for rid, r in ratings.items()]
    reviews.append(make_review("l", "SYNTHETIC FIXTURE l", rating=1))  # absent -> other
    definitions = [ThemeDefinition(f"T{i}", f"Theme {i}", "d") for i in range(1, 5)]
    return definitions, themes, reviews


def test_theme_stats_are_exact() -> None:
    definitions, assignments, reviews = _ranking_fixture()
    stats = {t.theme_id: t for t in theme_stats(definitions, assignments, reviews)}
    t1 = stats["T1"]
    assert (t1.count, t1.low_rating_count, t1.score) == (4, 2, 6.0)
    assert t1.share == pytest.approx(4 / 12)
    assert t1.avg_rating == pytest.approx(3.0)
    assert t1.low_rating_share == pytest.approx(0.5)
    assert t1.rating_counts == {1: 2, 2: 0, 3: 0, 4: 0, 5: 2}
    assert t1.review_ids == ["a", "b", "c", "d"]
    assert stats["T4"].count == 0
    assert stats["T4"].avg_rating is None
    # Score equals the documented formula count × (1 + low_rating_share).
    for t in stats.values():
        assert t.score == pytest.approx(t.count * (1 + t.low_rating_share))
    assert other_bucket(assignments, reviews) == {"count": 2, "share": 2 / 12}


def test_ranking_order_tie_breaks_and_exclusions() -> None:
    definitions, assignments, reviews = _ranking_fixture()
    ranked = rank_themes(theme_stats(definitions, assignments, reviews))
    assert [(t.theme_id, t.rank) for t in ranked] == [("T1", 1), ("T2", 2), ("T3", 3)]
    assert all(t.theme_id != OTHER for t in ranked)


def test_ranking_tie_on_score_breaks_by_count() -> None:
    reviews = [make_review(f"x{i}", "SYNTHETIC FIXTURE x", rating=5) for i in range(4)]
    reviews += [make_review(f"y{i}", "SYNTHETIC FIXTURE y", rating=1) for i in range(2)]
    assignments = {**{f"x{i}": "T2" for i in range(4)}, **{f"y{i}": "T1" for i in range(2)}}
    definitions = [ThemeDefinition("T1", "Y", "d"), ThemeDefinition("T2", "X", "d")]
    ranked = rank_themes(theme_stats(definitions, assignments, reviews))
    # Both score 4: T2 has 4 members (no low ratings), T1 has 2 members (both low).
    assert [t.theme_id for t in ranked] == ["T2", "T1"]
    assert ranked[0].score == ranked[1].score == 4.0
