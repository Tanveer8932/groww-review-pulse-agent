"""Deduplication (architecture §6.3, step 6; FR-8).

Three passes, each keeping the earliest review (by ``created_at``, then ``review_id``):

a. ``duplicate_id``: the same ``review_id`` seen again;
b. ``exact_duplicate``: the same text after case-folding and whitespace collapsing;
c. ``near_duplicate``: among reviews with the **same rating**, a rapidfuzz token-set ratio
   of at least ``NEAR_DUPLICATE_THRESHOLD`` (95) *and* word counts within
   ``LENGTH_RATIO_MIN`` (0.8) of each other.

The length guard exists because token-set ratio scores 100 whenever one review's words are
a subset of another's, which would merge a short review into an unrelated longer one.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

import numpy as np
from rapidfuzz import fuzz, process, utils

from groww_pulse.preprocessing.normalize import count_words, dedupe_key

NEAR_DUPLICATE_THRESHOLD = 95
LENGTH_RATIO_MIN = 0.8

T = TypeVar("T")


@dataclass(frozen=True)
class DedupeItem(Generic[T]):
    """What dedupe needs to know about a review; ``payload`` is carried through untouched."""

    review_id: str
    sort_key: tuple[object, ...]
    rating: int
    text: str
    payload: T


@dataclass
class DedupeResult(Generic[T]):
    kept: list[DedupeItem[T]]
    dropped: list[tuple[str, str]]  # (review_id, reason), one entry per dropped item

    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for _, reason in self.dropped:
            counts[reason] += 1
        return dict(sorted(counts.items()))


def _drop_seen(
    items: Sequence[DedupeItem[T]],
    key: Callable[[DedupeItem[T]], str],
    reason: str,
    dropped: list[tuple[str, str]],
) -> list[DedupeItem[T]]:
    seen: set[str] = set()
    kept = []
    for item in items:
        value = key(item)
        if value in seen:
            dropped.append((item.review_id, reason))
        else:
            seen.add(value)
            kept.append(item)
    return kept


def _near_duplicates(items: Sequence[DedupeItem[T]]) -> set[int]:
    """Indices (into ``items``, already in keep-priority order) that duplicate an earlier one."""
    if len(items) < 2:
        return set()
    texts = [item.text for item in items]
    scores = process.cdist(
        texts,
        texts,
        scorer=fuzz.token_set_ratio,
        processor=utils.default_process,
        score_cutoff=NEAR_DUPLICATE_THRESHOLD,
        dtype=np.uint8,
        workers=1,  # deterministic and cheap enough for a 12-week window
    )
    lengths = np.array([max(count_words(t), 1) for t in texts], dtype=float)
    ratio = np.minimum.outer(lengths, lengths) / np.maximum.outer(lengths, lengths)
    similar = (scores >= NEAR_DUPLICATE_THRESHOLD) & (ratio >= LENGTH_RATIO_MIN)

    duplicates: set[int] = set()
    kept: list[int] = []
    for j in range(len(items)):
        if kept and bool(similar[kept, j].any()):
            duplicates.add(j)
        else:
            kept.append(j)
    return duplicates


def deduplicate(items: Sequence[DedupeItem[T]]) -> DedupeResult[T]:
    ordered = sorted(items, key=lambda item: (item.sort_key, item.review_id))
    drops: list[tuple[str, str]] = []
    ordered = _drop_seen(ordered, lambda item: item.review_id, "duplicate_id", drops)
    ordered = _drop_seen(ordered, lambda item: dedupe_key(item.text), "exact_duplicate", drops)

    by_rating: dict[int, list[DedupeItem[T]]] = defaultdict(list)
    for item in ordered:
        by_rating[item.rating].append(item)
    near: set[str] = set()
    for bucket in by_rating.values():
        for index in _near_duplicates(bucket):
            near.add(bucket[index].review_id)
            drops.append((bucket[index].review_id, "near_duplicate"))

    kept = [item for item in ordered if item.review_id not in near]
    return DedupeResult(kept=kept, dropped=drops)
