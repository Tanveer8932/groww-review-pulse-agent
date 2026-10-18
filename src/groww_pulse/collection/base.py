"""Collection primitives: the reporting window and the review-source protocol."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol

from groww_pulse.config import MAX_WINDOW_WEEKS, MIN_WINDOW_WEEKS
from groww_pulse.errors import ConfigError
from groww_pulse.models import RawReview


@dataclass(frozen=True)
class DateWindow:
    """Inclusive UTC window ``[start, end]`` covering whole calendar days."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("DateWindow bounds must be timezone-aware")
        if self.start > self.end:
            raise ValueError("DateWindow start must not be after end")

    @classmethod
    def ending(cls, as_of: date, weeks: int) -> DateWindow:
        """Window of exactly ``weeks * 7`` whole UTC days ending on (and including) ``as_of``.

        Example: ``ending(date(2026, 10, 14), 12)`` covers 2026-07-23 00:00:00 to
        2026-10-14 23:59:59.999999 UTC.
        """
        if not MIN_WINDOW_WEEKS <= weeks <= MAX_WINDOW_WEEKS:
            raise ConfigError(
                f"Window must be {MIN_WINDOW_WEEKS}-{MAX_WINDOW_WEEKS} weeks, got {weeks}"
            )
        first_day = as_of - timedelta(days=weeks * 7 - 1)
        return cls(
            start=datetime.combine(first_day, time.min, tzinfo=UTC),
            end=datetime.combine(as_of, time.max, tzinfo=UTC),
        )

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment.astimezone(UTC) <= self.end


class ReviewSource(Protocol):
    """Anything that yields canonical raw reviews for a window."""

    def fetch(self, window: DateWindow) -> Iterator[RawReview]: ...
