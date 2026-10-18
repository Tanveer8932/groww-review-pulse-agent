"""Read model for the Streamlit dashboard (Phase 6; architecture §6.7).

Everything the UI shows comes from stored, verified artefacts; nothing is generated here.

* **Pulse.** The newest ``runs/*/pulse.json`` that passes the Phase 5 publish checks
  (:func:`~groww_pulse.delivery.publisher.load_publishable_pulse`: hashes, files, re-render,
  every Phase 4 validator re-run). Newer pulses that fail are listed with the reason and
  never displayed as content. Synthetic pulses are refused unless ``allow_synthetic`` (tests).
* **Statistics.** The preprocessing funnel and the analysis summary that the pulse names in
  its ``source`` block, read by run ID from ``DATA_DIR/runs``. Only counts and method
  metadata are read; no review text beyond the verified quotes ever reaches the UI.
* **Delivery status.** The idempotency ledger: the latest entry per target for this pulse,
  plus targets already delivered for the same ISO week with a *different* content hash.
  Tool responses are not copied into the read model.
* **Week lock.** The publisher's own :func:`~groww_pulse.delivery.ledger.week_lock`, so the
  UI explains a ``blocked_week_already_delivered`` refusal before it happens. When the week
  was only partly delivered by another version and that version's pulse is still on disk
  and verifies, it is loaded as :attr:`Dashboard.recovery` so the UI can finish it.

* **Snapshot fallback.** When ``DATA_DIR`` holds no verified pulse (a hosted app, where
  ``data/`` is not deployed) and a committed :mod:`~groww_pulse.snapshot` exists, the pulse
  and its statistics are read from the snapshot instead, through the same publish checks.
  A snapshot is read-only: there is no ledger, and :func:`live_delivery_blockers` refuses
  delivery. Its delivery status comes from the manifest summary.

:func:`preview_delivery`, :func:`deliver`, and :func:`check_mcp` are thin wrappers over the
Phase 5 publisher and client, so the UI uses exactly the CLI's code paths and safeguards.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from groww_pulse.config import DeliveryMode, Settings
from groww_pulse.delivery.ledger import (
    LEDGER_FILE,
    DeliveryLedger,
    LedgerEntry,
    Target,
    WeekLock,
    week_lock,
)
from groww_pulse.delivery.mcp_client import HealthReport
from groww_pulse.delivery.publisher import (
    DeliveryReport,
    PublishablePulse,
    find_pulse_by_hash,
    load_publishable_pulse,
    mcp_client_for,
    publish,
)
from groww_pulse.errors import ConfigError, DeliveryError
from groww_pulse.models import Pulse
from groww_pulse.report.pipeline import PULSE_JSON
from groww_pulse.snapshot import read_manifest

FUNNEL_FILE = "funnel.json"
ANALYSIS_FILE = "analysis.json"


@dataclass(frozen=True)
class RejectedPulse:
    run_id: str
    reason: str


@dataclass(frozen=True)
class DashboardPulse:
    directory: Path
    run_id: str
    created_at: str
    verified: PublishablePulse
    pulse: Pulse
    validators: list[dict[str, Any]]
    theme_method: str | None
    synthetic: bool
    week_lock: WeekLock | None = None  # None until the ledger is read

    def week_blocks(self) -> dict[str, str]:
        """Targets the publisher would refuse as ``blocked_week_already_delivered``.

        Targets this content already reached are left out: the publisher skips them.
        """
        lock = self.week_lock
        if lock is None:
            return {}
        return {
            t.value: reason
            for t in Target
            if t.value not in lock.own_succeeded and (reason := lock.reason(t)) is not None
        }


@dataclass(frozen=True)
class FunnelSummary:
    run_id: str
    window_start: str
    window_end: str
    input: int
    output: int
    total_dropped: int
    steps: list[dict[str, Any]]
    quote_ineligible: int
    quote_holds: dict[str, int]
    reviews_redacted: int
    pii_redactions: dict[str, int]


@dataclass(frozen=True)
class AnalysisSummary:
    run_id: str
    themes_method: str | None
    provider: str | None
    model: str | None
    ranking_formula: str | None
    fallback_reason: str | None
    n_quote_eligible: int | None


@dataclass(frozen=True)
class DeliveryRow:
    target: str
    status: str
    timestamp: str
    run_id: str
    forced: bool
    error_code: str | None


@dataclass
class Dashboard:
    pulse: DashboardPulse | None = None
    rejected: list[RejectedPulse] = field(default_factory=list)
    funnel: FunnelSummary | None = None
    analysis: AnalysisSummary | None = None
    delivery: dict[str, DeliveryRow | None] = field(default_factory=dict)
    other_versions_delivered: list[DeliveryRow] = field(default_factory=list)
    recovery: DashboardPulse | None = None  # the partly delivered version of this week
    recovery_problem: str | None = None  # why that version cannot be finished here
    problems: list[str] = field(default_factory=list)  # artefacts that could not be read
    ledger_error: str | None = None
    snapshot: dict[str, Any] | None = None  # manifest, when shown from a committed snapshot

    @property
    def validators_passed(self) -> bool:
        return self.pulse is not None and all(v["passed"] for v in self.pulse.validators)


# ---------------------------------------------------------------- loading
def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _pulse_candidates(data_dir: Path) -> list[tuple[str, Path]]:
    """``(created_at, pulse.json)`` newest first (ties: path order, like the publisher)."""
    found = []
    for path in sorted((data_dir / "runs").glob(f"*/{PULSE_JSON}")):
        document = _read_json(path) or {}
        found.append((str(document.get("created_at", "")), path))
    return sorted(found, key=lambda item: (item[0], str(item[1])), reverse=True)


def _load_pulse(path: Path, *, allow_synthetic: bool) -> DashboardPulse:
    verified = load_publishable_pulse(path, allow_synthetic=allow_synthetic)
    document = _read_json(path) or {}
    return DashboardPulse(
        directory=path.parent,
        run_id=verified.run_id,
        created_at=str(document.get("created_at", "")),
        verified=verified,
        pulse=Pulse.model_validate(document["pulse"]),
        validators=[
            {"name": v["name"], "passed": bool(v["passed"]), "detail": str(v.get("detail", ""))}
            for v in document.get("validators", [])
        ],
        theme_method=(document.get("source") or {}).get("theme_method"),
        synthetic=verified.synthetic,
    )


def _source_run(directory: Path, key: str) -> str | None:
    source = (_read_json(directory / PULSE_JSON) or {}).get("source") or {}
    value = source.get(key)
    return str(value) if value else None


def _load_funnel(data_dir: Path, run_id: str) -> FunnelSummary | None:
    funnel = _read_json(data_dir / "runs" / run_id / FUNNEL_FILE)
    if funnel is None:
        return None
    window = funnel.get("window") or {}
    return FunnelSummary(
        run_id=run_id,
        window_start=str(window.get("start", ""))[:10],
        window_end=str(window.get("end", ""))[:10],
        input=int(funnel.get("input", 0)),
        output=int(funnel.get("output", 0)),
        total_dropped=int(funnel.get("total_dropped", 0)),
        steps=[
            {
                "step": str(s.get("step", "")),
                "input": int(s.get("input", 0)),
                "dropped": int(s.get("dropped", 0)),
                "output": int(s.get("output", 0)),
                "reasons": {str(k): int(v) for k, v in (s.get("reasons") or {}).items()},
            }
            for s in funnel.get("steps", [])
        ],
        quote_ineligible=int(funnel.get("quote_ineligible", 0)),
        quote_holds={str(k): int(v) for k, v in (funnel.get("quote_holds") or {}).items()},
        reviews_redacted=int(funnel.get("reviews_redacted", 0)),
        pii_redactions={str(k): int(v) for k, v in (funnel.get("pii_redactions") or {}).items()},
    )


def _load_analysis(data_dir: Path, run_id: str) -> AnalysisSummary | None:
    document = _read_json(data_dir / "runs" / run_id / ANALYSIS_FILE)
    if document is None:
        return None
    body = document.get("analysis") or {}
    method = body.get("method") or {}
    n_eligible = (body.get("input") or {}).get("n_quote_eligible")
    return AnalysisSummary(
        run_id=run_id,
        themes_method=method.get("themes"),
        provider=method.get("provider"),
        model=method.get("model"),
        ranking_formula=method.get("ranking_formula"),
        fallback_reason=method.get("fallback_reason"),
        n_quote_eligible=int(n_eligible) if n_eligible is not None else None,
    )


def _delivery_rows(
    entries: list[LedgerEntry], pulse: PublishablePulse
) -> tuple[dict[str, DeliveryRow | None], list[DeliveryRow]]:
    """Latest ledger entry per target for this pulse; other delivered versions of the week."""
    current: dict[str, DeliveryRow | None] = {t.value: None for t in Target}
    others: dict[tuple[str, str], DeliveryRow] = {}
    for entry in entries:
        if entry.iso_week != pulse.iso_week:
            continue
        row = DeliveryRow(
            target=entry.target,
            status=entry.status,
            timestamp=entry.timestamp,
            run_id=entry.run_id,
            forced=entry.forced,
            error_code=entry.error_code,
        )
        if entry.content_hash == pulse.content_hash:
            current[entry.target] = row
        elif entry.status == "succeeded":
            others[(entry.target, entry.content_hash)] = row
    return current, list(others.values())


def load_dashboard(
    settings: Settings, *, allow_synthetic: bool = False, snapshot_dir: Path | None = None
) -> Dashboard:
    """Everything the dashboard shows; never raises for missing or invalid artefacts.

    ``DATA_DIR`` comes first. Only if it has no verified pulse is ``snapshot_dir`` used.
    """
    dashboard = _load_from(settings, settings.data_dir, allow_synthetic=allow_synthetic)
    if dashboard.pulse is not None or snapshot_dir is None:
        return dashboard
    manifest = read_manifest(snapshot_dir)
    if manifest is None:
        return dashboard
    snap = _load_from(settings, snapshot_dir, allow_synthetic=allow_synthetic, ledger=False)
    if snap.pulse is None:
        dashboard.rejected += snap.rejected
        return dashboard
    snap.rejected = dashboard.rejected + snap.rejected
    snap.snapshot = manifest
    return snap


def _load_from(
    settings: Settings, data_dir: Path, *, allow_synthetic: bool, ledger: bool = True
) -> Dashboard:
    dashboard = Dashboard()
    for _created, path in _pulse_candidates(data_dir):
        try:
            dashboard.pulse = _load_pulse(path, allow_synthetic=allow_synthetic)
        except DeliveryError as exc:
            dashboard.rejected.append(RejectedPulse(path.parent.name, str(exc)))
            continue
        except (KeyError, ValueError) as exc:  # pulse.json readable but not a Pulse
            reason = f"{path.name} could not be read ({type(exc).__name__})"
            dashboard.rejected.append(RejectedPulse(path.parent.name, reason))
            continue
        break
    if dashboard.pulse is None:
        return dashboard

    directory = dashboard.pulse.directory
    if run := _source_run(directory, "preprocess_run_id"):
        dashboard.funnel = _load_funnel(data_dir, run)
        if dashboard.funnel is None:
            dashboard.problems.append(f"preprocessing funnel for run {run} is missing")
    if run := _source_run(directory, "analysis_run_id"):
        dashboard.analysis = _load_analysis(data_dir, run)
        if dashboard.analysis is None:
            dashboard.problems.append(f"analysis for run {run} is missing")
    if not ledger:
        return dashboard

    try:
        entries = DeliveryLedger(data_dir / LEDGER_FILE).entries()
    except DeliveryError as exc:
        dashboard.ledger_error = str(exc)
        return dashboard
    verified = dashboard.pulse.verified
    dashboard.delivery, dashboard.other_versions_delivered = _delivery_rows(entries, verified)
    lock = week_lock(entries, verified.iso_week, verified.content_hash)
    dashboard.pulse = replace(dashboard.pulse, week_lock=lock)
    if dashboard.pulse.week_blocks():
        _load_recovery(dashboard, settings, entries, lock, allow_synthetic=allow_synthetic)
    return dashboard


def _load_recovery(
    dashboard: Dashboard, settings: Settings, entries: list[LedgerEntry], lock: WeekLock, *,
    allow_synthetic: bool,
) -> None:  # fmt: skip
    """Load the other version of this week that some target received but not all."""
    for content_hash in lock.delivered_hashes:
        own = week_lock(entries, lock.iso_week, content_hash)
        if own.own_succeeded >= {t.value for t in Target}:
            continue  # fully delivered: nothing left to finish
        label = f"version {content_hash[:12]} of {lock.iso_week}"
        directory = find_pulse_by_hash(settings.data_dir, lock.iso_week, content_hash)
        if directory is None:
            dashboard.recovery_problem = (
                f"{label} is only partly delivered, but its pulse files are no longer under "
                "DATA_DIR/runs, so its remaining targets cannot be delivered"
            )
            return
        try:
            view = _load_pulse(directory / PULSE_JSON, allow_synthetic=allow_synthetic)
        except (DeliveryError, KeyError, ValueError) as exc:
            dashboard.recovery_problem = f"{label} is only partly delivered, but {exc}"
            return
        dashboard.recovery = replace(view, week_lock=own)
        return


# ---------------------------------------------------------------- delivery gate
def live_delivery_blockers(
    settings: Settings, dashboard: Dashboard, view: DashboardPulse | None = None
) -> list[str]:
    """Reasons the confirmed-delivery button must stay disabled (empty means allowed).

    ``view`` is the pulse to deliver (default: the newest, ``dashboard.pulse``). These mirror
    the publisher's own checks so the UI can explain them up front; the publisher still
    re-checks everything, including the week lock and the MCP security gate. There is no
    way to override a week lock from the UI.
    """
    blockers = []
    if dashboard.snapshot is not None:
        blockers.append(
            "this is a read-only snapshot with no delivery ledger; deliver from the CLI "
            "where DATA_DIR and the ledger live"
        )
    view = view or dashboard.pulse
    if view is None:
        blockers.append("there is no validated pulse")
    else:
        if not all(v["passed"] for v in view.validators):
            blockers.append("not every Phase 4 validator passed")
        if view.synthetic:
            blockers.append("the pulse was built from synthetic test fixtures")
        if view.week_blocks():
            blockers.append(
                f"a different version of the {view.verified.iso_week} pulse was already "
                "delivered (duplicate-week protection)"
            )
    if dashboard.ledger_error:
        blockers.append("the delivery ledger cannot be read")
    if settings.delivery_mode is not DeliveryMode.CONFIRM:
        blockers.append("DELIVERY_MODE is not 'confirm'")
    try:
        settings.require_delivery()
    except ConfigError as exc:
        blockers.append(str(exc))
    if settings.mcp_auth_token is None:
        blockers.append("MCP_AUTH_TOKEN is not set")
    return blockers


def preview_delivery(settings: Settings, pulse_dir: Path, *, run_id: str) -> DeliveryReport:
    """Dry run of the publisher: no network calls; writes only ``runs/{run_id}/delivery.json``."""
    return publish(settings, run_id=run_id, pulse_path=pulse_dir, confirm=False)


def deliver(settings: Settings, pulse_dir: Path, *, run_id: str) -> DeliveryReport:
    """Confirmed delivery of exactly this pulse: Doc append + Gmail **draft**. Never ``force``."""
    return publish(settings, run_id=run_id, pulse_path=pulse_dir, confirm=True, force=False)


def check_mcp(settings: Settings) -> HealthReport:
    """Read-only MCP health check (handshake, ``tools/list``, contract, auth probe)."""
    settings.require_mcp()
    return asyncio.run(mcp_client_for(settings).check(probe_auth=True))


def format_timestamp(value: str) -> str:
    """``2026-10-17T06:25:58.477896+00:00`` -> ``2026-10-17 06:25 UTC`` (or the input)."""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        return f"{moment:%Y-%m-%d %H:%M}"
    return f"{moment.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
