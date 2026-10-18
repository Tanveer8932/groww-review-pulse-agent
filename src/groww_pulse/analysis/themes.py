"""Theme discovery and assignment (architecture §6.4).

1. **Discovery** (``theme_discovery.v1``): a stratified sample (by star rating and ISO week,
   at most :data:`SAMPLE_LIMIT` reviews, ``id`` + redacted text only) goes to the LLM, which
   proposes up to 5 themes, each with 2-8 evidence review IDs from the sample. Code assigns
   the theme IDs (``T1``..``T5``). If the client has a per-request token limit (Groq free
   tier), the sample shrinks until the request fits (never below :data:`MIN_SAMPLE`).
2. **Assignment** (``theme_assignment.v1``): every review, in batches of
   :data:`BATCH_SIZE` (halved until each request fits the client's limit), is mapped to one
   theme ID or ``other``. Unknown or repeated review IDs
   are rejected (repair retry); reviews the LLM leaves out default to ``other``.
3. **Fallback**: if the provider is unavailable (:class:`LLMProviderError`), TF-IDF + KMeans
   clustering runs instead and the result is labelled ``fallback_clustering``.

``other`` is never a theme: it is not counted among the 5 and is never ranked.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import silhouette_score

from groww_pulse.analysis.llm import LLMClient, OutputRejectedError, PromptRequest
from groww_pulse.analysis.prompts import data_block, load_prompt
from groww_pulse.errors import InsufficientDataError, LLMProviderError
from groww_pulse.models import Review
from groww_pulse.preprocessing.normalize import count_words
from groww_pulse.preprocessing.pii import PLACEHOLDERS, scan_pii

log = logging.getLogger(__name__)

OTHER = "other"
MAX_THEMES = 5
SAMPLE_LIMIT = 300
MIN_SAMPLE = 15
SAMPLE_SHRINK = 0.85
BATCH_SIZE = 50
MAX_NAME_CHARS = 60
MAX_NAME_WORDS = 8
MAX_DESCRIPTION_WORDS = 40
MIN_EVIDENCE, MAX_EVIDENCE = 2, 8
DISCOVERY_PROMPT = ("theme_discovery", "v1")
ASSIGNMENT_PROMPT = ("theme_assignment", "v1")
#: Fallback k range. The pulse needs 3 themes, so k starts at 3 (architecture: 2-5).
FALLBACK_K_MIN, FALLBACK_K_MAX = 3, 5
FALLBACK_TERMS = 3

_CATCH_ALL = {"other", "others", "misc", "miscellaneous", "general", "various", "uncategorized"}
_DIGIT_RE = re.compile(r"\d")
_PLACEHOLDER_RE = re.compile(r"\[(?:" + "|".join(PLACEHOLDERS) + r")\]")

#: Words for TF-IDF: alphabetic runs of 2+ letters (placeholders and numbers carry no theme).
TERM_PATTERN = r"(?u)\b[a-zA-Z][a-zA-Z]+\b"

METHOD_LLM = "llm"
METHOD_FALLBACK = "fallback_clustering"


@dataclass(frozen=True)
class ThemeDefinition:
    theme_id: str
    name: str
    description: str
    evidence_review_ids: tuple[str, ...] = ()


@dataclass
class ThemeResult:
    definitions: list[ThemeDefinition]
    assignments: dict[str, str]  # review_id -> theme_id | "other"
    method: str
    defaulted_to_other: int = 0
    fallback_reason: str | None = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- LLM output models
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscoveredTheme(_Strict):
    name: str
    description: str
    evidence_review_ids: list[str]


class DiscoveryOutput(_Strict):
    themes: list[DiscoveredTheme]


class AssignmentItem(_Strict):
    review_id: str
    theme_id: str


class AssignmentOutput(_Strict):
    assignments: list[AssignmentItem]


def _discovery_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "themes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "evidence_review_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "description", "evidence_review_ids"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["themes"],
        "additionalProperties": False,
    }


def _assignment_schema(review_ids: Sequence[str], theme_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "review_id": {"type": "string", "enum": list(review_ids)},
                        "theme_id": {"type": "string", "enum": [*theme_ids, OTHER]},
                    },
                    "required": ["review_id", "theme_id"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["assignments"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------- sampling
def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stratified_sample(reviews: Sequence[Review], limit: int = SAMPLE_LIMIT) -> list[Review]:
    """Up to ``limit`` reviews, round-robin across (rating, ISO week) strata.

    Deterministic: strata are visited in sorted order, and each stratum is ordered by a
    hash of the review ID (a stable pseudo-random order). The sample is returned in input
    order.
    """
    if len(reviews) <= limit:
        return list(reviews)
    strata: dict[tuple[int, int, int], list[Review]] = defaultdict(list)
    for review in reviews:
        iso = review.created_at.isocalendar()
        strata[(review.rating, iso.year, iso.week)].append(review)
    queues = [
        sorted(members, key=lambda r: _stable_hash(r.review_id))
        for _, members in sorted(strata.items())
    ]
    chosen: set[str] = set()
    depth = 0
    while len(chosen) < limit:
        for queue in queues:
            if depth < len(queue) and len(chosen) < limit:
                chosen.add(queue[depth].review_id)
        depth += 1
    return [r for r in reviews if r.review_id in chosen]


def _review_records(reviews: Sequence[Review]) -> list[dict[str, str]]:
    return [{"id": r.review_id, "text": r.text} for r in reviews]


# ---------------------------------------------------------------- discovery
def _short(value: str, limit: int = 40) -> str:
    """LLM-invented IDs echoed in a repair prompt are truncated: they are model output."""
    return value if len(value) <= limit else value[:limit] + "..."


def _clean_label(value: str) -> str:
    return " ".join(value.split())


def _check_label(field_name: str, index: int, value: str) -> list[str]:
    problems = []
    if not value:
        problems.append(f"themes[{index}].{field_name} is empty")
    if _DIGIT_RE.search(value):
        problems.append(f"themes[{index}].{field_name} contains digits")
    if scan_pii(value) or _PLACEHOLDER_RE.search(value):
        problems.append(f"themes[{index}].{field_name} contains personal data or a placeholder")
    return problems


def discovery_validator(sample_ids: set[str]) -> Callable[[DiscoveryOutput], None]:
    def validate(output: DiscoveryOutput) -> None:
        problems: list[str] = []
        if not 1 <= len(output.themes) <= MAX_THEMES:
            problems.append(f"expected 1 to {MAX_THEMES} themes, got {len(output.themes)}")
        seen: set[str] = set()
        for i, theme in enumerate(output.themes):
            name, description = _clean_label(theme.name), _clean_label(theme.description)
            problems += _check_label("name", i, name)
            problems += _check_label("description", i, description)
            key = name.casefold()
            if key in seen:
                problems.append(f"themes[{i}].name duplicates an earlier theme name")
            seen.add(key)
            if key in _CATCH_ALL:
                problems.append(f"themes[{i}].name is a catch-all; use specific themes")
            if len(name) > MAX_NAME_CHARS or count_words(name) > MAX_NAME_WORDS:
                problems.append(
                    f"themes[{i}].name is longer than {MAX_NAME_CHARS} characters "
                    f"or {MAX_NAME_WORDS} words"
                )
            if count_words(description) > MAX_DESCRIPTION_WORDS:
                problems.append(
                    f"themes[{i}].description is longer than {MAX_DESCRIPTION_WORDS} words"
                )
            evidence = theme.evidence_review_ids
            if not MIN_EVIDENCE <= len(set(evidence)) <= MAX_EVIDENCE:
                problems.append(
                    f"themes[{i}] needs {MIN_EVIDENCE} to {MAX_EVIDENCE} distinct evidence ids"
                )
            unknown = sorted(set(evidence) - sample_ids)
            if unknown:
                problems.append(
                    f"themes[{i}].evidence_review_ids has {len(unknown)} id(s) not in the "
                    f"sample: {', '.join(_short(u) for u in unknown[:5])}"
                )
        if problems:
            raise OutputRejectedError("; ".join(problems))

    return validate


def discover_themes(
    client: LLMClient, reviews: Sequence[Review]
) -> tuple[list[ThemeDefinition], int]:
    """Ask the LLM for at most 5 themes; returns the definitions and the sample size."""
    template = load_prompt(*DISCOVERY_PROMPT)

    def build(sample: Sequence[Review]) -> PromptRequest:
        return PromptRequest(
            name=template.name,
            version=template.version,
            system=template.system,
            user=template.render_user(
                n_sample=str(len(sample)),
                n_total=str(len(reviews)),
                reviews=data_block("reviews", _review_records(sample)),
            ),
            schema=_discovery_schema(),
            max_tokens=16_000,
            effort="medium",
        )

    sample = stratified_sample(reviews)
    request = build(sample)
    while not client.fits(request) and len(sample) > MIN_SAMPLE:
        sample = stratified_sample(reviews, max(MIN_SAMPLE, int(len(sample) * SAMPLE_SHRINK)))
        request = build(sample)
    if len(sample) < min(len(reviews), SAMPLE_LIMIT):
        log.info(
            "Discovery sample reduced to fit the per-request token limit",
            extra={"n_sample": len(sample), "n_total": len(reviews)},
        )
    output = client.complete_json(
        request, DiscoveryOutput, discovery_validator({r.review_id for r in sample})
    )
    definitions = [
        ThemeDefinition(
            theme_id=f"T{i}",
            name=_clean_label(theme.name),
            description=_clean_label(theme.description),
            evidence_review_ids=tuple(dict.fromkeys(theme.evidence_review_ids)),
        )
        for i, theme in enumerate(output.themes, 1)
    ]
    return definitions, len(sample)


# ---------------------------------------------------------------- assignment
def assignment_validator(
    batch_ids: Sequence[str], theme_ids: set[str]
) -> Callable[[AssignmentOutput], None]:
    expected = set(batch_ids)

    def validate(output: AssignmentOutput) -> None:
        problems: list[str] = []
        seen: set[str] = set()
        unknown: list[str] = []
        repeated: list[str] = []
        bad_theme: list[str] = []
        for item in output.assignments:
            if item.review_id not in expected:
                unknown.append(item.review_id)
            elif item.review_id in seen:
                repeated.append(item.review_id)
            seen.add(item.review_id)
            if item.theme_id != OTHER and item.theme_id not in theme_ids:
                bad_theme.append(item.theme_id)
        if unknown:
            problems.append(
                f"{len(unknown)} unknown review id(s): {', '.join(_short(u) for u in unknown[:5])}"
            )
        if repeated:
            problems.append(f"review id(s) assigned more than once: {', '.join(repeated[:5])}")
        if bad_theme:
            problems.append(
                f"unknown theme id(s): {', '.join(_short(t) for t in sorted(set(bad_theme))[:5])}"
            )
        if problems:
            raise OutputRejectedError("; ".join(problems))

    return validate


def assign_themes(
    client: LLMClient, reviews: Sequence[Review], themes: Sequence[ThemeDefinition]
) -> tuple[dict[str, str], int]:
    """Map every review to a theme ID or ``other``; returns (assignments, defaulted count)."""
    template = load_prompt(*ASSIGNMENT_PROMPT)
    theme_ids = [t.theme_id for t in themes]
    themes_block = data_block(
        "themes", [{"id": t.theme_id, "name": t.name, "description": t.description} for t in themes]
    )

    def build(batch: Sequence[Review]) -> PromptRequest:
        return PromptRequest(
            name=template.name,
            version=template.version,
            system=template.system,
            user=template.render_user(
                themes=themes_block,
                n_reviews=str(len(batch)),
                reviews=data_block("reviews", _review_records(batch)),
            ),
            schema=_assignment_schema([r.review_id for r in batch], theme_ids),
            max_tokens=8_000,
            effort="low",
        )

    pending = [reviews[i : i + BATCH_SIZE] for i in range(0, len(reviews), BATCH_SIZE)]
    batches: list[tuple[Sequence[Review], PromptRequest]] = []
    while pending:
        batch = pending.pop(0)
        request = build(batch)
        if len(batch) > 1 and not client.fits(request):
            half = (len(batch) + 1) // 2
            pending[:0] = [batch[:half], batch[half:]]
        else:
            batches.append((batch, request))

    assignments: dict[str, str] = {}
    defaulted = 0
    for batch, request in batches:
        batch_ids = [r.review_id for r in batch]
        output = client.complete_json(
            request, AssignmentOutput, assignment_validator(batch_ids, set(theme_ids))
        )
        returned = {item.review_id: item.theme_id for item in output.assignments}
        for review_id in batch_ids:
            if review_id in returned:
                assignments[review_id] = returned[review_id]
            else:
                assignments[review_id] = OTHER
                defaulted += 1
    if defaulted:
        log.warning("Missing LLM assignments defaulted to other", extra={"n": defaulted})
    return assignments, defaulted


# ---------------------------------------------------------------- fallback
def tfidf_vectorizer() -> TfidfVectorizer:
    """The TF-IDF settings shared by fallback clustering and quote ranking."""
    return TfidfVectorizer(stop_words="english", token_pattern=TERM_PATTERN, sublinear_tf=True)


def _fallback_text(text: str) -> str:
    return _PLACEHOLDER_RE.sub(" ", text)


def fallback_cluster(reviews: Sequence[Review]) -> tuple[list[ThemeDefinition], dict[str, str]]:
    """TF-IDF + KMeans with k in 3-5 chosen by silhouette score (deterministic)."""
    n = len(reviews)
    if n < FALLBACK_K_MIN + 1:
        raise InsufficientDataError(f"fallback clustering needs at least 4 reviews, got {n}")
    vectorizer = tfidf_vectorizer()
    try:
        matrix = vectorizer.fit_transform([_fallback_text(r.text) for r in reviews])
    except ValueError:
        raise InsufficientDataError("fallback clustering found no usable terms") from None
    terms = vectorizer.get_feature_names_out()

    best: tuple[float, int, np.ndarray[Any, Any], np.ndarray[Any, Any]] | None = None
    for k in range(FALLBACK_K_MIN, min(FALLBACK_K_MAX, n - 1) + 1):
        model = KMeans(n_clusters=k, n_init=10, random_state=0).fit(matrix)
        labels = model.labels_
        if len(set(labels.tolist())) < 2:
            continue
        score = float(silhouette_score(matrix, labels, metric="cosine"))
        if best is None or score > best[0]:
            best = (score, k, labels, model.cluster_centers_)
    if best is None:
        raise InsufficientDataError("fallback clustering could not separate the reviews")
    _, k, labels, centers = best

    # Stable theme order: larger clusters first, then the smallest member review ID.
    members: dict[int, list[str]] = defaultdict(list)
    for review, label in zip(reviews, labels.tolist(), strict=True):
        members[int(label)].append(review.review_id)
    order = sorted(members, key=lambda c: (-len(members[c]), min(members[c])))

    definitions: list[ThemeDefinition] = []
    assignments: dict[str, str] = {}
    for i, cluster in enumerate(order, 1):
        top = [str(terms[j]) for j in np.argsort(-centers[cluster], kind="stable")[:FALLBACK_TERMS]]
        theme_id = f"T{i}"
        definitions.append(
            ThemeDefinition(
                theme_id=theme_id,
                name=f"Fallback: {' / '.join(top)}",
                description=(
                    "Fallback clustering without the LLM: reviews that share the terms "
                    f"{', '.join(top)}."
                ),
            )
        )
        for review_id in members[cluster]:
            assignments[review_id] = theme_id
    log.info("Fallback clustering finished", extra={"k": k})
    return definitions, assignments


# ---------------------------------------------------------------- entry point
def build_themes(client: LLMClient, reviews: Sequence[Review]) -> ThemeResult:
    """Discover and assign themes with the LLM, falling back to clustering if it is down."""
    try:
        definitions, sample_size = discover_themes(client, reviews)
        assignments, defaulted = assign_themes(client, reviews, definitions)
    except LLMProviderError as exc:
        log.warning("LLM unavailable; using fallback clustering", extra={"reason": str(exc)})
        definitions, assignments = fallback_cluster(reviews)
        return ThemeResult(
            definitions=definitions,
            assignments=assignments,
            method=METHOD_FALLBACK,
            fallback_reason=str(exc),
            warnings=[f"LLM unavailable, themes come from fallback clustering: {exc}"],
        )
    warnings = []
    if defaulted:
        warnings.append(f"{defaulted} review(s) missing from LLM assignments defaulted to other")
    log.info(
        "Themes discovered and assigned",
        extra={"themes": len(definitions), "sample": sample_size, "defaulted": defaulted},
    )
    return ThemeResult(
        definitions=definitions,
        assignments=assignments,
        method=METHOD_LLM,
        defaulted_to_other=defaulted,
        warnings=warnings,
    )
