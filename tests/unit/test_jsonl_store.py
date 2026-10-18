# SYNTHETIC TEST FIXTURE — not real review data
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from groww_pulse.errors import CollectionError
from groww_pulse.models import Provenance, RawReview, ReviewSourceType
from groww_pulse.storage.jsonl_store import JsonlReviewStore, append_jsonl, atomic_write_lines

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def review(
    review_id: str, *, minutes: int = 0, text: str = "SYNTHETIC FIXTURE text", **kw: Any
) -> RawReview:
    return RawReview(
        review_id=review_id,
        source=ReviewSourceType.GOOGLE_PLAY_IMPORT,
        app_id="com.nextbillion.groww",
        rating=kw.pop("rating", 3),
        text=text,
        original_sha256=hashlib.sha256(text.encode()).hexdigest(),
        created_at=T0 + timedelta(minutes=minutes),
        provenance=Provenance(
            source_file="synthetic.csv", ingested_at=T0, run_id=kw.pop("run_id", "r1")
        ),
        **kw,
    )


def test_write_is_sorted_and_deterministic(tmp_path: Path) -> None:
    store = JsonlReviewStore(tmp_path / "raw" / "reviews_raw.jsonl")
    store.write([review("b", minutes=1), review("a", minutes=1), review("c", minutes=0)])
    assert [r.review_id for r in store] == ["c", "a", "b"]
    first = store.path.read_bytes()
    store.write(reversed(list(store)))
    assert store.path.read_bytes() == first
    assert list(tmp_path.joinpath("raw").glob("*.tmp")) == []


def test_upsert_counts(tmp_path: Path) -> None:
    store = JsonlReviewStore(tmp_path / "s.jsonl")
    assert store.upsert([review("a"), review("b")]).added == 2
    result = store.upsert(
        [
            review("a", run_id="r2"),  # same content, different provenance: duplicate
            review("b", text="SYNTHETIC FIXTURE edited", updated_at=T0 + timedelta(days=1)),
            review("c"),
        ]
    )
    assert (result.added, result.updated, result.duplicates, result.conflicts, result.total) == (
        1,
        1,
        1,
        0,
        3,
    )
    assert store.load()["a"].provenance.run_id == "r1"  # original provenance kept


def test_conflicting_older_record_is_kept_out(tmp_path: Path) -> None:
    store = JsonlReviewStore(tmp_path / "s.jsonl")
    store.upsert([review("a", text="SYNTHETIC FIXTURE newer", updated_at=T0 + timedelta(days=2))])
    result = store.upsert([review("a", text="SYNTHETIC FIXTURE older")])
    assert result.conflicts == 1
    assert store.load()["a"].text == "SYNTHETIC FIXTURE newer"


def test_unchanged_upsert_does_not_rewrite(tmp_path: Path) -> None:
    store = JsonlReviewStore(tmp_path / "s.jsonl")
    store.upsert([review("a")])
    mtime = store.path.stat().st_mtime_ns
    store.upsert([review("a")])
    assert store.path.stat().st_mtime_ns == mtime


def test_corrupt_store_line_raises(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text('{"review_id": "x"}\n', encoding="utf-8")
    with pytest.raises(CollectionError, match="line 1"):
        JsonlReviewStore(path).load()


def test_missing_store_is_empty(tmp_path: Path) -> None:
    assert JsonlReviewStore(tmp_path / "none.jsonl").load() == {}


def test_atomic_write_failure_keeps_previous_file(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    atomic_write_lines(path, ["original"])

    def exploding() -> Any:
        yield "partial"
        raise RuntimeError("disk full (simulated)")

    with pytest.raises(RuntimeError):
        atomic_write_lines(path, exploding())
    assert path.read_text(encoding="utf-8") == "original\n"
    assert [p.name for p in tmp_path.iterdir()] == ["s.jsonl"]


def test_append_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "q" / "run.jsonl"
    assert append_jsonl(path, [{"b": 1, "a": "é"}]) == 1
    assert append_jsonl(path, [{"c": 2}]) == 1
    assert path.read_text(encoding="utf-8").splitlines() == ['{"a": "é", "b": 1}', '{"c": 2}']


def test_raw_review_requires_aware_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        review("a").model_copy(update={}).model_validate(
            {**review("a").model_dump(), "created_at": datetime(2026, 9, 1)}
        )
