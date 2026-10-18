# SYNTHETIC TEST FIXTURE — not real review data
"""End-to-end importer tests against synthetic export files in temporary directories."""

from __future__ import annotations

import json
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from groww_pulse.collection.base import DateWindow
from groww_pulse.collection.importers import ImportReport, import_files
from groww_pulse.models import RawReview
from tests.fixtures.synthetic import (
    APP_ID,
    AS_OF,
    FAKE_DEV_ACC,
    FAKE_REVIEWER,
    IN_WINDOW,
    OUT_OF_WINDOW,
    PLAY_HEADERS,
    millis,
    play_row,
    scraper_style_record,
    synthetic_text,
    write_csv,
    write_json,
    write_jsonl,
    write_play_csv,
)

WINDOW = DateWindow.ending(AS_OF, 12)
ALLOWED_KEYS = set(RawReview.model_fields)


def run_import(
    data_dir: Path, *paths: Path, column_map: dict[str, str] | None = None
) -> ImportReport:
    return import_files(
        list(paths),
        app_id=APP_ID,
        window=WINDOW,
        data_dir=data_dir,
        run_id="test-run",
        column_map=column_map,
    )


def stored(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "raw" / "reviews_raw.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def quarantined(report: ImportReport) -> list[dict[str, Any]]:
    assert report.quarantine_path is not None
    return [json.loads(x) for x in report.quarantine_path.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------ Play Console CSV
@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-8", "utf-8-sig"])
def test_play_console_csv_all_encodings(tmp_path: Path, encoding: str) -> None:
    src = write_play_csv(
        tmp_path / "reviews_com.nextbillion.groww_202609.csv",
        [play_row("gp:SYN-1", title="Synthetic title"), play_row("gp:SYN-2", rating=5)],
        encoding=encoding,
    )
    report = run_import(tmp_path / "data", src)
    assert report.ok
    file_report = report.files[0]
    assert file_report.profile == "play_console"
    assert file_report.accepted == 2
    records = stored(tmp_path / "data")
    assert [r["review_id"] for r in records] == ["gp:SYN-1", "gp:SYN-2"]
    first = records[0]
    assert first["review_id_derived"] is False
    assert first["text"] == synthetic_text(0)
    assert first["title"] == "Synthetic title"
    assert first["rating"] == 2
    assert first["created_at"] == "2026-09-15T12:00:00Z"
    assert first["app_version"] == "9.9.9-synthetic"
    assert first["provenance"]["source_file"] == "reviews_com.nextbillion.groww_202609.csv"
    assert first["provenance"]["run_id"] == "test-run"


def test_play_console_columns_reordered(tmp_path: Path) -> None:
    rows = [play_row("gp:SYN-1")]
    a = write_play_csv(tmp_path / "a.csv", rows)
    b = write_play_csv(tmp_path / "b.csv", rows, headers=list(reversed(PLAY_HEADERS)))
    run_import(tmp_path / "da", a)
    run_import(tmp_path / "db", b)
    strip = lambda rs: [{k: v for k, v in r.items() if k != "provenance"} for r in rs]  # noqa: E731
    assert strip(stored(tmp_path / "da")) == strip(stored(tmp_path / "db"))


def test_unwanted_fields_are_dropped_everywhere(tmp_path: Path) -> None:
    bad = play_row("gp:SYN-BAD", rating="9")
    src = write_play_csv(tmp_path / "p.csv", [play_row("gp:SYN-1"), bad])
    report = run_import(tmp_path / "data", src)
    for record in stored(tmp_path / "data"):
        assert set(record) <= ALLOWED_KEYS
    raw_store = (tmp_path / "data" / "raw" / "reviews_raw.jsonl").read_text(encoding="utf-8")
    raw_quarantine = report.quarantine_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    for leaked in ("synthetic-device", "developer reply", FAKE_DEV_ACC, "dev_acc", "Review Link"):
        assert leaked not in raw_store
        assert leaked not in raw_quarantine


def test_generic_json_drops_personal_fields(tmp_path: Path) -> None:
    src = write_json(
        tmp_path / "vendor.json", [scraper_style_record("v-1", 1), scraper_style_record("v-2", 2)]
    )
    report = run_import(tmp_path / "data", src)
    assert report.files[0].accepted == 2
    raw = (tmp_path / "data" / "raw" / "reviews_raw.jsonl").read_text(encoding="utf-8")
    for leaked in (FAKE_REVIEWER, "avatar", "example.invalid", "userName", "developer reply"):
        assert leaked not in raw
    record = stored(tmp_path / "data")[0]
    assert set(record) <= ALLOWED_KEYS
    assert record["thumbs_up"] == 3
    assert record["app_id"] == APP_ID


def test_jsonl_with_bad_lines(tmp_path: Path) -> None:
    src = write_jsonl(
        tmp_path / "vendor.jsonl",
        [
            scraper_style_record("v-1", 1),
            ["not", "an", "object"],
            {"content": "SYNTHETIC FIXTURE no rating"},
        ],
        extra_lines=["{broken json", scraper_style_record_line("v-2")],
    )
    report = run_import(tmp_path / "data", src)
    assert report.ok
    assert report.files[0].accepted == 2
    reasons = [q["reasons"][0] for q in quarantined(report)]
    assert any("not a JSON object" in r for r in reasons)
    assert any("missing required field" in r for r in reasons)
    assert any("invalid JSON" in r for r in reasons)


def scraper_style_record_line(review_id: str) -> str:
    return json.dumps(scraper_style_record(review_id, 9))


# ------------------------------------------------------------------ quarantine
def test_malformed_rows_are_quarantined_with_reasons(tmp_path: Path) -> None:
    rows = [
        play_row("gp:OK"),
        play_row("gp:RATING-HIGH", rating=7),
        play_row("gp:RATING-TEXT", rating="great"),
        {
            **play_row("gp:BAD-DATE"),
            "Review Submit Millis Since Epoch": "",
            "Review Submit Date and Time": "01/02/2026",
        },
        play_row("gp:OTHER-APP", package="com.example.other"),
    ]
    src = write_play_csv(tmp_path / "p.csv", rows)
    # A row with the wrong number of fields, appended in the same encoding.
    with src.open("ab") as handle:
        handle.write("only,three,fields\r\n".encode("utf-16-le"))
    report = run_import(tmp_path / "data", src)
    assert report.ok, "row-level problems must not fail the file"
    assert report.files[0].accepted == 1
    assert report.files[0].quarantined == 5
    by_location = {q["location"]: q for q in quarantined(report)}
    reasons = " | ".join(r for q in by_location.values() for r in q["reasons"])
    assert "rating 7 is outside 1-5" in reasons
    assert "'great' is not a whole number" in reasons
    assert "unrecognized date format '01/02/2026'" in reasons
    assert "does not match configured APP_ID" in reasons
    assert "row has 3 fields, header has 16" in reasons
    assert all(
        q["run_id"] == "test-run" and q["source_file"] == "p.csv" for q in by_location.values()
    )
    assert [r["review_id"] for r in stored(tmp_path / "data")] == ["gp:OK"]


def test_rating_only_and_out_of_window_rows_are_skipped_not_quarantined(tmp_path: Path) -> None:
    rows = [
        play_row("gp:OK"),
        play_row("gp:NO-TEXT", text=""),
        play_row("gp:OLD", submitted=OUT_OF_WINDOW),
        play_row("gp:FUTURE", submitted=datetime(2026, 10, 15, 0, 0, tzinfo=UTC)),
    ]
    report = run_import(tmp_path / "data", write_play_csv(tmp_path / "p.csv", rows))
    f = report.files[0]
    assert (f.rows_read, f.accepted, f.skipped_no_text, f.skipped_out_of_window, f.quarantined) == (
        4,
        1,
        1,
        2,
        0,
    )
    assert report.quarantine_path is None


def test_window_boundary_rows(tmp_path: Path) -> None:
    rows = [
        play_row("gp:FIRST", submitted=WINDOW.start),
        play_row("gp:LAST", submitted=datetime(2026, 10, 14, 23, 59, 59, tzinfo=UTC)),
        play_row("gp:BEFORE", submitted=WINDOW.start - timedelta(seconds=1)),
    ]
    run_import(tmp_path / "data", write_play_csv(tmp_path / "p.csv", rows))
    assert sorted(r["review_id"] for r in stored(tmp_path / "data")) == ["gp:FIRST", "gp:LAST"]


# ------------------------------------------------------------------ file-level errors
def test_missing_required_column_rejects_file_but_not_others(tmp_path: Path) -> None:
    bad = write_csv(
        tmp_path / "bad.csv", ["content", "at"], [[synthetic_text(1), IN_WINDOW.isoformat()]]
    )
    good = write_play_csv(tmp_path / "good.csv", [play_row("gp:OK")])
    report = run_import(tmp_path / "data", bad, good)
    assert not report.ok
    assert "missing required column(s) ['rating']" in (report.files[0].error or "")
    assert report.files[1].accepted == 1
    assert [r["review_id"] for r in stored(tmp_path / "data")] == ["gp:OK"]


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("empty.csv", b"", "file is empty"),
        ("blank.csv", b"\r\n\r\n", "no header row"),
        ("obj.json", b'{"reviews": []}', "must be an array"),
        ("broken.json", b"[{", "invalid JSON"),
        ("notes.txt", b"hello", "unsupported file type"),
        (
            "latin1.csv",
            "content,score,at\ncaf\xe9 SYNTHETIC,3,2026-09-15\n".encode("latin-1"),
            "not valid utf-8",
        ),
    ],
)
def test_file_level_errors(tmp_path: Path, name: str, content: bytes, message: str) -> None:
    path = tmp_path / name
    path.write_bytes(content)
    report = run_import(tmp_path / "data", path)
    assert not report.ok
    assert message in (report.files[0].error or "")
    assert not (tmp_path / "data" / "raw" / "reviews_raw.jsonl").read_text(encoding="utf-8")


