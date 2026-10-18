# SYNTHETIC TEST FIXTURE — not real review data
"""Builders for synthetic raw-store records used by the Phase 2 tests.

Every pipeline-level text produced here starts with ``SYNTHETIC FIXTURE`` so it can never be
mistaken for a real Groww review, and the pipeline refuses it unless a test explicitly
passes ``allow_synthetic=True``. Files are written only under pytest's temporary
directories.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from groww_pulse.models import Provenance, RawReview, ReviewSourceType
from tests.fixtures.synthetic import APP_ID, IN_WINDOW

PREFIX = "SYNTHETIC FIXTURE"
INGESTED_AT = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def raw_review(
    review_id: str,
    text: str,
    *,
    rating: int = 2,
    created_at: datetime = IN_WINDOW,
    app_id: str = APP_ID,
    prefix: bool = True,
) -> RawReview:
    body = f"{PREFIX} {text}" if prefix else text
    return RawReview(
        review_id=review_id,
        source=ReviewSourceType.GOOGLE_PLAY_IMPORT,
        app_id=app_id,
        rating=rating,
        text=body,
        original_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        created_at=created_at,
        app_version="9.9.9-synthetic",
        provenance=Provenance(
            source_file="synthetic.csv", ingested_at=INGESTED_AT, run_id="synthetic-run"
        ),
    )


def write_raw_store(
    data_dir: Path, reviews: Iterable[RawReview], extra_lines: Iterable[str] = ()
) -> Path:
    path = data_dir / "raw" / "reviews_raw.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r.model_dump(mode="json"), ensure_ascii=False) for r in reviews]
    lines.extend(extra_lines)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def verhoeff_number(prefix11: str) -> str:
    """Append the Verhoeff check digit, giving a checksum-valid 12-digit number."""
    from groww_pulse.preprocessing.pii import verhoeff_valid

    for digit in "0123456789":
        if verhoeff_valid(prefix11 + digit):
            return prefix11 + digit
    raise AssertionError("unreachable: exactly one check digit is always valid")


def luhn_number(prefix: str) -> str:
    """Append the Luhn check digit."""
    from groww_pulse.preprocessing.pii import luhn_valid

    for digit in "0123456789":
        if luhn_valid(prefix + digit):
            return prefix + digit
    raise AssertionError("unreachable")
