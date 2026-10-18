# SYNTHETIC TEST FIXTURE — not real review data
"""End-to-end Phase 3 analysis on the synthetic corpus with fake LLM providers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from groww_pulse.analysis.llm import LLMClient, ResponseCache, TokenBudget
from groww_pulse.analysis.pipeline import AnalysisResult, run_analysis
from groww_pulse.analysis.quotes import verify_quotes
from groww_pulse.config import Settings, load_settings
from groww_pulse.errors import (
    BudgetExceededError,
    InsufficientDataError,
    LLMOutputError,
    LLMProviderError,
    PreprocessingError,
    QuoteVerificationError,
)
from groww_pulse.models import Quote, Review
from groww_pulse.preprocessing.pii import scan_pii
from groww_pulse.preprocessing.pipeline import load_analysis_reviews
from groww_pulse.storage.jsonl_store import JsonlReviewStore
from tests.fixtures.analysis import (
    CORPUS,
    PLANTED_PHONE,
    Call,
    RecordingProvider,
    build_corpus,
    data_blocks,
    keyword_provider,
    keyword_responder,
)

N_CORPUS = sum(len(v) for v in CORPUS.values())


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(env_file=None, data_dir=tmp_path / "data", log_level="WARNING")


@pytest.fixture
def corpus(settings: Settings) -> list[Review]:
    return build_corpus(settings)


def analyse(settings: Settings, provider: Any, run_id: str = "run-1") -> AnalysisResult:
    return run_analysis(settings, run_id=run_id, provider=provider, allow_synthetic=True)


def runs(settings: Settings) -> list[Path]:
    return sorted((settings.data_dir / "runs").glob("*/analysis.json"))


@pytest.mark.usefixtures("corpus")
def test_end_to_end_analysis(settings: Settings) -> None:
    provider = keyword_provider()
    result = analyse(settings, provider)
    a = result.analysis

    # Themes: 1..5, every review assigned to a theme or other, top 3 by score.
    assert 1 <= len(a["themes"]) <= 5
    assert set(a["assignments"]) == {r["review_id"] for r in _reviews(settings)}
    assert all(t["count"] >= 1 for t in a["themes"])
    assert a["top_theme_ids"] == [t["theme_id"] for t in a["themes"][:3]]
    assert [t["rank"] for t in a["themes"]] == list(range(1, len(a["themes"]) + 1))
    scores = [(-t["score"], -t["count"], t["theme_id"]) for t in a["themes"]]
    assert scores == sorted(scores)

    # Counts and percentages reconcile with the assignments.
    total = a["input"]["n_analyzed"]
    assert total == N_CORPUS
    assert sum(t["count"] for t in a["themes"]) + a["other"]["count"] == total
    for t in a["themes"]:
        members = [rid for rid, tid in a["assignments"].items() if tid == t["theme_id"]]
        assert sorted(members) == t["review_ids"]
        assert len(members) == t["count"]
        assert t["share_pct"] == f"{100 * t['count'] / total:.1f}"
        assert sum(t["rating_counts"].values()) == t["count"]
        assert t["low_rating_count"] == t["rating_counts"]["1"] + t["rating_counts"]["2"]
        assert t["score"] == t["count"] + t["low_rating_count"]

    # Exactly 3 verified quotes and 3 actions tied to the top themes.
    assert len(a["quotes"]) == 3
    assert len(a["actions"]) == 3
    assert {q["theme_id"] for q in a["quotes"]} <= set(a["top_theme_ids"])
    assert [x["theme_id"] for x in a["actions"]] == a["top_theme_ids"]
    reviews = {r.review_id: r for r in load_analysis_reviews(
        settings.data_dir / "reviews.jsonl", settings, allow_synthetic=True)}  # fmt: skip
    quotes = [Quote.model_validate(q) for q in a["quotes"]]
    for q in quotes:
        assert reviews[q.review_id].text[q.start : q.end] == q.text
    raw = JsonlReviewStore(settings.data_dir / "raw" / "reviews_raw.jsonl").load()
    verify_quotes(quotes, reviews, a["assignments"], raw)

    # Persisted, labelled as synthetic, with method and usage recorded.
    saved = json.loads(runs(settings)[0].read_text(encoding="utf-8"))
    assert saved["analysis"] == json.loads(json.dumps(a))
    assert saved["synthetic_fixture_data"] is True
    assert saved["analysis_sha256"] == result.analysis_sha256
    assert saved["analysis"]["method"]["themes"] == "llm"
    assert "count × (1 + low_rating_share)" in saved["analysis"]["method"]["ranking_formula"]
    assert saved["llm_usage"]["provider_calls"] == len(provider.calls) == 3
    assert result.summary()["quotes"] == 3


def _reviews(settings: Settings) -> list[dict[str, Any]]:
    path = settings.data_dir / "reviews.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.usefixtures("corpus")
def test_rerun_uses_cache_and_is_identical(settings: Settings) -> None:
    first = analyse(settings, keyword_provider(), "run-1")
    provider = keyword_provider()
    second = analyse(settings, provider, "run-2")
    assert provider.calls == []
    assert second.llm_usage["cache_hits"] == 3
    assert second.analysis == first.analysis
    assert second.analysis_sha256 == first.analysis_sha256


@pytest.mark.usefixtures("corpus")
def test_no_unredacted_text_reaches_the_llm(settings: Settings) -> None:
    provider = keyword_provider()
    analyse(settings, provider)
    raw_texts = [r.text for r in JsonlReviewStore(settings.data_dir / "raw" / "reviews_raw.jsonl")]
    assert any(PLANTED_PHONE in t for t in raw_texts)  # the raw store does hold it
    for call in provider.calls:
        prompt = call.system + "".join(m["content"] for m in call.messages)
        assert PLANTED_PHONE not in prompt
        for records in data_blocks(call.messages[0]["content"]).values():
            for record in records:
                for value in record.values():
                    if isinstance(value, str):
                        assert scan_pii(value) == {}


@pytest.mark.usefixtures("corpus")
def test_injection_review_stays_inside_the_data_block(settings: Settings) -> None:
    provider = keyword_provider()
    result = analyse(settings, provider)
    for call in provider.calls[:2]:
        prompt = call.messages[0]["content"]
        outside = prompt
        for tag in ("reviews", "themes"):
            start, end = outside.find(f"<{tag}>"), outside.find(f"</{tag}>")
            if start != -1:
                outside = outside[:start] + outside[end + len(tag) + 3 :]
        assert "Ignore all previous instructions" in prompt
        assert "Ignore all previous instructions" not in outside
    # It was classified as data, not obeyed: no theme named HACKED, it went to "other".
    a = result.analysis
    assert all("HACKED" not in t["name"] for t in a["themes"])
    injected = next(rid for rid in a["assignments"] if rid.startswith("syn-injection"))
    assert a["assignments"][injected] == "other"


@pytest.mark.usefixtures("corpus")
def test_fallback_clustering_when_llm_down_for_themes(settings: Settings) -> None:
    good = keyword_responder()

    def respond(call: Call) -> str:
        if call.kind != "actions":
            raise LLMProviderError("synthetic outage (test)")
        return good(call)

    result = analyse(settings, RecordingProvider(respond))
    method = result.analysis["method"]
    assert method["themes"] == "fallback_clustering"
    assert method["prompts"]["theme_discovery"] is None
    assert len(result.analysis["actions"]) == 3
    assert any("fallback clustering" in w for w in result.warnings)


@pytest.mark.usefixtures("corpus")
def test_provider_down_entirely_fails_without_output(settings: Settings) -> None:
    from tests.fixtures.analysis import failing_provider

    with pytest.raises(LLMProviderError):
        analyse(settings, failing_provider())
    assert runs(settings) == []


@pytest.mark.usefixtures("corpus")
def test_invalid_ai_output_fails_closed(settings: Settings) -> None:
    provider = RecordingProvider(lambda call: '{"themes": "not a list"}')
    with pytest.raises(LLMOutputError):
        analyse(settings, provider)
    assert len(provider.calls) == 2
    assert runs(settings) == []


@pytest.mark.usefixtures("corpus")
def test_budget_exceeded_fails_closed(settings: Settings) -> None:
    client = LLMClient(keyword_provider(), budget=TokenBudget(1000), cache=None)
    with pytest.raises(BudgetExceededError):
        run_analysis(settings, run_id="r", client=client, allow_synthetic=True)
    assert runs(settings) == []


@pytest.mark.usefixtures("corpus")
def test_fewer_than_three_themes_fails(settings: Settings) -> None:
    with pytest.raises(InsufficientDataError, match="non-empty theme"):
        analyse(settings, keyword_provider(themes=("login", "orders")))
    assert runs(settings) == []


@pytest.mark.usefixtures("corpus")
def test_missing_raw_store_fails_quote_provenance(settings: Settings) -> None:
    (settings.data_dir / "raw" / "reviews_raw.jsonl").unlink()
    with pytest.raises(QuoteVerificationError, match="provenance"):
        analyse(settings, keyword_provider())
    assert runs(settings) == []


@pytest.mark.usefixtures("corpus")
def test_synthetic_data_refused_without_opt_in(settings: Settings) -> None:
    provider = keyword_provider()
    with pytest.raises(PreprocessingError, match="synthetic test fixture"):
        run_analysis(settings, run_id="r", provider=provider)
    assert provider.calls == []


def test_missing_reviews_file(settings: Settings) -> None:
    with pytest.raises(PreprocessingError, match="does not exist"):
        analyse(settings, keyword_provider())


def test_empty_dataset_is_refused_before_any_llm_call(settings: Settings) -> None:
    path = settings.data_dir / "reviews.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    provider = keyword_provider()
    with pytest.raises(InsufficientDataError, match="no reviews"):
        analyse(settings, provider)
    assert provider.calls == []
    assert not (settings.data_dir / "runs").exists()


@pytest.mark.usefixtures("corpus")
def test_too_few_reviews_is_refused(tmp_path: Path, settings: Settings) -> None:
    strict = load_settings(
        env_file=None, data_dir=settings.data_dir, analysis_min_reviews=N_CORPUS + 1
    )
    provider = keyword_provider()
    with pytest.raises(InsufficientDataError, match="ANALYSIS_MIN_REVIEWS"):
        analyse(strict, provider)
    assert provider.calls == []


def test_too_few_quote_eligible_reviews(settings: Settings) -> None:
    build_corpus(settings)
    path = settings.data_dir / "reviews.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    # Lenient-policy reviews with emoji would be quote-ineligible; here we mark them so
    # directly, which the gate accepts for emoji-free text.
    for row in rows[2:]:
        row["quote_eligible"] = False
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    with pytest.raises(InsufficientDataError, match="quote-eligible"):
        analyse(settings, keyword_provider())


def test_default_provider_requires_configuration(settings: Settings) -> None:
    from groww_pulse.errors import ConfigError

    build_corpus(settings)
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        run_analysis(settings, run_id="r", allow_synthetic=True)


@pytest.mark.usefixtures("corpus")
def test_cache_written_under_data_dir(settings: Settings) -> None:
    analyse(settings, keyword_provider())
    cache = ResponseCache(settings.data_dir / "cache" / "llm")
    assert len(list(cache.cache_dir.glob("*.json"))) == 3


@pytest.mark.usefixtures("corpus")
def test_analysis_under_groq_free_tier_limits(settings: Settings) -> None:
    """Groq-style per-request limits shrink requests; the 5 / 3 / 3 contract still holds."""
    provider = keyword_provider()
    client = LLMClient(
        provider,
        budget=TokenBudget(10**7),
        request_token_limit=4_000,
        max_output_tokens=1_000,
    )
    result = run_analysis(settings, run_id="run-groq", client=client, allow_synthetic=True)
    a = result.analysis
    assert 3 <= len(a["themes"]) <= 5
    assert set(a["assignments"]) == {r["review_id"] for r in _reviews(settings)}
    assert len(a["quotes"]) == 3
    assert len(a["actions"]) == 3
    assert all(call.max_tokens <= 1_000 for call in provider.calls)
    assert a["method"]["themes"] == "llm"
    for call in provider.calls:  # every request stayed under the per-request limit
        chars = len(call.system) + sum(len(m["content"]) for m in call.messages)
        assert -(-chars // 3) + call.max_tokens <= 4_000