def test_header_only_and_empty_array_are_valid_but_empty(tmp_path: Path) -> None:
    header_only = write_play_csv(tmp_path / "h.csv", [])
    empty_array = write_json(tmp_path / "e.json", [])
    report = run_import(tmp_path / "data", header_only, empty_array)
    assert report.ok
    assert [f.rows_read for f in report.files] == [0, 0]
    assert report.store.total == 0


def test_partially_decodable_file_contributes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "mixed.csv"
    good_rows = "".join(f"SYNTHETIC FIXTURE row {i},3,2026-09-15\n" for i in range(5))
    path.write_bytes(("content,score,at\n" + good_rows).encode() + b"caf\xe9,3,2026-09-15\n")
    report = run_import(tmp_path / "data", path)
    assert report.files[0].error
    assert report.store.total == 0


# ------------------------------------------------------------------ idempotency & overlap
def test_reimport_is_byte_identical_and_adds_nothing(tmp_path: Path) -> None:
    src = write_play_csv(
        tmp_path / "p.csv", [play_row(f"gp:SYN-{i}", text=synthetic_text(i)) for i in range(20)]
    )
    first = run_import(tmp_path / "data", src)
    store_file = tmp_path / "data" / "raw" / "reviews_raw.jsonl"
    before = store_file.read_bytes()
    second = run_import(tmp_path / "data", src)
    assert (first.store.added, second.store.added) == (20, 0)
    assert second.store.duplicates == 20
    assert store_file.read_bytes() == before


