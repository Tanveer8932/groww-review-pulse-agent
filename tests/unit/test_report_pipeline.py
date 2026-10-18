# SYNTHETIC TEST FIXTURE — not real review data
"""Phase 4 report stage on real Phase 2/3 output from the labelled synthetic corpus."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from groww_pulse.config import Settings, load_settings
from groww_pulse.errors import ReportValidationError
from groww_pulse.models import Review
from groww_pulse.preprocessing.normalize import count_words
from groww_pulse.preprocessing.pii import scan_pii
from groww_pulse.preprocessing.pipeline import load_analysis_reviews
from groww_pulse.report.pipeline import (
    PULSE_JSON,
    PULSE_MD,
    PULSE_TXT,
    ReportResult,
    find_latest_analysis,
    run_report,
)
from groww_pulse.report.render import quote_display
from tests.fixtures.report import build_analysis, rewrite_analysis


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(env_file=None, data_dir=tmp_path / "data", log_level="WARNING")


@pytest.fixture
def analysis_path(settings: Settings) -> Path:
    return build_analysis(settings)


def report(settings: Settings, run_id: str = "report-1", **kwargs: Any) -> ReportResult:
    kwargs.setdefault("allow_synthetic", True)
    return run_report(settings, run_id=run_id, **kwargs)


def pulse_dirs(settings: Settings) -> list[Path]:
    return sorted(p.parent for p in (settings.data_dir / "runs").glob(f"*/{PULSE_JSON}"))


def reviews(settings: Settings) -> dict[str, Review]:
    loaded = load_analysis_reviews(
        settings.data_dir / "reviews.jsonl", settings, allow_synthetic=True
    )
    return {r.review_id: r for r in loaded}


# ---------------------------------------------------------------- success
@pytest.mark.usefixtures("analysis_path")
def test_report_end_to_end(settings: Settings) -> None:
    result = report(settings)
    out = settings.data_dir / "runs" / "report-1"
    assert result.output_dir == out
    text = (out / PULSE_TXT).read_text("utf-8")
    markdown = (out / PULSE_MD).read_text("utf-8")
    data = json.loads((out / PULSE_JSON).read_text("utf-8"))
    pulse = result.pulse

    # Files: marker line, then the hashed body.
    assert text == f"{result.rendered.marker}\n{pulse.body_text}"
    assert markdown == f"{result.rendered.marker}\n\n{pulse.body_markdown}"
    assert pulse.content_hash == hashlib.sha256(pulse.body_text.encode()).hexdigest()
    assert result.rendered.marker == f"[groww-pulse:{pulse.iso_week}:{pulse.content_hash[:8]}]"

    # Header facts: ISO week, window, counts.
    assert pulse.iso_week == "2026-W42"
    assert (str(pulse.window_start), str(pulse.window_end)) == ("2026-07-23", "2026-10-14")
    assert pulse.n_analyzed == 27
    assert "Window: 2026-07-23 to 2026-10-14 · 27 reviews analyzed (of 27 collected)" in text

    # Exactly 3 themes, 3 quotes, 3 actions; at most 250 words.
    assert (len(pulse.themes), len(pulse.quotes), len(pulse.actions)) == (3, 3, 3)
    assert pulse.word_count == count_words(text) <= 250
    assert count_words(markdown) <= 250

    # Theme lines carry the verified counts and percentages, in rank order.
    for rank, theme in enumerate(pulse.themes, 1):
        assert theme.rank == rank
        assert f"{rank}. {theme.name} — {theme.count} reviews ({theme.share * 100:.1f}%)" in text

    # Quotes are verbatim spans of stored reviews, shown with ellipses outside the span.
    stored = reviews(settings)
    for quote in pulse.quotes:
        assert stored[quote.review_id].text[quote.start : quote.end] == quote.text
        assert quote_display(quote) in text
        assert quote_display(quote) in markdown

    # No PII; the planted phone number never appears.
    assert scan_pii(text) == {} == scan_pii(markdown)
    assert "9876543210" not in text

    # pulse.json records provenance and every validator.
    assert data["synthetic_fixture_data"] is True
    assert data["publishable"] is False
    assert all(v["passed"] for v in data["validators"])
    assert data["source"]["analysis_run_id"] == "analysis-run"
    assert (
        data["source"]["reviews_sha256"]
        == hashlib.sha256((settings.data_dir / "reviews.jsonl").read_bytes()).hexdigest()
    )
    assert data["pulse"]["content_hash"] == pulse.content_hash


@pytest.mark.usefixtures("analysis_path")
def test_report_is_deterministic(settings: Settings) -> None:
    first, second = report(settings, "r1"), report(settings, "r2")
    assert first.pulse.content_hash == second.pulse.content_hash
    for name in (PULSE_TXT, PULSE_MD):
        assert (first.output_dir / name).read_bytes() == (second.output_dir / name).read_bytes()  # type: ignore[operator]


def test_find_latest_analysis_uses_created_at(settings: Settings, analysis_path: Path) -> None:
    older = analysis_path.parent.parent / "a-older" / "analysis.json"
    older.parent.mkdir()
    document = json.loads(analysis_path.read_text("utf-8"))
    document["created_at"] = "2000-01-01T00:00:00+00:00"
    older.write_text(json.dumps(document), encoding="utf-8")
    assert find_latest_analysis(settings.data_dir) == analysis_path


# ---------------------------------------------------------------- refusals (nothing written)
def assert_refused(settings: Settings, match: str, **kwargs: Any) -> None:
    with pytest.raises(ReportValidationError, match=match):
        report(settings, **kwargs)
    assert pulse_dirs(settings) == []


@pytest.mark.usefixtures("analysis_path")
def test_synthetic_analysis_is_refused_for_production(settings: Settings) -> None:
    assert_refused(settings, "synthetic test fixtures", allow_synthetic=False)


def test_missing_analysis_is_refused(settings: Settings) -> None:
    assert_refused(settings, "no analysis.json found")


def test_corrupted_json_is_refused(settings: Settings, analysis_path: Path) -> None:
    analysis_path.write_text("{not json", encoding="utf-8")
    assert_refused(settings, "not readable JSON")


def test_missing_fields_are_refused(settings: Settings, analysis_path: Path) -> None:
    analysis_path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    assert_refused(settings, "missing required fields")


def test_wrong_schema_version_is_refused(settings: Settings, analysis_path: Path) -> None:
    document = json.loads(analysis_path.read_text("utf-8"))
    document["schema_version"] = 99
    analysis_path.write_text(json.dumps(document), encoding="utf-8")
    assert_refused(settings, "schema_version 99")


def test_edit_without_rehash_fails_integrity(settings: Settings, analysis_path: Path) -> None:
    rewrite_analysis(analysis_path, lambda a: a["themes"][0].update(count=99), rehash=False)
    assert_refused(settings, "integrity check")


def test_broken_structure_is_refused(settings: Settings, analysis_path: Path) -> None:
    rewrite_analysis(analysis_path, lambda a: a.pop("quotes"))
    assert_refused(settings, "expected analysis structure")


def _set_theme(index: int, **values: Any) -> Callable[[dict[str, Any]], None]:
    return lambda a: a["themes"][index].update(values)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (_set_theme(0, count=7), "statistics differ.*count"),
        (_set_theme(1, share_pct="40.0"), "statistics differ.*share_pct"),
        (_set_theme(2, avg_rating=4.9), "statistics differ.*avg_rating"),
        (_set_theme(0, rank=2), "statistics differ.*rank"),
        (lambda a: a["top_theme_ids"].reverse(), "top_theme_ids"),
        (lambda a: a["themes"].reverse(), "ranking differs"),
        (lambda a: a["assignments"].popitem(), "assignments do not cover"),
        (
            lambda a: a["assignments"].update({next(iter(a["assignments"])): "T9"}),
            "unknown theme",
        ),
    ],
)
def test_fabricated_statistics_are_refused(
    settings: Settings,
    analysis_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    match: str,
) -> None:
    rewrite_analysis(analysis_path, mutate)
    assert_refused(settings, match)


def _edit_quote(a: dict[str, Any]) -> None:
    q = a["quotes"][0]
    q["text"] = q["text"].replace("slowly", "slow!!")
    q["end"] = q["start"] + len(q["text"])


def _quote_from_non_top_theme(a: dict[str, Any]) -> None:
    other_theme = next(t for t in a["themes"] if t["theme_id"] not in a["top_theme_ids"])
    a["quotes"][0]["theme_id"] = other_theme["theme_id"]


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (_edit_quote, "not the verbatim span"),
        (lambda a: a["quotes"].pop(), "expected exactly 3 quotes, got 2"),
        (lambda a: a["quotes"].append(a["quotes"][0]), "expected exactly 3 quotes, got 4"),
        (_quote_from_non_top_theme, "not from a top theme"),
        (lambda a: a["quotes"][0].update(review_id="no-such-review"), "not found"),
        (lambda a: a["quotes"][1].update(rating=5), "rating or date differs"),
        (lambda a: a["quotes"][1].update(trailing_ellipsis=True), "ellipsis flags"),
    ],
)
def test_unverifiable_quotes_are_refused(
    settings: Settings,
    analysis_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    match: str,
) -> None:
    rewrite_analysis(analysis_path, mutate)
    assert_refused(settings, match)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda a: a["actions"].pop(), "expected exactly 3 actions"),
        (
            lambda a: a["actions"][0].update(action="Fix the order lag within 48 hours"),
            "numbers not in the computed statistics: 48",
        ),
        (
            lambda a: a["actions"][0].update(action="Call the reviewer on 9876543210"),
            "personal data",
        ),
        (lambda a: a["actions"].reverse(), "rank order"),
        (lambda a: a["actions"][0].update(theme_id="T5"), "one action per theme"),
    ],
)
def test_invalid_actions_are_refused(
    settings: Settings,
    analysis_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    match: str,
) -> None:
    rewrite_analysis(analysis_path, mutate)
    assert_refused(settings, match)


@pytest.mark.usefixtures("analysis_path")
def test_changed_reviews_file_is_refused(settings: Settings) -> None:
    path = settings.data_dir / "reviews.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    assert_refused(settings, "reviews.jsonl has changed")


@pytest.mark.usefixtures("analysis_path")
def test_missing_reviews_file_is_refused(settings: Settings) -> None:
    (settings.data_dir / "reviews.jsonl").unlink()
    assert_refused(settings, "does not exist")


@pytest.mark.usefixtures("analysis_path")
def test_missing_raw_store_fails_provenance(settings: Settings) -> None:
    (settings.data_dir / "raw" / "reviews_raw.jsonl").unlink()
    assert_refused(settings, "provenance check failed")


@pytest.mark.usefixtures("analysis_path")
def test_tampered_raw_store_fails_provenance(settings: Settings) -> None:
    raw = settings.data_dir / "raw" / "reviews_raw.jsonl"
    raw.write_text(raw.read_text("utf-8").replace("slowly", "quickly"), encoding="utf-8")
    assert_refused(settings, "provenance check failed")


@pytest.mark.usefixtures("analysis_path")
def test_missing_funnel_is_refused(settings: Settings) -> None:
    for funnel in (settings.data_dir / "runs").glob("*/funnel.json"):
        funnel.unlink()
    assert_refused(settings, "no preprocessing funnel")


def test_insufficient_real_data_is_refused(analysis_path: Path, tmp_path: Path) -> None:
    strict = load_settings(
        env_file=None, data_dir=tmp_path / "data", log_level="WARNING", analysis_min_reviews=50
    )
    with pytest.raises(ReportValidationError, match="ANALYSIS_MIN_REVIEWS is 50"):
        run_report(strict, run_id="r", analysis_path=analysis_path, allow_synthetic=True)


@pytest.mark.usefixtures("analysis_path")
def test_pii_in_rendered_report_fails_safely(settings: Settings) -> None:
    # A theme name is not recomputed from reviews, so plant PII there (and re-sign).
    analysis = find_latest_analysis(settings.data_dir)
    rewrite_analysis(analysis, _set_theme(0, name="Order lag for rahul@example.com"))
    with pytest.raises(ReportValidationError) as excinfo:
        report(settings)
    message = str(excinfo.value)
    assert "no_pii" in message
    assert "theme 1 name matches EMAIL" in message
    assert "rahul@example.com" not in message  # the value is never echoed
    assert pulse_dirs(settings) == []


def test_fallback_clustering_is_labelled(settings: Settings, analysis_path: Path) -> None:
    rewrite_analysis(analysis_path, lambda a: a["method"].update(themes="fallback_clustering"))
    result = report(settings)
    assert "Themes: fallback clustering." in result.pulse.body_text
    assert result.warnings == ["themes come from fallback clustering, not the LLM"]


# ---------------------------------------------------------------- more refusal paths
def test_explicit_missing_analysis_path_is_refused(settings: Settings, tmp_path: Path) -> None:
    assert_refused(settings, "does not exist", analysis_path=tmp_path / "missing.json")


def test_unreadable_funnel_files_are_skipped(settings: Settings, analysis_path: Path) -> None:
    junk = settings.data_dir / "runs" / "zz-junk" / "funnel.json"
    junk.parent.mkdir()
    junk.write_text("{broken", encoding="utf-8")
    assert report(settings).pulse.iso_week == "2026-W42"


def _move_members_to_other(theme_ranks: set[int]) -> Callable[[dict[str, Any]], None]:
    def mutate(a: dict[str, Any]) -> None:
        ids = {t["theme_id"] for t in a["themes"] if t["rank"] in theme_ranks}
        for review_id, theme_id in a["assignments"].items():
            if theme_id in ids:
                a["assignments"][review_id] = "other"

    return mutate


def _duplicate_theme_id(a: dict[str, Any]) -> None:
    a["themes"][4]["theme_id"] = a["themes"][3]["theme_id"]


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (_duplicate_theme_id, "theme ids are not unique"),
        (_move_members_to_other({5}), "has no members when recomputed"),
        (_move_members_to_other({3, 4, 5}), "only 2 non-empty theme"),
        (lambda a: a["input"].update(n_analyzed=28), "n_analyzed is 28"),
    ],
)
def test_inconsistent_analysis_is_refused(
    settings: Settings,
    analysis_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    match: str,
) -> None:
    rewrite_analysis(analysis_path, mutate)
    assert_refused(settings, match)


def test_reviews_failing_the_phase2_gate_are_refused(
    settings: Settings, analysis_path: Path
) -> None:
    path = settings.data_dir / "reviews.jsonl"
    lines = path.read_text("utf-8").splitlines()
    first = json.loads(lines[0])
    first["lang_confidence"] = 0.1
    lines[0] = json.dumps(first)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    new_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    rewrite_analysis(analysis_path, lambda a: a["input"].update(reviews_sha256=new_sha))
    assert_refused(settings, "failed the Phase 2 gate")


def _edit_funnel(settings: Settings, mutate: Callable[[dict[str, Any]], None]) -> None:
    path = next((settings.data_dir / "runs").glob("*/funnel.json"))
    funnel = json.loads(path.read_text("utf-8"))
    mutate(funnel)
    path.write_text(json.dumps(funnel), encoding="utf-8")


@pytest.mark.usefixtures("analysis_path")
def test_funnel_without_window_step_is_refused(settings: Settings) -> None:
    _edit_funnel(
        settings, lambda f: f.update(steps=[s for s in f["steps"] if s["step"] != "window"])
    )
    assert_refused(settings, "has no window step")


@pytest.mark.usefixtures("analysis_path")
def test_reviews_outside_the_funnel_window_are_refused(settings: Settings) -> None:
    _edit_funnel(settings, lambda f: f["window"].update(end="2026-08-05T23:59:59+00:00"))
    assert_refused(settings, "outside the preprocessing window")
