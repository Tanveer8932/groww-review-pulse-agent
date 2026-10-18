"""CSV / JSON / JSONL review importers (Phase 1, architecture §6.1).

Design rules:
* Columns are mapped by **header name**, never by position. There are two built-in
  profiles: ``play_console`` (Google Play Console monthly reviews CSV) and ``generic``
  (vendor and JSON exports). An optional column map overrides either.
* Encodings: UTF-8, UTF-8 with BOM, and UTF-16 (BOM or BOM-less). Anything else is rejected
  rather than guessed, because a wrong guess would corrupt verbatim review text.
* Allowlist ingestion: only canonical fields are kept. Reviewer names, avatars, profile
  links, devices, developer replies, emails, and any other columns are dropped and never
  written anywhere, including the quarantine file.
* Malformed rows are quarantined with reasons; the import continues. A file-level problem
  (unreadable, missing required columns) rejects that file only, and none of its rows are
  stored.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from groww_pulse.collection.base import DateWindow
from groww_pulse.errors import ImportSchemaError
from groww_pulse.models import Provenance, RawReview, ReviewSourceType
from groww_pulse.storage.jsonl_store import JsonlReviewStore, UpsertResult, append_jsonl

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".csv": "csv", ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl"}

CANONICAL_FIELDS: tuple[str, ...] = (
    "review_id",
    "review_link",
    "rating",
    "title",
    "text",
    "created_at",
    "created_at_millis",
    "updated_at",
    "updated_at_millis",
    "app_id",
    "app_version",
    "thumbs_up",
    "reviewer_language",
)

#: Google Play Console reviews report headers (Play Console Help, answer 6135870).
PLAY_CONSOLE_COLUMNS: Mapping[str, str] = {
    "app_id": "Package Name",
    "app_version": "App Version Name",
    "reviewer_language": "Reviewer Language",
    "created_at": "Review Submit Date and Time",
    "created_at_millis": "Review Submit Millis Since Epoch",
    "updated_at": "Review Last Update Date and Time",
    "updated_at_millis": "Review Last Update Millis Since Epoch",
    "rating": "Star Rating",
    "title": "Review Title",
    "text": "Review Text",
    "review_link": "Review Link",
}

#: Aliases for generic exports, in priority order (first match wins).
GENERIC_ALIASES: Mapping[str, tuple[str, ...]] = {
    "review_id": ("reviewId", "review_id", "id"),
    "rating": ("score", "rating", "star_rating", "stars"),
    "title": ("title", "review_title"),
    "text": ("content", "text", "review_text", "review", "body"),
    "created_at": ("at", "date", "created_at", "review_date", "timestamp"),
    "updated_at": ("updated_at", "last_updated", "review_last_update"),
    "app_id": ("app_id", "appId", "package_name"),
    "app_version": ("reviewCreatedVersion", "app_version", "appVersion", "version"),
    "thumbs_up": ("thumbsUpCount", "thumbs_up", "helpful_count"),
    "reviewer_language": ("reviewer_language", "language", "lang"),
}

_REVIEW_ID_IN_LINK = re.compile(r"ReviewPlace:id=([^&#\s]+)")
_INTEGER_LIKE = re.compile(r"^\s*(\d+)(?:\.0+)?\s*$")
_EPOCH_DIGITS = re.compile(r"^\s*\d{9,14}\s*$")
_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
)
_MIN_YEAR, _MAX_YEAR = 2008, 2100
_QUARANTINE_VALUE_LIMIT = 500


# ---------------------------------------------------------------------------- results
@dataclass
class FileReport:
    source_file: str
    format: str = ""
    encoding: str = ""
    profile: str = ""
    rows_read: int = 0
    accepted: int = 0
    quarantined: int = 0
    skipped_no_text: int = 0
    skipped_out_of_window: int = 0
    error: str | None = None


@dataclass
class ImportReport:
    run_id: str
    window: DateWindow
    store_path: Path
    files: list[FileReport] = field(default_factory=list)
    store: UpsertResult = field(default_factory=UpsertResult)
    quarantine_path: Path | None = None

    @property
    def ok(self) -> bool:
        return all(f.error is None for f in self.files)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "window": {"start": self.window.start.isoformat(), "end": self.window.end.isoformat()},
            "store_path": str(self.store_path),
            "quarantine_path": str(self.quarantine_path) if self.quarantine_path else None,
            "store": vars(self.store),
            "files": [vars(f) for f in self.files],
            "ok": self.ok,
        }


@dataclass(frozen=True)
class _Rejected:
    location: str
    reasons: tuple[str, ...]
    fields: Mapping[str, Any]


@dataclass(frozen=True)
class _NoText:
    location: str


_Outcome = RawReview | _Rejected | _NoText


# ---------------------------------------------------------------------------- column mapping
def normalize_header(name: str) -> str:
    """Case-, space-, underscore- and hyphen-insensitive header key."""
    return re.sub(r"[\s_\-]+", "", name.strip().lstrip("﻿").lower())


def load_column_map(path: Path) -> dict[str, str]:
    """Load a ``{canonical_field: source_header}`` override map from YAML (or JSON)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ImportSchemaError(f"Cannot read column map {path.name}: {exc}") from None
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise ImportSchemaError(f"Column map {path.name} must be a mapping of field name to header")
    unknown = sorted(set(data) - set(CANONICAL_FIELDS))
    if unknown:
        raise ImportSchemaError(
            f"Column map {path.name} has unknown fields {unknown}; "
            f"allowed: {list(CANONICAL_FIELDS)}"
        )
    return dict(data)