def test_overlapping_monthly_exports(tmp_path: Path) -> None:
    aug_sep = [
        play_row(f"gp:A{i}", submitted=datetime(2026, 8, 20 + i, tzinfo=UTC)) for i in range(5)
    ]
    sep_oct = [
        play_row(f"gp:A{i}", submitted=datetime(2026, 8, 20 + i, tzinfo=UTC)) for i in (3, 4)
    ]
    sep_oct += [
        play_row(f"gp:B{i}", submitted=datetime(2026, 10, 1 + i, tzinfo=UTC)) for i in range(3)
    ]
    f1 = write_play_csv(tmp_path / "reviews_202608.csv", aug_sep)
    f2 = write_play_csv(tmp_path / "reviews_202610.csv", sep_oct)
    report = run_import(tmp_path / "data", f1, f2)
    assert report.store.added == 8
    assert report.store.duplicates == 2
    assert report.store.total == 8
    ids = [r["review_id"] for r in stored(tmp_path / "data")]
    assert len(ids) == len(set(ids)) == 8
    # Provenance of overlapping records is the first file that supplied them.
    first_of_overlap = next(r for r in stored(tmp_path / "data") if r["review_id"] == "gp:A3")
    assert first_of_overlap["provenance"]["source_file"] == "reviews_202608.csv"


