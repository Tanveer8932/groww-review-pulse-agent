# SYNTHETIC TEST FIXTURE — not real review data
"""Builders for the Phase 4 report tests.

Everything here is synthetic. :func:`pulse_content` builds in-memory pulse inputs for the
renderer and validator tests; :func:`build_analysis` runs the real Phase 2 and Phase 3
pipelines on the labelled corpus (``tests/fixtures/analysis.py``) with a fake LLM, in a
temporary ``DATA_DIR``. None of it is real Groww review data or may be published.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from groww_pulse.analysis.pipeline import run_analysis
from groww_pulse.config import Settings
from groww_pulse.models import ActionIdea, Quote, Theme
from groww_pulse.report.pipeline import analysis_sha256
from groww_pulse.report.render import PulseContent
from tests.fixtures.analysis import build_corpus, keyword_provider

QUOTE_TEXTS = (
    "SYNTHETIC FIXTURE orders lag at market open and my limit order filled late",
    "SYNTHETIC FIXTURE the login OTP never arrives so I cannot open the app",
    "SYNTHETIC FIXTURE my withdrawal has been pending for six days without any update",
)


def theme(theme_id: str, name: str, count: int, n_analyzed: int, avg: float, rank: int) -> Theme:
    return Theme(
        theme_id=theme_id,
        name=name,
        description="SYNTHETIC theme description",
        review_ids=[f"{theme_id}-r{i}" for i in range(count)],
        count=count,
        share=count / n_analyzed,
        avg_rating=avg,
        low_rating_count=count,
        low_rating_share=1.0,
        rating_counts={1: count, 2: 0, 3: 0, 4: 0, 5: 0},
        score=float(2 * count),
        rank=rank,
    )


def quote(i: int, text: str, *, lead: bool = False, trail: bool = False, rating: int = 1) -> Quote:
    start = 7 if lead else 0
    return Quote(
        review_id=f"q{i}",
        theme_id=f"T{i}",
        text=text,
        start=start,
        end=start + len(text),
        rating=rating,
        created_at=datetime(2026, 8, i, 10, 0, tzinfo=UTC),
        leading_ellipsis=lead,
        trailing_ellipsis=trail,
    )


def pulse_content(
    *,
    names: tuple[str, str, str] = (
        "Order execution lag",
        "Login and OTP failures",
        "Slow withdrawals",
    ),
    quote_texts: tuple[str, ...] = QUOTE_TEXTS,
    actions: tuple[str, ...] = (
        "Fix order routing delays at market open",
        "Repair OTP delivery for login",
        "Speed up withdrawal settlement",
    ),
    n_analyzed: int = 40,
    synthetic: bool = True,
    fallback: bool = False,
    trail_first: bool = False,
) -> PulseContent:
    counts = (12, 9, 7)
    return PulseContent(
        iso_week="2026-W42",
        window_start=date(2026, 7, 23),
        window_end=date(2026, 10, 14),
        n_collected=55,
        n_analyzed=n_analyzed,
        themes=tuple(
            theme(f"T{i}", names[i - 1], counts[i - 1], n_analyzed, 1.5 + i / 10, i)
            for i in (1, 2, 3)
        ),
        quotes=tuple(
            quote(i, text, trail=trail_first and i == 1) for i, text in enumerate(quote_texts, 1)
        ),
        actions=tuple(
            ActionIdea(theme_id=f"T{i}", action=a, owner_area="Engineering", rationale="")
            for i, a in enumerate(actions, 1)
        ),
        fallback_clustering=fallback,
        synthetic=synthetic,
    )


def build_analysis(settings: Settings, run_id: str = "analysis-run") -> Path:
    """Real Phase 2 + Phase 3 on the labelled corpus; returns the analysis.json path."""
    build_corpus(settings)
    result = run_analysis(
        settings, run_id=run_id, provider=keyword_provider(), allow_synthetic=True
    )
    assert result.output_path is not None
    return result.output_path


def rewrite_analysis(
    path: Path, mutate: Callable[[dict[str, Any]], None], *, rehash: bool = True
) -> None:
    """Apply ``mutate`` to the ``analysis`` object; re-sign it unless ``rehash=False``.

    Re-signing simulates a consistent-looking but wrong file, so the semantic checks (not
    just the integrity hash) are exercised.
    """
    document = json.loads(path.read_text("utf-8"))
    mutate(document["analysis"])
    if rehash:
        document["analysis_sha256"] = analysis_sha256(document["analysis"])
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")


def build_pulse(settings: Settings, run_id: str = "report-run") -> Path:
    """Real Phases 2-4 on the labelled corpus; returns the run directory with pulse.*.

    The pulse carries the synthetic banner and ``publishable: false``; only tests that pass
    ``allow_synthetic=True`` can load it.
    """
    from groww_pulse.report.pipeline import run_report

    build_analysis(settings)
    result = run_report(settings, run_id=run_id, allow_synthetic=True)
    assert result.output_dir is not None
    return result.output_dir


def build_variant(pulse_dir: Path, run_id: str) -> Path:
    """A second, fully valid pulse for the same ISO week with a different content hash.

    Copies ``pulse_dir`` to ``runs/{run_id}``, re-renders the stored content at another
    detail level, and re-signs it (hash, marker, ``pulse.txt``/``.md``), with a later
    ``created_at`` so it is the newest pulse. Models a report re-run for a delivered week.
    """
    import shutil

    from groww_pulse.analysis.themes import METHOD_FALLBACK
    from groww_pulse.models import Pulse
    from groww_pulse.preprocessing.normalize import count_words
    from groww_pulse.report.render import MAX_DETAIL_LEVEL, audit_marker, render

    target = pulse_dir.parent / run_id
    shutil.copytree(pulse_dir, target)
    data = json.loads((target / "pulse.json").read_text("utf-8"))
    pulse = Pulse.model_validate(data["pulse"])
    content = PulseContent(
        iso_week=pulse.iso_week,
        window_start=pulse.window_start,
        window_end=pulse.window_end,
        n_collected=pulse.n_collected,
        n_analyzed=pulse.n_analyzed,
        themes=tuple(pulse.themes),
        quotes=tuple(pulse.quotes),
        actions=tuple(pulse.actions),
        fallback_clustering=data["source"].get("theme_method") == METHOD_FALLBACK,
        synthetic=data["synthetic_fixture_data"],
    )
    level = MAX_DETAIL_LEVEL if data["detail_level"] != MAX_DETAIL_LEVEL else 0
    rendered = render(content, level)
    digest = rendered.content_hash
    assert digest != pulse.content_hash, "the variant must differ from the original"
    marker = audit_marker(pulse.iso_week, digest)
    data["run_id"] = run_id
    data["created_at"] = datetime.now(UTC).isoformat()
    data["detail_level"] = level
    data["audit_marker"] = marker
    data["pulse"].update(
        body_text=rendered.body_text,
        body_markdown=rendered.body_markdown,
        content_hash=digest,
        word_count=count_words(rendered.full_text),
    )
    (target / "pulse.json").write_text(json.dumps(data), encoding="utf-8")
    (target / "pulse.txt").write_text(f"{marker}\n{rendered.body_text}", encoding="utf-8")
    (target / "pulse.md").write_text(f"{marker}\n\n{rendered.body_markdown}", encoding="utf-8")
    return target
