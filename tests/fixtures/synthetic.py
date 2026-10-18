# SYNTHETIC TEST FIXTURE — not real review data
"""Builders for synthetic review export files used by the Phase 1 tests.

Every review text produced here starts with ``SYNTHETIC FIXTURE`` so it can never be
mistaken for a real Groww review. Reviewer names, developer IDs, and links are fake.
These files exist only inside pytest's temporary directories and are never imported into
a real data directory or used for a published pulse.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

APP_ID = "com.nextbillion.groww"
AS_OF = date(2026, 10, 14)  # 12-week window: 2026-07-23 .. 2026-10-14 (UTC, inclusive)
IN_WINDOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
OUT_OF_WINDOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
FAKE_REVIEWER = "Synthetic Reviewer Name"
FAKE_DEV_ACC = "0000000000000000000"

#: Google Play Console reviews report header set (Play Console Help, answer 6135870).
PLAY_HEADERS: list[str] = [
    "Package Name",
    "App Version Code",
    "App Version Name",
    "Reviewer Language",
    "Device",
    "Review Submit Date and Time",
    "Review Submit Millis Since Epoch",
    "Review Last Update Date and Time",
    "Review Last Update Millis Since Epoch",
    "Star Rating",
    "Review Title",
    "Review Text",
    "Developer Reply Date and Time",
    "Developer Reply Millis Since Epoch",
    "Developer Reply Text",
    "Review Link",
]


def synthetic_text(n: int, extra: str = "") -> str:
    return f"SYNTHETIC FIXTURE review {n}: orders lag when the market opens{extra}"


def millis(moment: datetime) -> str:
    return str(int(moment.timestamp() * 1000))


def play_link(review_id: str) -> str:
    return (
        f"https://play.google.com/apps/publish/?dev_acc={FAKE_DEV_ACC}#ReviewPlace:id={review_id}"
    )


def play_row(
    review_id: str,
    submitted: datetime = IN_WINDOW,
    rating: int | str = 2,
    text: str | None = None,
    *,
    title: str = "",
    updated: datetime | None = None,
    package: str = APP_ID,
    link: str | None = None,
) -> dict[str, str]:
    updated = updated or submitted
    return {
        "Package Name": package,
        "App Version Code": "1",
        "App Version Name": "9.9.9-synthetic",
        "Reviewer Language": "en",
        "Device": "synthetic-device",
        "Review Submit Date and Time": submitted.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Review Submit Millis Since Epoch": millis(submitted),
        "Review Last Update Date and Time": updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Review Last Update Millis Since Epoch": millis(updated),
        "Star Rating": str(rating),
        "Review Title": title,
        "Review Text": synthetic_text(0) if text is None else text,
        "Developer Reply Date and Time": "",
        "Developer Reply Millis Since Epoch": "",
        "Developer Reply Text": "SYNTHETIC developer reply that must be dropped",
        "Review Link": play_link(review_id) if link is None else link,
    }


def write_csv(
    path: Path,
    header: Sequence[str],
    rows: Iterable[Sequence[str] | dict[str, str]],
    encoding: str = "utf-8",
) -> Path:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(h, "") for h in header] if isinstance(row, dict) else row)
    path.write_bytes(buffer.getvalue().encode(encoding))
    return path


def write_play_csv(
    path: Path,
    rows: Iterable[dict[str, str]],
    *,
    encoding: str = "utf-16",
    headers: Sequence[str] = PLAY_HEADERS,
) -> Path:
    """Play Console exports are UTF-16; ``"utf-16"`` writes a BOM, ``"utf-16-le"`` does not."""
    return write_csv(path, headers, rows, encoding=encoding)


def scraper_style_record(
    review_id: str, n: int, at: datetime = IN_WINDOW, score: Any = 2
) -> dict[str, Any]:
    """A generic export record carrying personal fields that must be dropped at ingest."""
    return {
        "reviewId": review_id,
        "userName": FAKE_REVIEWER,
        "userImage": "https://example.invalid/avatar/synthetic.png",
        "userEmail": "synthetic.reviewer@example.invalid",
        "content": synthetic_text(n),
        "score": score,
        "thumbsUpCount": 3,
        "reviewCreatedVersion": "9.9.9-synthetic",
        "at": at.isoformat(),
        "replyContent": "SYNTHETIC developer reply that must be dropped",
        "repliedAt": at.isoformat(),
    }


def write_json(path: Path, records: Any) -> Path:
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def write_jsonl(path: Path, records: Iterable[Any], extra_lines: Iterable[str] = ()) -> Path:
    lines = [json.dumps(r) for r in records] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