def detect_profile(headers: Sequence[str]) -> str:
    keys = {normalize_header(h) for h in headers}
    play_keys = {
        normalize_header(PLAY_CONSOLE_COLUMNS[k]) for k in ("text", "rating", "review_link")
    }
    return "play_console" if play_keys <= keys else "generic"


def resolve_columns(
    headers: Sequence[str], overrides: Mapping[str, str] | None = None
) -> tuple[str, dict[str, str]]:
    """Map canonical field to the actual header present in the file.

    Returns ``(profile, mapping)``. Raises :class:`ImportSchemaError` on duplicate headers
    or on an override naming a header that is not present.
    """
    by_key: dict[str, str] = {}
    for header in headers:
        key = normalize_header(header)
        if key in by_key:
            raise ImportSchemaError(f"Duplicate column header: {header!r}")
        by_key[key] = header

    profile = detect_profile(headers)
    mapping: dict[str, str] = {}
    if profile == "play_console":
        for canonical, header in PLAY_CONSOLE_COLUMNS.items():
            if (key := normalize_header(header)) in by_key:
                mapping[canonical] = by_key[key]
    else:
        for canonical, aliases in GENERIC_ALIASES.items():
            for alias in aliases:
                if (key := normalize_header(alias)) in by_key:
                    mapping[canonical] = by_key[key]
                    break

    for canonical, header in (overrides or {}).items():
        key = normalize_header(header)
        if key not in by_key:
            raise ImportSchemaError(f"Column map names {header!r} for {canonical}, not in file")
        mapping[canonical] = by_key[key]
    return profile, mapping


def missing_required(mapping: Mapping[str, str]) -> list[str]:
    missing = [f for f in ("text", "rating") if f not in mapping]
    if "created_at" not in mapping and "created_at_millis" not in mapping:
        missing.append("created_at")
    return missing


# ---------------------------------------------------------------------------- value parsing
def detect_encoding(sample: bytes) -> str:
    """Detect UTF-8 / UTF-8-BOM / UTF-16 from the first bytes of a file."""
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    nuls = sample.count(0)
    if nuls and nuls >= len(sample) // 4:  # BOM-less UTF-16: ASCII text has a NUL per char
        even_nuls = sample[0::2].count(0)
        odd_nuls = sample[1::2].count(0)
        return "utf-16-be" if even_nuls > odd_nuls else "utf-16-le"
    return "utf-8"