def test_edited_review_replaces_older_version_only(tmp_path: Path) -> None:
    original = play_row("gp:EDIT", text=synthetic_text(1))
    edited = play_row(
        "gp:EDIT",
        text=synthetic_text(1, " (edited)"),
        updated=IN_WINDOW + timedelta(days=2),
        rating=4,
    )
    run_import(tmp_path / "data", write_play_csv(tmp_path / "v1.csv", [original]))
    newer = run_import(tmp_path / "data", write_play_csv(tmp_path / "v2.csv", [edited]))
    assert newer.store.updated == 1
    older_again = run_import(tmp_path / "data", write_play_csv(tmp_path / "v1b.csv", [original]))
    assert older_again.store.conflicts == 1
    (record,) = stored(tmp_path / "data")
    assert record["text"].endswith("(edited)")
    assert record["rating"] == 4


def test_duplicates_within_one_file(tmp_path: Path) -> None:
    rows = [play_row("gp:DUP"), play_row("gp:DUP"), play_row("gp:OTHER")]
    report = run_import(tmp_path / "data", write_play_csv(tmp_path / "p.csv", rows))
    assert (report.store.added, report.store.duplicates, report.store.total) == (2, 1, 2)


def test_derived_ids_are_stable_when_no_id_or_link(tmp_path: Path) -> None:
    rows = [
        {"content": synthetic_text(i), "score": "3", "at": "2026-09-15T12:00:00Z"} for i in range(3)
    ]
    src = write_csv(
        tmp_path / "noid.csv", ["content", "score", "at"], [list(r.values()) for r in rows]
    )
    run_import(tmp_path / "data", src)
    first_ids = [r["review_id"] for r in stored(tmp_path / "data")]
    again = run_import(tmp_path / "data", src)
    assert again.store.added == 0
    assert all(i.startswith("sha256-") for i in first_ids)
    assert all(r["review_id_derived"] for r in stored(tmp_path / "data"))


def test_link_without_fragment_falls_back_to_derived_id(tmp_path: Path) -> None:
    row = play_row("unused", link="https://play.google.com/apps/publish/?dev_acc=1")
    run_import(tmp_path / "data", write_play_csv(tmp_path / "p.csv", [row]))
    (record,) = stored(tmp_path / "data")
    assert record["review_id"].startswith("sha256-")
    assert record["review_id_derived"] is True


# ------------------------------------------------------------------ verbatim text & mapping
def test_text_is_stored_verbatim(tmp_path: Path) -> None:
    tricky = '  SYNTHETIC FIXTURE: "quoted", commas, ₹10,000 — émoji-free\nsecond line  '
    run_import(
        tmp_path / "data", write_play_csv(tmp_path / "p.csv", [play_row("gp:V", text=tricky)])
    )
    (record,) = stored(tmp_path / "data")
    assert record["text"] == tricky
    import hashlib

    assert record["original_sha256"] == hashlib.sha256(tricky.encode("utf-8")).hexdigest()


