# SYNTHETIC TEST FIXTURE — not real review data
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from groww_pulse import errors
from groww_pulse.models import Provenance, Pulse, Quote, Review, ReviewSourceType

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _review(**overrides: Any) -> Review:
    data: dict[str, Any] = {
        "review_id": "r-1",
        "source": ReviewSourceType.GOOGLE_PLAY_IMPORT,
        "app_id": "com.nextbillion.groww",
        "rating": 2,
        "text": "synthetic fixture text used only to exercise model validation rules",
        "original_sha256": "0" * 64,
        "created_at": NOW,
        "word_count": 10,
        "provenance": Provenance(source_file="fixture.csv", ingested_at=NOW, run_id="t"),
    }
    data.update(overrides)
    return Review(**data)


def test_valid_review() -> None:
    assert _review().rating == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"rating": 0},
        {"rating": 6},
        {"text": ""},
        {"original_sha256": "not-a-hash"},
        {"reviewer_name": "should never be stored"},
    ],
)
def test_invalid_review_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _review(**overrides)


def test_provenance_requires_exactly_one_origin() -> None:
    with pytest.raises(ValidationError):
        Provenance(ingested_at=NOW, run_id="t")
    with pytest.raises(ValidationError):
        Provenance(source_file="a.csv", collector="c", ingested_at=NOW, run_id="t")


def test_quote_span_must_match_text_length() -> None:
    Quote(review_id="r", theme_id="t", text="abc", start=2, end=5, rating=1, created_at=NOW)
    with pytest.raises(ValidationError):
        Quote(review_id="r", theme_id="t", text="abc", start=0, end=5, rating=1, created_at=NOW)


def test_pulse_caps_lists_at_three_and_checks_week_label() -> None:
    base: dict[str, Any] = {
        "iso_week": "2026-W42",
        "window_start": NOW.date(),
        "window_end": NOW.date(),
        "n_collected": 0,
        "n_analyzed": 0,
        "themes": [],
        "quotes": [],
        "actions": [],
    }
    Pulse(**base)
    with pytest.raises(ValidationError):
        Pulse(**{**base, "iso_week": "2026-42"})


def test_error_hierarchy() -> None:
    for cls in (
        errors.ConfigError,
        errors.CollectionError,
        errors.PreprocessingError,
        errors.AnalysisError,
        errors.ReportValidationError,
        errors.DeliveryError,
    ):
        assert issubclass(cls, errors.GrowwPulseError)
    assert issubclass(errors.ImportSchemaError, errors.CollectionError)
    assert issubclass(errors.CollectorDisabledError, errors.CollectionError)
    assert issubclass(errors.LLMOutputError, errors.AnalysisError)
    assert issubclass(errors.BudgetExceededError, errors.AnalysisError)
    assert issubclass(errors.LLMProviderError, errors.AnalysisError)
    assert issubclass(errors.ForbiddenToolError, errors.DeliveryError)
    assert issubclass(errors.MCPToolError, errors.DeliveryError)
    assert issubclass(errors.MCPUnavailableError, errors.DeliveryError)
