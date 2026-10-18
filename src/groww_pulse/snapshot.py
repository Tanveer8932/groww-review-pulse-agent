"""Read-only dashboard snapshot: one verified real pulse, safe to commit for a hosted UI.

``DATA_DIR`` is gitignored (raw reviews, the redacted store, caches, the delivery ledger),
so a hosted dashboard such as Streamlit Community Cloud starts empty. ``groww-pulse
export-snapshot`` copies exactly what the dashboard reads for one pulse into a directory
with the same ``runs/{run_id}/`` layout, so the dashboard loads it with the usual code and
re-runs every publish check (:func:`~groww_pulse.delivery.publisher.load_publishable_pulse`).

**What is copied** (from authentic artefacts only; synthetic pulses are refused):

* ``pulse.txt`` and ``pulse.md`` byte-for-byte, and ``pulse.json`` with its themes,
  verbatim quotes, actions, numbers, body, content hash, marker, and validator results
  unchanged;
* from ``funnel.json``: the window and the per-step counts, quote holds, and redaction
  counts;
* from ``analysis.json``: the method block (provider, model, prompt versions, ranking
  formula) and the input counts.

**What is withheld:** Google Play review IDs (``pulse.json`` theme member lists are emptied
and each quote's ``review_id`` becomes ``withheld-N``; no validator or rendered text uses
them), per-review theme assignments and other review-level analysis data, local paths and
preprocessing settings, LLM usage, and the whole delivery ledger. The manifest records a
per-target delivery *summary* for the snapshot's week instead (no keys, run IDs, Doc IDs,
recipients, or timestamps), so the hosted UI can say whether this version was delivered.

``snapshot.json`` (the manifest) holds the SHA-256 of each source file, so a snapshot can
be traced to the local artefacts it came from.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from groww_pulse.delivery.ledger import LEDGER_FILE, DeliveryLedger, Target, week_lock
from groww_pulse.delivery.publisher import find_latest_pulse, load_publishable_pulse
from groww_pulse.errors import DeliveryError
from groww_pulse.report.pipeline import PULSE_JSON, PULSE_MD, PULSE_TXT
from groww_pulse.storage.jsonl_store import atomic_write_lines

MANIFEST_FILE = "snapshot.json"
SCHEMA_VERSION = 1
KIND = "groww-pulse-dashboard-snapshot"
FUNNEL_FILE = "funnel.json"
ANALYSIS_FILE = "analysis.json"

FUNNEL_FIELDS = (
    "run_id", "window", "input", "output", "total_dropped", "steps", "quote_ineligible",
    "quote_holds", "reviews_redacted", "pii_redactions",
)  # fmt: skip
SOURCE_FIELDS = (
    "analysis_run_id", "analysis_sha256", "reviews_sha256", "preprocess_run_id",
    "theme_method",
)  # fmt: skip
WITHHELD = (
    "Google Play review IDs (theme member lists and quote review IDs)",
    "per-review theme assignments and other review-level analysis data",
    "local file paths and preprocessing settings",
    "LLM token usage",
    "the delivery ledger (keys, run IDs, timestamps)",
)

# Per-target delivery summary for the snapshot's own content hash.
DELIVERED = "delivered"
BLOCKED_WEEK = "blocked_week_already_delivered"
UNCERTAIN = "blocked_other_version_outcome_unknown"
NOT_DELIVERED = "not_delivered"
NO_LEDGER = "unknown_no_ledger"


@dataclass(frozen=True)
class SnapshotResult:
    directory: Path
    pulse_dir: Path
    manifest: dict[str, Any]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise DeliveryError(f"{path} does not exist") from None
    except (OSError, ValueError):
        raise DeliveryError(f"{path} is not readable JSON") from None
    if not isinstance(value, dict):
        raise DeliveryError(f"{path} is not a JSON object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_lines(path, [json.dumps(value, indent=2, ensure_ascii=False)])


def sanitize_pulse_document(document: dict[str, Any]) -> dict[str, Any]:
    """``pulse.json`` without review IDs or local paths; everything validated is kept."""
    out = dict(document)
    pulse = dict(out["pulse"])
    pulse["themes"] = [{**theme, "review_ids": []} for theme in pulse["themes"]]
    pulse["quotes"] = [
        {**quote, "review_id": f"withheld-{i}"} for i, quote in enumerate(pulse["quotes"], 1)
    ]
    out["pulse"] = pulse
    source = out.get("source") or {}
    out["source"] = {k: source[k] for k in SOURCE_FIELDS if k in source}
    return out


def sanitize_funnel(funnel: dict[str, Any]) -> dict[str, Any]:
    return {k: funnel[k] for k in FUNNEL_FIELDS if k in funnel}


def sanitize_analysis(document: dict[str, Any]) -> dict[str, Any]:
    body = document.get("analysis") or {}
    return {
        "schema_version": document.get("schema_version"),
        "run_id": document.get("run_id"),
        "created_at": document.get("created_at"),
        "synthetic_fixture_data": document.get("synthetic_fixture_data"),
        "analysis": {"method": body.get("method") or {}, "input": body.get("input") or {}},
    }


def delivery_summary(data_dir: Path, iso_week: str, content_hash: str) -> dict[str, str]:
    """Per target: was *this* content delivered, or is the week held by another version?"""
    path = data_dir / LEDGER_FILE
    if not path.exists():
        return {t.value: NO_LEDGER for t in Target}
    lock = week_lock(DeliveryLedger(path).entries(), iso_week, content_hash)
    summary: dict[str, str] = {}
    for target in Target:
        if target.value in lock.own_succeeded:
            summary[target.value] = DELIVERED
        elif lock.reason(target, force=True) is not None:
            summary[target.value] = BLOCKED_WEEK
        elif lock.reason(target) is not None:
            summary[target.value] = UNCERTAIN
        else:
            summary[target.value] = NOT_DELIVERED
    return summary


def export_snapshot(
    data_dir: Path, out_dir: Path, *, pulse_path: Path | None = None, allow_synthetic: bool = False
) -> SnapshotResult:
    """Write a verified snapshot of one real pulse to ``out_dir`` (replacing any snapshot).

    Raises :class:`DeliveryError` if the pulse is synthetic or fails any publish check, if a
    source artefact is missing, or if the written snapshot does not re-verify. Only tests
    pass ``allow_synthetic=True``; the CLI never does.
    """
    source_json = pulse_path or find_latest_pulse(data_dir)
    if source_json.is_dir():
        source_json = source_json / PULSE_JSON
    # Refuses synthetic pulses (unless a test opts in) and re-runs every publish check.
    verified = load_publishable_pulse(source_json, allow_synthetic=allow_synthetic)
    source_dir = source_json.parent
    document = _read(source_json)
    source = document.get("source") or {}
    preprocess_run = source.get("preprocess_run_id")
    analysis_run = source.get("analysis_run_id")
    if not preprocess_run or not analysis_run:
        raise DeliveryError(f"{source_json} does not name its preprocess and analysis runs")
    funnel_src = data_dir / "runs" / str(preprocess_run) / FUNNEL_FILE
    analysis_src = data_dir / "runs" / str(analysis_run) / ANALYSIS_FILE
    funnel, analysis = _read(funnel_src), _read(analysis_src)
    if analysis.get("synthetic_fixture_data") and not allow_synthetic:
        raise DeliveryError(f"{analysis_src} was built from synthetic fixtures")

    runs = out_dir / "runs"
    if out_dir.exists():
        for old in (out_dir / MANIFEST_FILE, *runs.glob("*/*.json"), *runs.glob("*/pulse.*")):
            old.unlink(missing_ok=True)
        for directory in runs.glob("*"):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
    pulse_dir = runs / verified.run_id
    _write_json(pulse_dir / PULSE_JSON, sanitize_pulse_document(document))
    for name in (PULSE_TXT, PULSE_MD):
        pulse_dir.mkdir(parents=True, exist_ok=True)
        (pulse_dir / name).write_bytes((source_dir / name).read_bytes())
    _write_json(runs / str(preprocess_run) / FUNNEL_FILE, sanitize_funnel(funnel))
    _write_json(runs / str(analysis_run) / ANALYSIS_FILE, sanitize_analysis(analysis))

    copy = load_publishable_pulse(pulse_dir, allow_synthetic=allow_synthetic)  # re-verify
    if (copy.iso_week, copy.content_hash, copy.full_text) != (
        verified.iso_week, verified.content_hash, verified.full_text,
    ):  # fmt: skip
        raise DeliveryError("the snapshot does not reproduce the source pulse exactly")

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "exported_at": datetime.now(UTC).isoformat(),
        "iso_week": verified.iso_week,
        "content_hash": verified.content_hash,
        "pulse_run_id": verified.run_id,
        "preprocess_run_id": preprocess_run,
        "analysis_run_id": analysis_run,
        "source_sha256": {
            PULSE_JSON: _sha256(source_json),
            PULSE_TXT: _sha256(source_dir / PULSE_TXT),
            PULSE_MD: _sha256(source_dir / PULSE_MD),
            FUNNEL_FILE: _sha256(funnel_src),
            ANALYSIS_FILE: _sha256(analysis_src),
        },
        "delivery": delivery_summary(data_dir, verified.iso_week, verified.content_hash),
        "withheld": list(WITHHELD),
    }
    _write_json(out_dir / MANIFEST_FILE, manifest)
    return SnapshotResult(out_dir, pulse_dir, manifest)


def read_manifest(directory: Path) -> dict[str, Any] | None:
    """The snapshot manifest in ``directory``, or ``None`` if it is absent or not one."""
    try:
        value = json.loads((directory / MANIFEST_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("kind") != KIND:
        return None
    return value