def test_custom_column_map(tmp_path: Path) -> None:
    src = write_csv(
        tmp_path / "custom.csv",
        ["Comment", "Stars", "When", "Reviewer"],
        [[synthetic_text(1), "1", "2026-09-15T00:00:00Z", FAKE_REVIEWER]],
    )
    report = run_import(
        tmp_path / "data",
        src,
        column_map={"text": "Comment", "rating": "Stars", "created_at": "When"},
    )
    assert report.files[0].accepted == 1
    assert FAKE_REVIEWER not in (tmp_path / "data" / "raw" / "reviews_raw.jsonl").read_text(
        encoding="utf-8"
    )


def test_directory_input_expands_supported_files(tmp_path: Path) -> None:
    imports = tmp_path / "imports"
    imports.mkdir()
    write_play_csv(imports / "a.csv", [play_row("gp:A")])
    write_json(imports / "b.json", [scraper_style_record("v-1", 1)])
    (imports / "README.txt").write_text("ignored", encoding="utf-8")
    report = run_import(tmp_path / "data", imports)
    assert [f.source_file for f in report.files] == ["a.csv", "b.json"]
    assert report.store.total == 2


# ------------------------------------------------------------------ scale
def test_large_file_streams_with_bounded_memory(tmp_path: Path) -> None:
    n = 10_000
    rows = (
        play_row(f"gp:L{i}", submitted=IN_WINDOW - timedelta(minutes=i), text=synthetic_text(i))
        for i in range(n)
    )
    src = write_play_csv(tmp_path / "large.csv", rows)
    tracemalloc.start()
    started = time.perf_counter()
    report = run_import(tmp_path / "data", src)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert report.store.added == n
    assert elapsed < 60, f"import of {n} rows took {elapsed:.1f}s"
    assert peak < 120 * 1024 * 1024, f"peak memory {peak / 1e6:.0f} MB"


def test_millis_column_preferred_over_text_date(tmp_path: Path) -> None:
    row = {
        **play_row("gp:M"),
        "Review Submit Date and Time": "not a date",
        "Review Submit Millis Since Epoch": millis(IN_WINDOW),
    }
    run_import(tmp_path / "data", write_play_csv(tmp_path / "p.csv", [row]))
    assert stored(tmp_path / "data")[0]["created_at"] == "2026-09-15T12:00:00Z"


def test_more_row_level_reasons_and_blank_lines(tmp_path: Path) -> None:
    base = scraper_style_record("v-ok", 1)
    records = [
        base,
        {**scraper_style_record("v-num", 2), "content": 12345},
        {**scraper_style_record("v-upd", 3), "updated_at": "soon"},
        {**scraper_style_record("v-thumb", 4), "thumbsUpCount": "-1"},
        {**scraper_style_record("v-bool", 5), "thumbsUpCount": True},
        {**scraper_style_record("v-dupkey", 6), "Content": "SYNTHETIC FIXTURE duplicate key"},
    ]
    src = write_jsonl(tmp_path / "v.jsonl", records, extra_lines=["", "   "])
    report = run_import(tmp_path / "data", src)
    assert report.files[0].rows_read == 6, "blank lines are not rows"
    assert report.files[0].accepted == 1
    reasons = " | ".join(r for q in quarantined(report) for r in q["reasons"])
    assert "review text must be a string" in reasons
    assert "updated_at: unrecognized date format 'soon'" in reasons
    assert "thumbs_up '-1' is not a whole number" in reasons
    assert "thumbs_up must be a whole number" in reasons
    assert "Duplicate column header" in reasons


def test_blank_csv_rows_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "blank_rows.csv"
    path.write_text(
        f"\n,,\ncontent,score,at\n{synthetic_text(1)},3,2026-09-15\n,,\n\n", encoding="utf-8"
    )
    report = run_import(tmp_path / "data", path)
    assert report.ok
    assert (report.files[0].rows_read, report.files[0].accepted) == (1, 1)
