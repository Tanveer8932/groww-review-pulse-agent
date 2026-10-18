"""Phase 4 report stage: ``runs/{analysis}/analysis.json`` -> ``runs/{run_id}/pulse.{md,txt,json}``.

The report stage makes **no LLM calls** and trusts nothing it reads. Before rendering, it
re-establishes every fact from stored data:

1. **Integrity.** ``analysis.json`` parses, has the expected schema version and structure,
   and its ``analysis_sha256`` matches the recomputed hash, so an edited file is refused.
2. **Origin.** Synthetic fixture analyses are refused unless a test passes
   ``allow_synthetic=True`` (the CLI never does). ``reviews.jsonl`` must be byte-identical to
   the file the analysis used (``reviews_sha256``) and pass the Phase 2 gate again.
3. **Sufficiency.** The analysed count matches ``reviews.jsonl`` and is at least
   ``ANALYSIS_MIN_REVIEWS``; there are exactly 3 top themes, 3 quotes, and 3 actions.
4. **Statistics.** Every theme's count, share, ratings, score, and rank is recomputed from
   the assignments and the reviews and must equal the stored value; the top 3 must be the
   first 3 of the recomputed ranking.
5. **Quotes.** Each quote is re-verified against ``reviews.jsonl`` and the raw store
   (verbatim span, text hash, theme membership, rating and date, eligibility, PII scan, and
   provenance) and must belong to a top theme.
6. **Actions.** The Phase 3 action validator runs again: one per top theme, in rank order,
   with only computed numbers and no personal data.
7. **Window.** The preprocessing funnel whose ``output_sha256`` equals ``reviews_sha256``
   supplies the reporting window and the in-window review count (``n_collected``).

The pulse is then rendered from the deterministic template (:mod:`.render`), shrinking
optional metadata only if the 250-word limit requires it, and every validator
(:mod:`.validators`) must pass. Nothing is written unless everything passes; any failure
raises :class:`ReportValidationError` listing each reason.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from groww_pulse.analysis.actions import (
    ActionItem,
    ActionsOutput,
    action_problems,
    allowed_numbers,
)
from groww_pulse.analysis.pipeline import ANALYSIS_FILE
from groww_pulse.analysis.pipeline import SCHEMA_VERSION as ANALYSIS_SCHEMA_VERSION
from groww_pulse.analysis.quotes import verify_quotes
from groww_pulse.analysis.ranking import TOP_N, rank_themes, theme_stats
from groww_pulse.analysis.themes import METHOD_FALLBACK, OTHER, ThemeDefinition
from groww_pulse.config import Settings
from groww_pulse.errors import AnalysisError, PreprocessingError, ReportValidationError
from groww_pulse.models import ActionIdea, Pulse, Quote, Review, Theme
from groww_pulse.preprocessing.normalize import count_words
from groww_pulse.preprocessing.pipeline import OUTPUT_FILE, RAW_STORE, load_analysis_reviews
from groww_pulse.report.render import (
    MAX_DETAIL_LEVEL,
    PulseContent,
    RenderedPulse,
    iso_week_label,
    pct,
    render,
)
from groww_pulse.report.validators import CheckResult, check_word_limit, validate_rendered
from groww_pulse.storage.jsonl_store import JsonlReviewStore, atomic_write_lines

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
PULSE_MD, PULSE_TXT, PULSE_JSON = "pulse.md", "pulse.txt", "pulse.json"
FUNNEL_FILE = "funnel.json"


# ---------------------------------------------------------------- input models
class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _AnalysisInput(_Loose):
    reviews_sha256: str
    n_analyzed: int
    first_review_at: datetime
    last_review_at: datetime


class _AnalysisMethod(_Loose):
    themes: str


class _StoredTheme(Theme):
    """A theme as stored in ``analysis.json``: the model plus its display percentages."""

    share_pct: str
    low_rating_pct: str


class _AnalysisBody(_Loose):
    input: _AnalysisInput
    method: _AnalysisMethod
    themes: list[_StoredTheme]
    top_theme_ids: list[str]
    assignments: dict[str, str]
    quotes: list[Quote]
    actions: list[ActionIdea]


class _AnalysisDocument(_Loose):
    schema_version: int
    run_id: str
    synthetic_fixture_data: bool
    analysis_sha256: str
    analysis: dict[str, Any]


class _FunnelWindow(_Loose):
    start: datetime
    end: datetime


class _FunnelStep(_Loose):
    step: str
    output: int


class _Funnel(_Loose):
    run_id: str
    window: _FunnelWindow
    steps: list[_FunnelStep]
    output_sha256: str


# ---------------------------------------------------------------- result
@dataclass
class ReportResult:
    run_id: str
    created_at: datetime
    pulse: Pulse
    rendered: RenderedPulse
    checks: list[CheckResult]
    source: dict[str, Any]
    synthetic_fixture_data: bool
    output_dir: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat(),
            "synthetic_fixture_data": self.synthetic_fixture_data,
            # Validated here; delivery (Phase 5) is a separate, explicit step.
            "publishable": not self.synthetic_fixture_data,
            "audit_marker": self.rendered.marker,
            "detail_level": self.rendered.detail_level,
            "source": self.source,
            "validators": [c.as_dict() for c in self.checks],
            "warnings": self.warnings,
            "pulse": self.pulse.model_dump(mode="json"),
        }

    def summary(self) -> dict[str, Any]:
        """Short view for the CLI: no review text."""
        return {
            "run_id": self.run_id,
            "output_dir": str(self.output_dir) if self.output_dir else None,
            "iso_week": self.pulse.iso_week,
            "window": [self.pulse.window_start.isoformat(), self.pulse.window_end.isoformat()],
            "n_analyzed": self.pulse.n_analyzed,
            "word_count": self.pulse.word_count,
            "content_hash": self.pulse.content_hash,
            "detail_level": self.rendered.detail_level,
            "validators": {c.name: c.passed for c in self.checks},
            "source_analysis": self.source["analysis_path"],
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------- loading
def find_latest_analysis(data_dir: Path) -> Path:
    """The ``analysis.json`` with the newest ``created_at`` (ties: path order)."""
    candidates: list[tuple[str, str, Path]] = []
    for path in sorted((data_dir / "runs").glob(f"*/{ANALYSIS_FILE}")):
        try:
            created = str(json.loads(path.read_text("utf-8")).get("created_at", ""))
        except (OSError, ValueError, AttributeError):
            created = ""  # still a candidate; loading it will report the problem
        candidates.append((created, str(path), path))
    if not candidates:
        raise ReportValidationError(
            f"no {ANALYSIS_FILE} found under {data_dir / 'runs'}; run `groww-pulse analyze` first"
        )
    return max(candidates)[2]


def analysis_sha256(analysis: dict[str, Any]) -> str:
    """Same canonical hash as :attr:`AnalysisResult.analysis_sha256` (Phase 3)."""
    canonical = json.dumps(analysis, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_document(path: Path) -> tuple[_AnalysisDocument, _AnalysisBody]:
    try:
        raw = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise ReportValidationError(f"{path} does not exist") from None
    except (OSError, ValueError) as exc:
        raise ReportValidationError(f"{path.name} is not readable JSON ({exc})") from None
    try:
        document = _AnalysisDocument.model_validate(raw)
    except ValidationError as exc:
        raise ReportValidationError(
            f"{path.name} is missing required fields ({exc.error_count()} errors)"
        ) from None
    if document.schema_version != ANALYSIS_SCHEMA_VERSION:
        raise ReportValidationError(
            f"{path.name} has schema_version {document.schema_version}; "
            f"expected {ANALYSIS_SCHEMA_VERSION}"
        )
    if analysis_sha256(document.analysis) != document.analysis_sha256:
        raise ReportValidationError(
            f"{path.name} failed its integrity check: analysis_sha256 does not match the "
            "content (the file was edited or corrupted)"
        )
    try:
        body = _AnalysisBody.model_validate(document.analysis)
    except ValidationError as exc:
        raise ReportValidationError(
            f"{path.name} does not have the expected analysis structure "
            f"({exc.error_count()} errors)"
        ) from None
    return document, body


def _find_funnel(data_dir: Path, reviews_sha256: str) -> tuple[Path, _Funnel]:
    matches: list[tuple[str, Path, _Funnel]] = []
    for path in sorted((data_dir / "runs").glob(f"*/{FUNNEL_FILE}")):
        try:
            funnel = _Funnel.model_validate_json(path.read_text("utf-8"))
        except (OSError, ValidationError):
            continue
        if funnel.output_sha256 == reviews_sha256:
            matches.append((funnel.run_id, path, funnel))
    if not matches:
        raise ReportValidationError(
            "no preprocessing funnel matches the analysed reviews.jsonl, so the reporting "
            "window cannot be established; re-run `groww-pulse preprocess` and `analyze`"
        )
    _, path, funnel = max(matches, key=lambda m: (str(m[1]), m[0]))
    return path, funnel


# ---------------------------------------------------------------- verification
def _close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def _theme_differences(stored: _StoredTheme, computed: Theme) -> list[str]:
    fields: list[str] = []
    if stored.review_ids != computed.review_ids:
        fields.append("review_ids")
    for name in ("count", "low_rating_count", "rank", "rating_counts"):
        if getattr(stored, name) != getattr(computed, name):
            fields.append(name)
    for name in ("share", "avg_rating", "low_rating_share", "score"):
        if not _close(getattr(stored, name), getattr(computed, name)):
            fields.append(name)
    if stored.share_pct != pct(computed.share):
        fields.append("share_pct")
    if stored.low_rating_pct != pct(computed.low_rating_share):
        fields.append("low_rating_pct")
    return fields


def _verify_statistics(
    body: _AnalysisBody, reviews: list[Review], problems: list[str]
) -> list[Theme]:
    """Recompute every theme statistic; return the verified top themes in rank order."""
    review_ids = {r.review_id for r in reviews}
    theme_ids = [t.theme_id for t in body.themes]
    if set(body.assignments) != review_ids:
        problems.append("assignments do not cover exactly the reviews in reviews.jsonl")
    unknown = {v for v in body.assignments.values() if v != OTHER and v not in theme_ids}
    if unknown:
        problems.append(f"assignments reference unknown theme(s): {', '.join(sorted(unknown))}")
    if len(set(theme_ids)) != len(theme_ids):
        problems.append("theme ids are not unique")

    definitions = [ThemeDefinition(t.theme_id, t.name, t.description) for t in body.themes]
    computed = rank_themes(theme_stats(definitions, body.assignments, reviews))
    by_id = {t.theme_id: t for t in computed}
    if [t.theme_id for t in body.themes] != [t.theme_id for t in computed]:
        problems.append("stored theme ranking differs from the recomputed ranking")
    for stored in body.themes:
        computed_theme = by_id.get(stored.theme_id)
        if computed_theme is None:
            problems.append(f"theme {stored.theme_id} has no members when recomputed")
            continue
        if differences := _theme_differences(stored, computed_theme):
            problems.append(
                f"theme {stored.theme_id} statistics differ from the recomputed values: "
                f"{', '.join(differences)}"
            )
    top = computed[:TOP_N]
    if len(top) < TOP_N:
        problems.append(f"only {len(top)} non-empty theme(s); the pulse needs exactly {TOP_N}")
    if body.top_theme_ids != [t.theme_id for t in top]:
        problems.append("top_theme_ids are not the first 3 themes of the recomputed ranking")
    return top


def _verify_quotes(
    body: _AnalysisBody, reviews: list[Review], data_dir: Path, top_ids: list[str]
) -> list[str]:
    problems = [
        f"quote {i} ({q.review_id}) is not from a top theme"
        for i, q in enumerate(body.quotes, 1)
        if q.theme_id not in top_ids
    ]
    raw_path = data_dir / RAW_STORE
    raw_by_id = JsonlReviewStore(raw_path).load() if raw_path.exists() else {}
    try:
        verify_quotes(body.quotes, {r.review_id: r for r in reviews}, body.assignments, raw_by_id)
    except AnalysisError as exc:
        problems.append(str(exc))
    return problems


def _verify_actions(body: _AnalysisBody, top: list[Theme]) -> list[str]:
    top_ids = [t.theme_id for t in top]
    output = ActionsOutput(
        actions=[
            ActionItem(
                theme_id=a.theme_id,
                action=a.action,
                owner_area=a.owner_area,
                rationale=a.rationale,
            )
            for a in body.actions
        ]
    )
    problems = [f"actions: {p}" for p in action_problems(output, top_ids, allowed_numbers(top))]
    if [a.theme_id for a in body.actions] != top_ids[: len(body.actions)]:
        problems.append("actions are not in theme rank order")
    return problems


def _raise_if(problems: list[str], prefix: str) -> None:
    if problems:
        raise ReportValidationError(f"{prefix}: " + "; ".join(problems))


def build_content(
    settings: Settings, analysis_path: Path, *, allow_synthetic: bool = False
) -> tuple[PulseContent, dict[str, Any], list[str]]:
    """Load and fully re-verify an analysis; return the pulse inputs, provenance, warnings."""
    data_dir = settings.data_dir
    document, body = _load_document(analysis_path)
    if document.synthetic_fixture_data and not allow_synthetic:
        raise ReportValidationError(
            f"{analysis_path.name} was produced from synthetic test fixtures; a pulse is "
            "only generated from real, permitted review data"
        )

    reviews_path = data_dir / OUTPUT_FILE
    if not reviews_path.exists():
        raise ReportValidationError(f"{reviews_path} does not exist; it is needed to verify")
    reviews_sha256 = hashlib.sha256(reviews_path.read_bytes()).hexdigest()
    if reviews_sha256 != body.input.reviews_sha256:
        raise ReportValidationError(
            "reviews.jsonl has changed since this analysis was produced; re-run "
            "`groww-pulse analyze` before reporting"
        )
    try:
        reviews = load_analysis_reviews(reviews_path, settings, allow_synthetic=allow_synthetic)
    except PreprocessingError as exc:
        raise ReportValidationError(f"reviews.jsonl failed the Phase 2 gate: {exc}") from None

    problems: list[str] = []
    if len(reviews) != body.input.n_analyzed:
        problems.append(
            f"n_analyzed is {body.input.n_analyzed} but reviews.jsonl has {len(reviews)} reviews"
        )
    if len(reviews) < settings.analysis_min_reviews:
        problems.append(
            f"only {len(reviews)} analysed review(s); ANALYSIS_MIN_REVIEWS is "
            f"{settings.analysis_min_reviews}"
        )
    _raise_if(problems, "Insufficient or inconsistent data")

    top = _verify_statistics(body, reviews, problems)
    _raise_if(problems, "Theme statistics failed verification")
    top_ids = [t.theme_id for t in top]
    _raise_if(_verify_quotes(body, reviews, data_dir, top_ids), "Quotes failed verification")
    _raise_if(_verify_actions(body, top), "Action ideas failed verification")

    funnel_path, funnel = _find_funnel(data_dir, reviews_sha256)
    window_start, window_end = funnel.window.start.date(), funnel.window.end.date()
    window_step = next((s for s in funnel.steps if s.step == "window"), None)
    if window_step is None:
        raise ReportValidationError(f"{funnel_path} has no window step")
    if not (
        funnel.window.start <= body.input.first_review_at
        and body.input.last_review_at <= funnel.window.end
    ):
        raise ReportValidationError("analysed reviews fall outside the preprocessing window")

    content = PulseContent(
        iso_week=iso_week_label(window_end),
        window_start=window_start,
        window_end=window_end,
        n_collected=window_step.output,
        n_analyzed=len(reviews),
        themes=tuple(top),
        quotes=tuple(body.quotes),
        actions=tuple(body.actions),
        fallback_clustering=body.method.themes == METHOD_FALLBACK,
        synthetic=document.synthetic_fixture_data,
    )
    source = {
        "analysis_path": str(analysis_path),
        "analysis_run_id": document.run_id,
        "analysis_sha256": document.analysis_sha256,
        "reviews_sha256": reviews_sha256,
        "funnel_path": str(funnel_path),
        "preprocess_run_id": funnel.run_id,
        "theme_method": body.method.themes,
    }
    warnings = (
        ["themes come from fallback clustering, not the LLM"] if content.fallback_clustering else []
    )
    return content, source, warnings


def render_within_limit(content: PulseContent) -> RenderedPulse:
    """The fullest rendering that fits the word limit (or the most compact one)."""
    rendered = render(content, 0)
    for level in range(1, MAX_DETAIL_LEVEL + 1):
        if check_word_limit(rendered).passed:
            break
        rendered = render(content, level)
    return rendered


def run_report(
    settings: Settings,
    *,
    run_id: str,
    analysis_path: Path | None = None,
    allow_synthetic: bool = False,
) -> ReportResult:
    """Verify an analysis, render the pulse, validate it, and write ``runs/{run_id}/pulse.*``.

    ``analysis_path`` defaults to the newest ``analysis.json``. Raises
    :class:`ReportValidationError` (and writes nothing) on any failed check.
    """
    path = analysis_path or find_latest_analysis(settings.data_dir)
    content, source, warnings = build_content(settings, path, allow_synthetic=allow_synthetic)
    rendered = render_within_limit(content)
    checks = validate_rendered(content, rendered, allow_synthetic=allow_synthetic)
    failed = [c for c in checks if not c.passed]
    if failed:
        raise ReportValidationError(
            "Pulse failed validation: " + "; ".join(f"{c.name}: {c.detail}" for c in failed)
        )
    if rendered.detail_level:
        warnings.append(
            f"optional metadata dropped to fit the word limit (detail level "
            f"{rendered.detail_level})"
        )

    pulse = Pulse(
        iso_week=content.iso_week,
        window_start=content.window_start,
        window_end=content.window_end,
        n_collected=content.n_collected,
        n_analyzed=content.n_analyzed,
        themes=list(content.themes),
        quotes=list(content.quotes),
        actions=list(content.actions),
        body_markdown=rendered.body_markdown,
        body_text=rendered.body_text,
        word_count=count_words(rendered.full_text),
        content_hash=rendered.content_hash,
    )
    result = ReportResult(
        run_id=run_id,
        created_at=datetime.now(UTC),
        pulse=pulse,
        rendered=rendered,
        checks=checks,
        source=source,
        synthetic_fixture_data=content.synthetic,
        warnings=warnings,
    )
    out_dir = settings.data_dir / "runs" / run_id
    atomic_write_lines(out_dir / PULSE_MD, [rendered.full_markdown.rstrip("\n")])
    atomic_write_lines(out_dir / PULSE_TXT, [rendered.full_text.rstrip("\n")])
    # pulse.json last: its presence means the report is complete.
    atomic_write_lines(
        out_dir / PULSE_JSON, [json.dumps(result.as_dict(), indent=2, ensure_ascii=False)]
    )
    result.output_dir = out_dir
    log.info(
        "Report finished",
        extra={
            "iso_week": pulse.iso_week,
            "word_count": pulse.word_count,
            "detail_level": rendered.detail_level,
        },
    )
    return result
