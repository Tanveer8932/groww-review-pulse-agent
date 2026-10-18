"""Atomic JSONL storage for raw reviews, upserted by ``review_id`` (architecture §6.2).

Writes are deterministic: records are sorted by ``(created_at, review_id)`` and serialized
with sorted keys, so importing the same data twice yields a byte-identical file.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from groww_pulse.errors import CollectionError
from groww_pulse.models import RawReview

log = logging.getLogger(__name__)


@dataclass
class UpsertResult:
    added: int = 0
    updated: int = 0
    duplicates: int = 0
    conflicts: int = 0
    total: int = 0


def atomic_write_lines(path: Path, lines: Iterable[str]) -> None:
    """Write lines to ``path`` atomically: temp file in the same dir, fsync, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(line)
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def append_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    """Append JSON objects to ``path`` (used for quarantine); returns the number written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))
            handle.write("\n")
            count += 1
    return count


def _serialize(review: RawReview) -> str:
    return json.dumps(
        review.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def merge_review(current: dict[str, RawReview], incoming: RawReview, result: UpsertResult) -> None:
    """Upsert one review into ``current`` in place.

    * new ID: added;
    * same ID, identical content: duplicate (existing record and provenance kept);
    * same ID, incoming modified later (``updated_at``/``created_at``): replaced;
    * same ID, different content, not newer: conflict, existing kept.
    """
    existing = current.get(incoming.review_id)
    if existing is None:
        current[incoming.review_id] = incoming
        result.added += 1
    elif existing.content_key() == incoming.content_key():
        result.duplicates += 1
    elif incoming.last_modified > existing.last_modified:
        current[incoming.review_id] = incoming
        result.updated += 1
    else:
        result.conflicts += 1
        log.warning(
            "Conflicting record for existing review ID kept unchanged",
            extra={"review_id": incoming.review_id},
        )


class JsonlReviewStore:
    """``data/raw/reviews_raw.jsonl``: restricted, gitignored, original text."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __iter__(self) -> Iterator[RawReview]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    yield RawReview.model_validate_json(line)
                except ValidationError as exc:
                    raise CollectionError(
                        f"Corrupt record in {self.path.name} at line {line_no}: "
                        f"{exc.error_count()} validation error(s)"
                    ) from None

    def load(self) -> dict[str, RawReview]:
        return {review.review_id: review for review in self}

    def write(self, reviews: Iterable[RawReview]) -> None:
        ordered = sorted(reviews, key=lambda r: (r.created_at, r.review_id))
        atomic_write_lines(self.path, (_serialize(r) for r in ordered))

    def upsert(self, incoming: Iterable[RawReview]) -> UpsertResult:
        current = self.load()
        result = UpsertResult()
        for review in incoming:
            merge_review(current, review, result)
        if result.added or result.updated or not self.path.exists():
            self.write(current.values())
        result.total = len(current)
        return result
