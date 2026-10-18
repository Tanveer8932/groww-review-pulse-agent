"""Phase 3 analysis stage: ``reviews.jsonl`` -> ``runs/{run_id}/analysis.json``.

Order: load (via the Phase 2 gate) -> data sufficiency checks -> themes (LLM, or fallback
clustering if the provider is down) -> code-computed statistics and ranking -> top 3 ->
code-selected, verified quotes -> LLM action ideas (validated) -> write.

Fail-closed rules (each raises an :class:`AnalysisError` subclass and writes nothing):

* ``reviews.jsonl`` missing, unfit, or containing synthetic fixtures (``PreprocessingError``
  from :func:`load_analysis_reviews`; only tests pass ``allow_synthetic=True``);
* fewer than ``ANALYSIS_MIN_REVIEWS`` reviews, or fewer than 3 quote-eligible ones;
* fewer than 3 non-empty themes;
* fewer than 3 verified quotes, or any quote failing verification;
* LLM output still invalid after one repair, token budget exceeded, provider unavailable
  for action ideas.

Nothing is invented to fill a gap: an empty or insufficient dataset produces an error, not
an analysis.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from groww_pulse.analysis.actions import ACTIONS_PROMPT, OWNER_AREAS, generate_actions, pct
from groww_pulse.analysis.llm import LLMClient, LLMProvider, build_client, provider_from_settings
from groww_pulse.analysis.quotes import QUOTES_REQUIRED, select_quotes, verify_quotes
from groww_pulse.analysis.ranking import (
    RANKING_FORMULA,
    TOP_N,
    other_bucket,
    rank_themes,
    theme_stats,
)
from groww_pulse.analysis.themes import (
    ASSIGNMENT_PROMPT,
    DISCOVERY_PROMPT,
    METHOD_FALLBACK,
    build_themes,
)
from groww_pulse.config import Settings
from groww_pulse.errors import InsufficientDataError
from groww_pulse.models import ActionIdea, Quote, Review, Theme
from groww_pulse.preprocessing.pipeline import (
    OUTPUT_FILE,
    RAW_STORE,
    SYNTHETIC_MARKER_RE,
    load_analysis_reviews,
)
from groww_pulse.storage.jsonl_store import JsonlReviewStore, atomic_write_lines

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ANALYSIS_FILE = "analysis.json"


@dataclass
class AnalysisResult:
    run_id: str
    created_at: datetime
    analysis: dict[str, Any]
    llm_usage: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    synthetic_fixture_data: bool = False
    output_path: Path | None = None

    @property
    def analysis_sha256(self) -> str:
        """Hash of the deterministic analysis content (excludes run ID, time, usage)."""
        canonical = json.dumps(self.analysis, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat(),
            "synthetic_fixture_data": self.synthetic_fixture_data,
            "analysis_sha256": self.analysis_sha256,
            "warnings": self.warnings,
            "llm_usage": self.llm_usage,
            "analysis": self.analysis,
        }

    def summary(self) -> dict[str, Any]:
        """Short view for the CLI: no review text, no member lists."""
        a = self.analysis
        return {
            "run_id": self.run_id,
            "output_path": str(self.output_path) if self.output_path else None,
            "analysis_sha256": self.analysis_sha256,
            "n_analyzed": a["input"]["n_analyzed"],
            "theme_method": a["method"]["themes"],
            "themes": [
                {k: t[k] for k in ("theme_id", "rank", "name", "count", "share_pct", "score")}
                for t in a["themes"]
            ],
            "other": a["other"],
            "top_theme_ids": a["top_theme_ids"],
            "quotes": len(a["quotes"]),
            "actions": len(a["actions"]),
            "llm_usage": self.llm_usage,
            "warnings": self.warnings,
        }


def _theme_dict(theme: Theme) -> dict[str, Any]:
    data = theme.model_dump(mode="json")
    data["share_pct"] = pct(theme.share)
    data["low_rating_pct"] = pct(theme.low_rating_share)
    return data


def _check_sufficient(reviews: list[Review], settings: Settings) -> None:
    if not reviews:
        raise InsufficientDataError(
            "reviews.jsonl contains no reviews; import and preprocess a real, permitted "
            "export first. No analysis was produced."
        )
    if len(reviews) < settings.analysis_min_reviews:
        raise InsufficientDataError(
            f"only {len(reviews)} review(s) available; ANALYSIS_MIN_REVIEWS is "
            f"{settings.analysis_min_reviews}. No analysis was produced."
        )
    eligible = sum(r.quote_eligible for r in reviews)
    if eligible < QUOTES_REQUIRED:
        raise InsufficientDataError(
            f"only {eligible} quote-eligible review(s); {QUOTES_REQUIRED} are required"
        )


def run_analysis(
    settings: Settings,
    *,
    run_id: str,
    provider: LLMProvider | None = None,
    client: LLMClient | None = None,
    allow_synthetic: bool = False,
) -> AnalysisResult:
    """Analyse ``DATA_DIR/reviews.jsonl`` and write ``DATA_DIR/runs/{run_id}/analysis.json``.

    ``provider`` defaults to the configured one (``ConfigError`` if it is not configured);
    ``client`` overrides both (tests). Raises an ``AnalysisError`` subclass or
    ``PreprocessingError`` on any fail-closed condition (see module docstring).
    """
    data_dir = settings.data_dir
    reviews_path = data_dir / OUTPUT_FILE
    reviews = load_analysis_reviews(reviews_path, settings, allow_synthetic=allow_synthetic)
    _check_sufficient(reviews, settings)
    reviews_sha256 = hashlib.sha256(reviews_path.read_bytes()).hexdigest()

    if client is None:
        client = build_client(settings, provider or provider_from_settings(settings))

    theme_result = build_themes(client, reviews)
    warnings = list(theme_result.warnings)
    themes = theme_stats(theme_result.definitions, theme_result.assignments, reviews)
    empty = [t.theme_id for t in themes if t.count == 0]
    if empty:
        warnings.append(f"theme(s) with no assigned reviews dropped: {', '.join(empty)}")
    ranked = rank_themes(themes)
    if len(ranked) < TOP_N:
        raise InsufficientDataError(
            f"only {len(ranked)} non-empty theme(s) found; the pulse needs {TOP_N}"
        )
    top = ranked[:TOP_N]

    quotes: list[Quote] = select_quotes(top, reviews)
    raw_by_id = JsonlReviewStore(data_dir / RAW_STORE).load()
    verify_quotes(quotes, {r.review_id: r for r in reviews}, theme_result.assignments, raw_by_id)
    actions: list[ActionIdea] = generate_actions(client, top, quotes)

    llm_themes = theme_result.method != METHOD_FALLBACK
    analysis = {
        "input": {
            "reviews_file": OUTPUT_FILE.name,
            "reviews_sha256": reviews_sha256,
            "n_analyzed": len(reviews),
            "n_quote_eligible": sum(r.quote_eligible for r in reviews),
            "first_review_at": min(r.created_at for r in reviews).isoformat(),
            "last_review_at": max(r.created_at for r in reviews).isoformat(),
        },
        "method": {
            "themes": theme_result.method,
            "fallback_reason": theme_result.fallback_reason,
            "provider": client.provider.name,
            "model": client.provider.model,
            "prompts": {
                "theme_discovery": ".".join(DISCOVERY_PROMPT) if llm_themes else None,
                "theme_assignment": ".".join(ASSIGNMENT_PROMPT) if llm_themes else None,
                "actions": ".".join(ACTIONS_PROMPT),
            },
            "ranking_formula": RANKING_FORMULA,
            "top_n": TOP_N,
            "owner_areas": list(OWNER_AREAS),
        },
        "themes": [_theme_dict(t) for t in ranked],
        "other": {
            **other_bucket(theme_result.assignments, reviews),
            "defaulted_from_missing_assignments": theme_result.defaulted_to_other,
        },
        "top_theme_ids": [t.theme_id for t in top],
        "assignments": dict(sorted(theme_result.assignments.items())),
        "quotes": [q.model_dump(mode="json") for q in quotes],
        "actions": [a.model_dump(mode="json") for a in actions],
    }
    result = AnalysisResult(
        run_id=run_id,
        created_at=datetime.now(UTC),
        analysis=analysis,
        llm_usage=client.usage.as_dict(),
        warnings=warnings,
        synthetic_fixture_data=any(SYNTHETIC_MARKER_RE.search(r.text) for r in reviews),
    )
    result.output_path = data_dir / "runs" / run_id / ANALYSIS_FILE
    atomic_write_lines(
        result.output_path, [json.dumps(result.as_dict(), indent=2, ensure_ascii=False)]
    )
    log.info(
        "Analysis finished",
        extra={
            "n_analyzed": len(reviews),
            "themes": len(ranked),
            "method": theme_result.method,
            "provider_calls": client.usage.provider_calls,
            "cache_hits": client.usage.cache_hits,
        },
    )
    return result
