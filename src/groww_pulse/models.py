"""Canonical data models (architecture §5).

Phase 0 defines the shapes and field-level validation only. Behaviour (redaction, quote
extraction, rendering) is implemented by the owning layer in later phases.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReviewSourceType(StrEnum):
    GOOGLE_PLAY_IMPORT = "google_play_import"
    GOOGLE_PLAY_PUBLIC = "google_play_public"


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_file: str | None = None
    collector: str | None = None
    ingested_at: datetime
    run_id: str

    @model_validator(mode="after")
    def _one_origin(self) -> Provenance:
        if (self.source_file is None) == (self.collector is None):
            raise ValueError("exactly one of source_file or collector must be set")
        return self


def _require_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


class RawReview(BaseModel):
    """One line of ``data/raw/reviews_raw.jsonl`` (Phase 1 output).

    ``text`` is the review body exactly as exported: never edited, redacted, or trimmed here.
    Only these allowlisted fields are ever stored. Reviewer names, avatars, profile links,
    device, developer replies, and any other export columns are dropped at ingestion.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    review_id: str = Field(min_length=1)
    review_id_derived: bool = False
    source: ReviewSourceType
    app_id: str
    rating: int = Field(ge=1, le=5)
    title: str | None = None
    text: str = Field(min_length=1)
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    updated_at: datetime | None = None
    app_version: str | None = None
    thumbs_up: int | None = Field(default=None, ge=0)
    reviewer_language: str | None = None
    provenance: Provenance

    _utc = field_validator("created_at", "updated_at")(_require_utc)

    @property
    def last_modified(self) -> datetime:
        return self.updated_at or self.created_at

    def content_key(self) -> dict[str, object]:
        """Everything except provenance: used to detect true duplicates."""
        return self.model_dump(exclude={"provenance"})


class Review(BaseModel):
    """One line of ``data/reviews.jsonl``. ``text`` is always the redacted text.

    ``original_sha256`` links back to the unredacted text in the raw store; ``text_sha256``
    is the hash of ``text`` itself, so later stages can detect an edited record before
    verifying quotes against it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    review_id: str = Field(min_length=1)
    source: ReviewSourceType
    app_id: str
    rating: int = Field(ge=1, le=5)
    text: str = Field(min_length=1)
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    app_version: str | None = None
    thumbs_up: int | None = Field(default=None, ge=0)
    lang: str | None = None
    lang_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    word_count: int = Field(ge=0)
    quote_eligible: bool = True
    #: Why the original text must not be quoted verbatim (see preprocessing/integrity.py).
    quote_hold_reasons: list[str] = Field(default_factory=list)
    pii_redactions: dict[str, int] = Field(default_factory=dict)
    provenance: Provenance


class Theme(BaseModel):
    """A theme and its code-computed statistics.

    ``share`` is ``count / n_analyzed``; ``low_rating_share`` is the share of members rated
    1-2 stars; ``rating_counts`` maps every star value 1-5 to its member count. ``rank`` is
    1-based among ranked themes (``None`` until ranked).
    """

    model_config = ConfigDict(extra="forbid")

    theme_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    review_ids: list[str] = Field(default_factory=list)
    count: int = Field(default=0, ge=0)
    share: float = Field(default=0.0, ge=0.0, le=1.0)
    avg_rating: float | None = Field(default=None, ge=1.0, le=5.0)
    low_rating_count: int = Field(default=0, ge=0)
    low_rating_share: float = Field(default=0.0, ge=0.0, le=1.0)
    rating_counts: dict[int, int] = Field(default_factory=dict)
    score: float = Field(default=0.0, ge=0.0)
    rank: int | None = Field(default=None, ge=1)


class Quote(BaseModel):
    """A verbatim span of a stored review. Invariant: ``review.text[start:end] == text``.

    ``leading_ellipsis`` / ``trailing_ellipsis`` say the span does not start or end the
    review; a renderer adds the ellipsis *outside* the verbatim text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    review_id: str
    theme_id: str
    text: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    rating: int = Field(ge=1, le=5)
    created_at: datetime
    leading_ellipsis: bool = False
    trailing_ellipsis: bool = False

    @model_validator(mode="after")
    def _span_matches_length(self) -> Quote:
        if self.end - self.start != len(self.text):
            raise ValueError("end - start must equal len(text)")
        return self


class ActionIdea(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    theme_id: str
    action: str = Field(min_length=1)
    owner_area: str = ""
    rationale: str = ""


class Pulse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iso_week: str = Field(pattern=r"^\d{4}-W\d{2}$")
    window_start: date
    window_end: date
    n_collected: int = Field(ge=0)
    n_analyzed: int = Field(ge=0)
    themes: list[Theme] = Field(max_length=3)
    quotes: list[Quote] = Field(max_length=3)
    actions: list[ActionIdea] = Field(max_length=3)
    body_markdown: str = ""
    body_text: str = ""
    word_count: int = Field(default=0, ge=0)
    content_hash: str = ""


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    iso_week: str | None = None
    content_hash: str | None = None
    stage_status: dict[str, StageStatus] = Field(default_factory=dict)
    mcp_results: list[dict[str, object]] = Field(default_factory=list)
    created_at: datetime