def parse_rating(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("rating is missing")
    if isinstance(value, int):
        rating = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"rating {value!r} is not a whole number")
        rating = int(value)
    else:
        match = _INTEGER_LIKE.match(str(value))
        if not match:
            raise ValueError(f"rating {str(value)[:20]!r} is not a whole number")
        rating = int(match.group(1))
    if not 1 <= rating <= 5:
        raise ValueError(f"rating {rating} is outside 1-5")
    return rating


def _from_epoch(number: float) -> datetime:
    seconds = number / 1000 if abs(number) >= 1e11 else number
    return datetime.fromtimestamp(seconds, tz=UTC)


def parse_timestamp(value: Any) -> datetime:
    """Parse ISO 8601, epoch seconds/milliseconds, or a few unambiguous formats to UTC.

    Naive values are interpreted as UTC. Slash dates such as 01/02/2026 are rejected
    because day/month order is ambiguous.
    """
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        raise ValueError("date is missing")
    if isinstance(value, int | float):
        parsed = _from_epoch(float(value))
    else:
        text = str(value).strip()
        is_epoch = bool(_EPOCH_DIGITS.match(text))
        parsed = _from_epoch(float(text)) if is_epoch else _parse_date_string(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    if not _MIN_YEAR <= parsed.year <= _MAX_YEAR:
        raise ValueError(f"date {parsed.date()} is outside {_MIN_YEAR}-{_MAX_YEAR}")
    return parsed


def _parse_date_string(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognized date format {text[:40]!r}")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_thumbs(value: Any) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("thumbs_up must be a whole number")
    match = _INTEGER_LIKE.match(str(value))
    if not match:
        raise ValueError(f"thumbs_up {str(value)[:20]!r} is not a whole number")
    return int(match.group(1))


def review_id_from_link(link: Any) -> str | None:
    """Extract the review ID from a Play Console ``Review Link`` URL fragment."""
    if not isinstance(link, str):
        return None
    match = _REVIEW_ID_IN_LINK.search(link)
    return match.group(1) if match else None


def derive_review_id(source: ReviewSourceType, created_at: datetime, text: str) -> str:
    """Fallback ID (architecture §5.1): ``sha256(source + created_at + original_text)``."""
    digest = hashlib.sha256(f"{source.value}|{created_at.isoformat()}|{text}".encode())
    return f"sha256-{digest.hexdigest()}"


# ---------------------------------------------------------------------------- row conversion
@dataclass(frozen=True)
class _Context:
    app_id: str
    source_file: str
    run_id: str
    ingested_at: datetime


def _quarantine_fields(values: Mapping[str, Any]) -> dict[str, Any]:
    """Allowlisted canonical values only; the review link (developer-account URL) is omitted."""
    out: dict[str, Any] = {}
    for name, value in values.items():
        if name == "review_link":
            continue
        out[name] = value[:_QUARANTINE_VALUE_LIMIT] if isinstance(value, str) else value
    return out


def build_review(values: Mapping[str, Any], location: str, ctx: _Context) -> _Outcome:
    """Convert one row of canonical values (already column-mapped) into a RawReview."""
    reasons: list[str] = []

    raw_text = values.get("text")
    if raw_text is None or (isinstance(raw_text, str) and not raw_text.strip()):
        # Rating-only review (common in Play Console exports): nothing to analyse, not malformed.
        return _NoText(location)
    if not isinstance(raw_text, str):
        reasons.append("review text must be a string")

    rating = created_at = updated_at = thumbs_up = None
    try:
        rating = parse_rating(values.get("rating"))
    except ValueError as exc:
        reasons.append(str(exc))

    created_raw = values.get("created_at_millis")
    if created_raw in (None, ""):
        created_raw = values.get("created_at")
    try:
        created_at = parse_timestamp(created_raw)
    except ValueError as exc:
        reasons.append(f"created_at: {exc}")

    updated_raw = values.get("updated_at_millis")
    if updated_raw in (None, ""):
        updated_raw = values.get("updated_at")
    if updated_raw not in (None, ""):
        try:
            updated_at = parse_timestamp(updated_raw)
        except ValueError as exc:
            reasons.append(f"updated_at: {exc}")

    try:
        thumbs_up = _parse_thumbs(values.get("thumbs_up"))
    except ValueError as exc:
        reasons.append(str(exc))

    app_id = _optional_str(values.get("app_id")) or ctx.app_id
    if app_id != ctx.app_id:
        reasons.append(f"app_id {app_id!r} does not match configured APP_ID {ctx.app_id!r}")

    if reasons or rating is None or created_at is None or not isinstance(raw_text, str):
        return _Rejected(location, tuple(reasons), _quarantine_fields(values))

    source = ReviewSourceType.GOOGLE_PLAY_IMPORT
    review_id = _optional_str(values.get("review_id")) or review_id_from_link(
        values.get("review_link")
    )
    derived = review_id is None
    if review_id is None:
        review_id = derive_review_id(source, created_at, raw_text)

    try:
        return RawReview(
            review_id=review_id,
            review_id_derived=derived,
            source=source,
            app_id=app_id,
            rating=rating,
            title=_optional_str(values.get("title")),
            text=raw_text,
            original_sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
            created_at=created_at,
            updated_at=updated_at,
            app_version=_optional_str(values.get("app_version")),
            thumbs_up=thumbs_up,
            reviewer_language=_optional_str(values.get("reviewer_language")),
            provenance=Provenance(
                source_file=ctx.source_file, ingested_at=ctx.ingested_at, run_id=ctx.run_id
            ),
        )
    except ValidationError as exc:
        details = tuple(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return _Rejected(location, details, _quarantine_fields(values))


# ---------------------------------------------------------------------------- file readers
def _read_sample(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(4096)


def _iter_csv(
    path: Path, encoding: str, overrides: Mapping[str, str] | None, report: FileReport
) -> Iterator[tuple[str, dict[str, Any]]]:
    with path.open(encoding=encoding, newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        while header is not None and not any(cell.strip() for cell in header):
            header = next(reader, None)
        if header is None:
            raise ImportSchemaError(f"{path.name}: no header row")
        profile, mapping = resolve_columns(header, overrides)
        report.profile = profile
        if missing := missing_required(mapping):
            raise ImportSchemaError(f"{path.name}: missing required column(s) {missing}")
        index = {canonical: header.index(column) for canonical, column in mapping.items()}
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            location = f"line {reader.line_num}"
            if len(row) != len(header):
                yield (
                    location,
                    {"__error__": f"row has {len(row)} fields, header has {len(header)}"},
                )
                continue
            yield location, {canonical: row[i] for canonical, i in index.items()}


def _map_record(
    record: Any, overrides: Mapping[str, str] | None, report: FileReport
) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {"__error__": "record is not a JSON object"}
    keys = [k for k in record if isinstance(k, str)]
    try:
        profile, mapping = resolve_columns(keys, overrides)
    except ImportSchemaError as exc:
        return {"__error__": str(exc)}
    report.profile = report.profile or profile
    if missing := missing_required(mapping):
        return {"__error__": f"missing required field(s) {missing}"}
    return {canonical: record.get(column) for canonical, column in mapping.items()}


def _iter_json(
    path: Path, encoding: str, overrides: Mapping[str, str] | None, report: FileReport
) -> Iterator[tuple[str, dict[str, Any]]]:
    try:
        with path.open(encoding=encoding) as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ImportSchemaError(
            f"{path.name}: invalid JSON ({exc.msg} at line {exc.lineno})"
        ) from None
    if not isinstance(data, list):
        raise ImportSchemaError(f"{path.name}: top-level JSON value must be an array of reviews")
    for i, record in enumerate(data, 1):
        yield f"item {i}", _map_record(record, overrides, report)


def _iter_jsonl(
    path: Path, encoding: str, overrides: Mapping[str, str] | None, report: FileReport
) -> Iterator[tuple[str, dict[str, Any]]]:
    with path.open(encoding=encoding) as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                yield f"line {line_no}", {"__error__": f"invalid JSON ({exc.msg})"}
                continue
            yield f"line {line_no}", _map_record(record, overrides, report)


_READERS = {"csv": _iter_csv, "json": _iter_json, "jsonl": _iter_jsonl}


def iter_file(
    path: Path,
    ctx: _Context,
    overrides: Mapping[str, str] | None,
    report: FileReport,
) -> Iterator[_Outcome]:
    """Yield one outcome per data row. Raises :class:`ImportSchemaError` for file-level faults."""
    fmt = SUPPORTED_SUFFIXES.get(path.suffix.lower())
    if fmt is None:
        raise ImportSchemaError(
            f"{path.name}: unsupported file type; expected {sorted(SUPPORTED_SUFFIXES)}"
        )
    report.format = fmt
    sample = _read_sample(path)
    if not sample:
        raise ImportSchemaError(f"{path.name}: file is empty")
    report.encoding = detect_encoding(sample)
    try:
        for location, values in _READERS[fmt](path, report.encoding, overrides, report):
            report.rows_read += 1
            if "__error__" in values:
                yield _Rejected(location, (str(values["__error__"]),), {})
            else:
                yield build_review(values, location, ctx)
    except UnicodeError:
        raise ImportSchemaError(
            f"{path.name}: not valid {report.encoding}; export as UTF-8 or UTF-16"
        ) from None
    except csv.Error as exc:
        raise ImportSchemaError(f"{path.name}: malformed CSV ({exc})") from None


# ---------------------------------------------------------------------------- orchestration
def expand_inputs(paths: Sequence[Path]) -> list[Path]:
    """Expand directories to their supported files (sorted); keep explicit files as given."""
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES))
        else:
            out.append(path)
    return out


def import_files(
    paths: Sequence[Path],
    *,
    app_id: str,
    window: DateWindow,
    data_dir: Path,
    run_id: str,
    column_map: Mapping[str, str] | None = None,
) -> ImportReport:
    """Import review exports into ``data_dir/raw/reviews_raw.jsonl``.

    Rows outside ``window`` or without review text are counted and skipped. Malformed rows
    go to ``data_dir/quarantine/{run_id}.jsonl``. A file with a file-level fault contributes
    nothing. Re-importing the same data adds nothing and leaves the store byte-identical.
    """
    store = JsonlReviewStore(data_dir / "raw" / "reviews_raw.jsonl")
    report = ImportReport(run_id=run_id, window=window, store_path=store.path)
    ingested_at = datetime.now(UTC)
    accepted: list[RawReview] = []
    quarantine: list[dict[str, Any]] = []

    for path in expand_inputs(paths):
        file_report = FileReport(source_file=path.name)
        report.files.append(file_report)
        ctx = _Context(app_id, path.name, run_id, ingested_at)
        file_reviews: list[RawReview] = []
        file_quarantine: list[dict[str, Any]] = []
        try:
            for outcome in iter_file(path, ctx, column_map, file_report):
                if isinstance(outcome, _NoText):
                    file_report.skipped_no_text += 1
                elif isinstance(outcome, _Rejected):
                    file_quarantine.append(
                        {
                            "run_id": run_id,
                            "source_file": path.name,
                            "location": outcome.location,
                            "reasons": list(outcome.reasons),
                            "fields": dict(outcome.fields),
                        }
                    )
                elif not window.contains(outcome.created_at):
                    file_report.skipped_out_of_window += 1
                else:
                    file_reviews.append(outcome)
        except (ImportSchemaError, OSError) as exc:
            file_report.error = (
                str(exc)
                if isinstance(exc, ImportSchemaError)
                else (f"{path.name}: cannot read file ({type(exc).__name__})")
            )
            log.error("File rejected", extra={"source_file": path.name, "error": file_report.error})
            continue
        file_report.accepted = len(file_reviews)
        file_report.quarantined = len(file_quarantine)
        accepted.extend(file_reviews)
        quarantine.extend(file_quarantine)
        log.info(
            "File parsed",
            extra={k: v for k, v in vars(file_report).items() if k not in ("error",)},
        )

    report.store = store.upsert(accepted)
    if quarantine:
        report.quarantine_path = data_dir / "quarantine" / f"{run_id}.jsonl"
        append_jsonl(report.quarantine_path, quarantine)
    log.info("Import finished", extra={"store": vars(report.store), "ok": report.ok})
    return report
