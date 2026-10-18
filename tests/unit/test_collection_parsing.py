# SYNTHETIC TEST FIXTURE — not real review data
"""Window, value parsing, encoding detection, and header-mapping unit tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from groww_pulse.collection.base import DateWindow
from groww_pulse.collection.importers import (
    detect_encoding,
    detect_profile,
    load_column_map,
    missing_required,
    normalize_header,
    parse_rating,
    parse_timestamp,
    resolve_columns,
    review_id_from_link,
)
from groww_pulse.errors import ConfigError, ImportSchemaError
from tests.fixtures.synthetic import PLAY_HEADERS, play_link

# ------------------------------------------------------------------ window
WINDOW = DateWindow.ending(date(2026, 10, 14), 12)


def test_window_is_whole_days_inclusive() -> None:
    assert WINDOW.start == datetime(2026, 7, 23, tzinfo=UTC)
    assert WINDOW.end == datetime(2026, 10, 14, 23, 59, 59, 999999, tzinfo=UTC)
    assert (WINDOW.end - WINDOW.start) + timedelta(microseconds=1) == timedelta(days=84)


@pytest.mark.parametrize("weeks", [8, 12])
def test_window_accepts_allowed_lengths(weeks: int) -> None:
    window = DateWindow.ending(date(2026, 10, 14), weeks)
    assert window.start.date() == date(2026, 10, 14) - timedelta(days=weeks * 7 - 1)


@pytest.mark.parametrize("weeks", [7, 13, 0])
def test_window_rejects_out_of_range(weeks: int) -> None:
    with pytest.raises(ConfigError):
        DateWindow.ending(date(2026, 10, 14), weeks)


@pytest.mark.parametrize(
    ("moment", "inside"),
    [
        (WINDOW.start, True),
        (WINDOW.end, True),
        (WINDOW.start - timedelta(microseconds=1), False),
        (WINDOW.end + timedelta(microseconds=1), False),
        # 03:00 IST on the first day is 21:30 UTC the previous day: outside.
        (datetime(2026, 7, 23, 3, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))), False),
        # 23:00 UTC-02:00 on the last day is 01:00 UTC the next day: outside.
        (datetime(2026, 10, 14, 23, 0, tzinfo=timezone(timedelta(hours=-2))), False),
    ],
)
def test_window_boundaries_and_timezones(moment: datetime, inside: bool) -> None:
    assert WINDOW.contains(moment) is inside


def test_window_requires_aware_bounds() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DateWindow(datetime(2026, 1, 1), datetime(2026, 2, 1))
    with pytest.raises(ValueError, match="after"):
        DateWindow(WINDOW.end, WINDOW.start)


# ------------------------------------------------------------------ rating
@pytest.mark.parametrize(("raw", "expected"), [("4", 4), ("4.0", 4), (" 5 ", 5), (1, 1), (3.0, 3)])
def test_parse_rating_valid(raw: object, expected: int) -> None:
    assert parse_rating(raw) == expected


@pytest.mark.parametrize("raw", ["4.5", "five", "0", "6", "", None, True, 2.5, -1, "1e1"])
def test_parse_rating_invalid(raw: object) -> None:
    with pytest.raises(ValueError, match="rating"):
        parse_rating(raw)


# ------------------------------------------------------------------ timestamps
EXPECTED = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "raw",
    [
        "2026-09-01T10:00:00Z",
        "2026-09-01T15:30:00+05:30",
        "2026-09-01T10:00:00",  # naive -> UTC
        "2026-09-01 10:00:00",
        1788256800000,  # epoch millis (int)
        "1788256800000",  # epoch millis (str)
        1788256800,  # epoch seconds
        "1788256800",
        1788256800.0,
    ],
)
def test_parse_timestamp_variants(raw: object) -> None:
    assert parse_timestamp(raw) == EXPECTED


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sep 1, 2026", datetime(2026, 9, 1, tzinfo=UTC)),
        ("1 September 2026", datetime(2026, 9, 1, tzinfo=UTC)),
        ("2026/09/01", datetime(2026, 9, 1, tzinfo=UTC)),
        ("2026-09-01", datetime(2026, 9, 1, tzinfo=UTC)),
    ],
)
def test_parse_timestamp_named_formats(raw: str, expected: datetime) -> None:
    assert parse_timestamp(raw) == expected


@pytest.mark.parametrize("raw", ["01/02/2026", "", "   ", None, "yesterday", True, "1990-01-01", 0])
def test_parse_timestamp_rejects(raw: object) -> None:
    with pytest.raises(ValueError, match="date"):
        parse_timestamp(raw)


# ------------------------------------------------------------------ encoding
@pytest.mark.parametrize(
    ("encoding", "expected"),
    [
        ("utf-8", "utf-8"),
        ("utf-8-sig", "utf-8-sig"),
        ("utf-16", "utf-16"),
        ("utf-16-le", "utf-16-le"),
        ("utf-16-be", "utf-16-be"),
    ],
)
def test_detect_encoding(encoding: str, expected: str) -> None:
    assert detect_encoding("Review Text,Star Rating\nhello,5\n".encode(encoding)) == expected


# ------------------------------------------------------------------ review ID
def test_review_id_from_link_fragment() -> None:
    assert review_id_from_link(play_link("gp:AOqpTSYNTHETIC-1")) == "gp:AOqpTSYNTHETIC-1"


@pytest.mark.parametrize(
    "link", ["", None, "https://play.google.com/apps/publish/?dev_acc=1", "ReviewPlace:other=1", 5]
)
def test_review_id_from_link_absent(link: object) -> None:
    assert review_id_from_link(link) is None


# ------------------------------------------------------------------ header mapping
def test_normalize_header() -> None:
    assert normalize_header("﻿ Review_Text ") == "reviewtext"
    assert normalize_header("reviewId") == normalize_header("review_id") == "reviewid"


def test_play_console_profile_maps_by_name_in_any_order() -> None:
    shuffled = list(reversed(PLAY_HEADERS))
    profile, mapping = resolve_columns(shuffled)
    assert profile == "play_console"
    assert mapping["text"] == "Review Text"
    assert mapping["rating"] == "Star Rating"
    assert mapping["created_at_millis"] == "Review Submit Millis Since Epoch"
    assert "Device" not in mapping.values()
    assert "Developer Reply Text" not in mapping.values()


def test_generic_aliases_priority_and_case() -> None:
    profile, mapping = resolve_columns(["CONTENT", "Text", "Score", "AT", "userName"])
    assert profile == "generic"
    assert mapping == {"text": "CONTENT", "rating": "Score", "created_at": "AT"}


def test_overrides_and_errors() -> None:
    _, mapping = resolve_columns(
        ["Comment", "Stars", "When"], {"text": "comment", "rating": "Stars", "created_at": "When"}
    )
    assert mapping == {"text": "Comment", "rating": "Stars", "created_at": "When"}
    with pytest.raises(ImportSchemaError, match="not in file"):
        resolve_columns(["a"], {"text": "missing"})
    with pytest.raises(ImportSchemaError, match="Duplicate"):
        resolve_columns(["Review Text", "review_text"])


def test_missing_required() -> None:
    assert missing_required({"text": "t"}) == ["rating", "created_at"]
    assert missing_required({"text": "t", "rating": "r", "created_at_millis": "m"}) == []


def test_detect_profile_needs_play_signature() -> None:
    assert detect_profile(["Review Text", "Star Rating"]) == "generic"


def test_load_column_map(tmp_path: Path) -> None:
    good = tmp_path / "map.yaml"
    good.write_text("text: Comment\nrating: Stars\n", encoding="utf-8")
    assert load_column_map(good) == {"text": "Comment", "rating": "Stars"}

    unknown = tmp_path / "unknown.yaml"
    unknown.write_text("reviewer_name: Name\n", encoding="utf-8")
    with pytest.raises(ImportSchemaError, match="unknown fields"):
        load_column_map(unknown)

    not_mapping = tmp_path / "list.yaml"
    not_mapping.write_text("- text\n", encoding="utf-8")
    with pytest.raises(ImportSchemaError, match="mapping"):
        load_column_map(not_mapping)

    broken = tmp_path / "broken.yaml"
    broken.write_text("text: [unclosed\n", encoding="utf-8")
    with pytest.raises(ImportSchemaError, match="Cannot read"):
        load_column_map(broken)
