"""Theme statistics and transparent ranking (architecture §6.4, FR-12).

All numbers are computed here from the assignments; none come from the LLM.

``score = count × (1 + low_rating_share)``, where ``low_rating_share`` is the share of a
theme's reviews rated 1 or 2 stars. Because ``count × low_rating_share`` is exactly the
number of 1-2★ members, the score is computed as the integer ``count + low_rating_count``,
which avoids floating-point ties. Ties break by higher ``count``, then by ``theme_id``
(ascending). ``other`` is never ranked; themes with no members are dropped.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from groww_pulse.analysis.themes import OTHER, ThemeDefinition
from groww_pulse.models import Review, Theme

TOP_N = 3
LOW_RATINGS = frozenset({1, 2})
RANKING_FORMULA = (
    "score = count × (1 + low_rating_share), where low_rating_share is the share of the "
    "theme's reviews rated 1-2 stars; ties break by higher count, then theme_id"
)


def theme_stats(
    definitions: Sequence[ThemeDefinition],
    assignments: Mapping[str, str],
    reviews: Sequence[Review],
) -> list[Theme]:
    """One :class:`Theme` per definition with code-computed counts, shares, and ratings."""
    n_analyzed = len(reviews)
    by_theme: dict[str, list[Review]] = {d.theme_id: [] for d in definitions}
    for review in reviews:
        theme_id = assignments.get(review.review_id, OTHER)
        if theme_id in by_theme:
            by_theme[theme_id].append(review)
    themes = []
    for definition in definitions:
        members = by_theme[definition.theme_id]
        count = len(members)
        low = sum(r.rating in LOW_RATINGS for r in members)
        themes.append(
            Theme(
                theme_id=definition.theme_id,
                name=definition.name,
                description=definition.description,
                review_ids=sorted(r.review_id for r in members),
                count=count,
                share=count / n_analyzed if n_analyzed else 0.0,
                avg_rating=sum(r.rating for r in members) / count if count else None,
                low_rating_count=low,
                low_rating_share=low / count if count else 0.0,
                rating_counts={s: sum(r.rating == s for r in members) for s in range(1, 6)},
                score=float(count + low),
            )
        )
    return themes


def rank_themes(themes: Sequence[Theme]) -> list[Theme]:
    """Non-empty themes sorted by score, with ``rank`` set (1 = highest)."""
    ranked = sorted(
        (t for t in themes if t.count > 0), key=lambda t: (-t.score, -t.count, t.theme_id)
    )
    return [t.model_copy(update={"rank": i}) for i, t in enumerate(ranked, 1)]


def other_bucket(assignments: Mapping[str, str], reviews: Sequence[Review]) -> dict[str, float]:
    count = sum(assignments.get(r.review_id, OTHER) == OTHER for r in reviews)
    return {"count": count, "share": count / len(reviews) if reviews else 0.0}
