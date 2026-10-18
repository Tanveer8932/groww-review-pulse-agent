"""Phase 5 publisher: a validated Phase 4 pulse -> Google Doc append + Gmail **draft**.

**Input.** Only a publishable Phase 4 pulse (:func:`load_publishable_pulse`):
* ``pulse.json`` must be schema 1, with ``publishable: true`` and no synthetic data, and
  every Phase 4 validator must have passed;
* ``pulse.txt`` and ``pulse.md`` must equal the audit marker plus the stored bodies;
* the content hash and the marker must match;
* the body must re-render byte-for-byte from the stored themes, quotes, and actions, and
  every Phase 4 validator must pass again (word limit, 3/3/3, verbatim quotes, computed
  numbers, no PII, no synthetic data).

Synthetic pulses are refused; only tests pass ``allow_synthetic=True``.

**Calls** (built in code; nothing else is ever sent):
* ``google_docs_append_content(documentId=GOOGLE_DOC_ID, content=pulse.txt,
  ensureLeadingNewline=true)``
* ``gmail_create_draft(to=PULSE_EMAIL_TO, subject="Groww Review Pulse — {iso_week}",
  body=pulse.txt, bodyType="text")``. A draft only; nothing is sent.

**Modes.**
* **Dry run (default)** makes no network call. It returns the exact tool names and
  arguments, with the Doc ID and recipients masked, and what the ledger would decide.
* **Live** requires all of the following before any network I/O:
  - the ``--confirm`` flag (``confirm=True``);
  - ``DELIVERY_MODE=confirm``;
  - ``MCP_SERVER_URL``, ``GOOGLE_DOC_ID``, and ``PULSE_EMAIL_TO``;
  - ``MCP_AUTH_TOKEN``.

  Then, over the network:
  1. a health check (handshake, ``tools/list``, contract);
  2. the **security gate**: the server must reject clients with no credentials and with a
     wrong token (:meth:`McpClient.probe_auth`), otherwise :class:`SecurityGateError`;
  3. the tool calls.

**Idempotency** (:mod:`.ledger`). Each target has its own key, built from the ISO week,
the content hash, and the Doc ID or the recipients.
* Already succeeded: skipped, with 0 calls.
* ``pending`` or ``unknown``: blocked until the operator checks the Doc or Drafts and passes
  ``force``.
* ``failed`` (definite): retried.

**One version per ISO week** (:func:`.ledger.week_lock`). Once any target received a
version of a week, a different content hash for that week is ``blocked_week_already_delivered``
in dry runs and live runs alike, and ``force`` does not change that. The version that was
delivered can still finish its remaining targets; the block message names its run directory
(:func:`find_pulse_by_hash`). The check lives here, so the CLI and the UI share it.

The Doc is appended first, then the draft. A Doc outcome of ``unknown`` stops the run
before the draft. Every reported status comes from a real tool response or a recorded
local decision; nothing is synthesized.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from groww_pulse.analysis.themes import METHOD_FALLBACK
from groww_pulse.config import DeliveryMode, Settings
from groww_pulse.delivery.contract import DOC_APPEND_TOOL, DRAFT_TOOL
from groww_pulse.delivery.ledger import (
    LEDGER_FILE,
    DeliveryLedger,
    LedgerEntry,
    LedgerStatus,
    Target,
    WeekLock,
    doc_key,
    draft_key,
    week_lock,
)
from groww_pulse.delivery.mcp_client import (
    HealthReport,
    McpClient,
    ToolCallResult,
    ToolOutcome,
    ToolSession,
    call_failure,
    describe,
)
from groww_pulse.errors import (
    DeliveryError,
    MCPAuthError,
    MCPUnavailableError,
    SecurityGateError,
)
from groww_pulse.logging_setup import mask
from groww_pulse.models import Pulse
from groww_pulse.report.pipeline import PULSE_JSON, PULSE_MD, PULSE_TXT
from groww_pulse.report.pipeline import SCHEMA_VERSION as PULSE_SCHEMA_VERSION
from groww_pulse.report.render import PulseContent, audit_marker, render
from groww_pulse.report.validators import MAX_WORDS, validate_rendered
from groww_pulse.storage.jsonl_store import atomic_write_lines

log = logging.getLogger(__name__)

DELIVERY_FILE = "delivery.json"
SUBJECT_PREFIX = "Groww Review Pulse"
REQUIRED_VALIDATORS = frozenset(
    {"word_limit", "three_three_three", "quotes_verbatim", "numbers_computed", "no_pii",
     "no_synthetic"}
)  # fmt: skip


# ---------------------------------------------------------------- pulse input
class _Validator(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    passed: bool


class _PulseDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")
    schema_version: int
    run_id: str
    created_at: datetime
    synthetic_fixture_data: bool
    publishable: bool
    audit_marker: str
    detail_level: int
    source: dict[str, Any]
    validators: list[_Validator]
    pulse: Pulse


@dataclass(frozen=True)
class PublishablePulse:
    run_id: str
    directory: Path
    iso_week: str
    content_hash: str
    marker: str
    full_text: str
    full_markdown: str
    word_count: int
    synthetic: bool


def find_latest_pulse(data_dir: Path) -> Path:
    """The ``pulse.json`` with the newest ``created_at`` (ties: path order)."""
    candidates: list[tuple[str, str, Path]] = []
    for path in sorted((data_dir / "runs").glob(f"*/{PULSE_JSON}")):
        try:
            created = str(json.loads(path.read_text("utf-8")).get("created_at", ""))
        except (OSError, ValueError, AttributeError):
            created = ""
        candidates.append((created, str(path), path))
    if not candidates:
        raise DeliveryError(
            f"no {PULSE_JSON} found under {data_dir / 'runs'}; run `groww-pulse report` first"
        )
    return max(candidates)[2]


def find_pulse_by_hash(data_dir: Path, iso_week: str, content_hash: str) -> Path | None:
    """The newest run directory whose ``pulse.json`` holds this week and content hash.

    Only the stored identifiers are compared; the caller still verifies the pulse with
    :func:`load_publishable_pulse` before delivering it.
    """
    found: list[tuple[str, str, Path]] = []
    for path in (data_dir / "runs").glob(f"*/{PULSE_JSON}"):
        try:
            document = json.loads(path.read_text("utf-8"))
            pulse = document["pulse"]
            if (pulse["iso_week"], pulse["content_hash"]) == (iso_week, content_hash):
                found.append((str(document.get("created_at", "")), str(path), path.parent))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return max(found)[2] if found else None


def load_publishable_pulse(path: Path, *, allow_synthetic: bool = False) -> PublishablePulse:
    """Load ``pulse.json`` (or its directory) and re-verify everything; see module docstring."""
    pulse_json = path / PULSE_JSON if path.is_dir() else path
    directory = pulse_json.parent
    try:
        document = _PulseDocument.model_validate_json(pulse_json.read_text("utf-8"))
    except FileNotFoundError:
        raise DeliveryError(f"{pulse_json} does not exist") from None
    except (OSError, ValidationError) as exc:
        detail = f"{exc.error_count()} errors" if isinstance(exc, ValidationError) else "unreadable"
        raise DeliveryError(f"{pulse_json.name} is not a valid Phase 4 pulse ({detail})") from None

    problems: list[str] = []
    if document.schema_version != PULSE_SCHEMA_VERSION:
        problems.append(f"schema_version {document.schema_version} is not supported")
    if not allow_synthetic and (document.synthetic_fixture_data or not document.publishable):
        raise DeliveryError(
            "this pulse was built from synthetic test fixtures or is marked not publishable; "
            "only a pulse from real, permitted review data can be delivered"
        )
    passed = {v.name for v in document.validators if v.passed}
    if failed := sorted(REQUIRED_VALIDATORS - passed):
        problems.append(f"Phase 4 validators did not all pass: {', '.join(failed)}")

    pulse = document.pulse
    if hashlib.sha256(pulse.body_text.encode("utf-8")).hexdigest() != pulse.content_hash:
        problems.append("content_hash does not match body_text")
    marker = audit_marker(pulse.iso_week, pulse.content_hash)
    if document.audit_marker != marker:
        problems.append("audit marker does not match the ISO week and content hash")
    full_text, full_markdown = f"{marker}\n{pulse.body_text}", f"{marker}\n\n{pulse.body_markdown}"
    for name, expected in ((PULSE_TXT, full_text), (PULSE_MD, full_markdown)):
        file = directory / name
        if not file.exists() or file.read_text("utf-8") != expected:
            problems.append(f"{name} is missing or differs from pulse.json")

    content = PulseContent(
        iso_week=pulse.iso_week,
        window_start=pulse.window_start,
        window_end=pulse.window_end,
        n_collected=pulse.n_collected,
        n_analyzed=pulse.n_analyzed,
        themes=tuple(pulse.themes),
        quotes=tuple(pulse.quotes),
        actions=tuple(pulse.actions),
        fallback_clustering=document.source.get("theme_method") == METHOD_FALLBACK,
        synthetic=document.synthetic_fixture_data,
    )
    try:
        rendered = render(content, document.detail_level)
    except ValueError:
        problems.append(f"detail_level {document.detail_level} is invalid")
    else:
        if (rendered.body_text, rendered.body_markdown) != (pulse.body_text, pulse.body_markdown):
            problems.append("the body does not re-render from its themes, quotes, and actions")
        for check in validate_rendered(content, rendered, allow_synthetic=allow_synthetic):
            if not check.passed:
                problems.append(f"{check.name}: {check.detail}")
    if pulse.word_count > MAX_WORDS:
        problems.append(f"word_count {pulse.word_count} exceeds {MAX_WORDS}")
    if problems:
        raise DeliveryError(f"{pulse_json} is not publishable: " + "; ".join(problems))
    return PublishablePulse(
        run_id=document.run_id,
        directory=directory,
        iso_week=pulse.iso_week,
        content_hash=pulse.content_hash,
        marker=marker,
        full_text=full_text,
        full_markdown=full_markdown,
        word_count=pulse.word_count,
        synthetic=document.synthetic_fixture_data,
    )


# ---------------------------------------------------------------- plan
def draft_subject(iso_week: str) -> str:
    return f"{SUBJECT_PREFIX} — {iso_week}"


def doc_arguments(pulse: PublishablePulse, document_id: str) -> dict[str, Any]:
    return {"documentId": document_id, "content": pulse.full_text, "ensureLeadingNewline": True}


def draft_arguments(pulse: PublishablePulse, recipients: Sequence[str]) -> dict[str, Any]:
    return {
        "to": list(recipients),
        "subject": draft_subject(pulse.iso_week),
        "body": pulse.full_text,
        "bodyType": "text",
    }


class TargetStatus(StrEnum):
    WOULD_DELIVER = "would_deliver"
    WOULD_SKIP = "would_skip_already_delivered"
    DELIVERED = "delivered"
    SKIPPED = "skipped_already_delivered"
    BLOCKED = "blocked_outcome_unknown"
    BLOCKED_WEEK = "blocked_week_already_delivered"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    NOT_ATTEMPTED = "not_attempted"
    NOT_CONFIGURED = "not_configured"


_DONE = {TargetStatus.DELIVERED, TargetStatus.SKIPPED}


@dataclass
class TargetReport:
    target: Target
    tool: str
    status: TargetStatus
    key: str | None
    arguments_preview: dict[str, Any]
    detail: str = ""
    result: ToolCallResult | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.value,
            "tool": self.tool,
            "status": self.status.value,
            "idempotency_key": self.key,
            "detail": self.detail,
            "arguments_preview": self.arguments_preview,
            "result": self.result.as_dict() if self.result else None,
        }


@dataclass
class DeliveryReport:
    run_id: str
    mode: DeliveryMode
    pulse_run_id: str
    iso_week: str
    content_hash: str
    targets: list[TargetReport]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    health: dict[str, Any] | None = None
    forced: bool = False
    output_path: Path | None = None

    @property
    def ok(self) -> bool:
        if self.mode is DeliveryMode.DRY_RUN:
            return True
        return all(t.status in _DONE for t in self.targets)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat(),
            "mode": self.mode.value,
            "ok": self.ok,
            "forced": self.forced,
            "pulse_run_id": self.pulse_run_id,
            "iso_week": self.iso_week,
            "content_hash": self.content_hash,
            "email_sent": False,  # this application has no code path that sends email
            "health": self.health,
            "targets": [t.as_dict() for t in self.targets],
        }


def _preview(arguments: dict[str, Any]) -> dict[str, Any]:
    """Arguments as they will be sent, with the Doc ID and addresses masked."""
    preview = dict(arguments)
    if "documentId" in preview:
        preview["documentId"] = "<GOOGLE_DOC_ID>"
    if "to" in preview:
        preview["to"] = [f"<recipient {i}>" for i in range(1, len(preview["to"]) + 1)]
    return preview


@dataclass
class _Step:
    report: TargetReport
    arguments: dict[str, Any]
    expected_document_id: str | None = None


def _latest(entries: list[LedgerEntry], key: str) -> LedgerEntry | None:
    found = None
    for entry in entries:
        if entry.key == key:
            found = entry
    return found


def _decide(
    entries: list[LedgerEntry], lock: WeekLock, target: Target, key: str, *, force: bool,
    dry_run: bool, recovery: str,
) -> tuple[TargetStatus | None, str]:  # fmt: skip
    """``(None, …)`` means deliver; otherwise the status that replaces delivery.

    Order: this exact content already delivered (skip), then the week lock, then the
    ordinary per-key rules.
    """
    latest = _latest(entries, key)
    if latest is not None and latest.status == LedgerStatus.SUCCEEDED and not force:
        status = TargetStatus.WOULD_SKIP if dry_run else TargetStatus.SKIPPED
        return status, f"already delivered (run {latest.run_id})"
    if reason := lock.reason(target, force=force):
        return TargetStatus.BLOCKED_WEEK, reason + recovery
    if latest is None or latest.status == LedgerStatus.FAILED:
        return (TargetStatus.WOULD_DELIVER if dry_run else None), ""
    if force:
        return (TargetStatus.WOULD_DELIVER if dry_run else None), (
            f"forced although the ledger says {latest.status}"
        )
    return TargetStatus.BLOCKED, (
        f"previous attempt (run {latest.run_id}) ended '{latest.status}': the write may have "
        "happened. Check the Doc or Drafts folder, then re-run with --force only if it did not."
    )


def _recovery_hint(settings: Settings, entries: list[LedgerEntry], lock: WeekLock) -> str:
    """How to finish the version that already reached part of the week, if one did."""
    for content_hash in lock.delivered_hashes:
        reached = week_lock(entries, lock.iso_week, content_hash).own_succeeded
        if reached >= {t.value for t in Target}:
            continue
        directory = find_pulse_by_hash(settings.data_dir, lock.iso_week, content_hash)
        if directory is None:
            return (
                f" Version {content_hash[:12]} is only partly delivered and its pulse files "
                "are no longer under DATA_DIR/runs, so it cannot be finished from here."
            )
        return (
            f" To finish the partly delivered version {content_hash[:12]}, deliver its "
            f"pulse instead: groww-pulse publish --pulse {directory} --confirm"
        )
    return ""


def _plan(
    settings: Settings, pulse: PublishablePulse, ledger: DeliveryLedger, *, force: bool,
    dry_run: bool,
) -> list[_Step]:  # fmt: skip
    steps: list[_Step] = []
    entries = ledger.entries()
    lock = week_lock(entries, pulse.iso_week, pulse.content_hash)
    recovery = _recovery_hint(settings, entries, lock) if lock.delivered else ""
    document_id = settings.google_doc_id.get_secret_value() if settings.google_doc_id else None
    recipients = settings.pulse_recipients
    targets: list[tuple[Target, str, dict[str, Any] | None, str | None]] = [
        (
            Target.GOOGLE_DOC,
            DOC_APPEND_TOOL,
            doc_arguments(pulse, document_id) if document_id else None,
            doc_key(pulse.iso_week, pulse.content_hash, document_id) if document_id else None,
        ),
        (
            Target.GMAIL_DRAFT,
            DRAFT_TOOL,
            draft_arguments(pulse, recipients) if recipients else None,
            draft_key(pulse.iso_week, pulse.content_hash, recipients) if recipients else None,
        ),
    ]
    for target, tool, arguments, key in targets:
        if arguments is None or key is None:
            missing = "GOOGLE_DOC_ID" if target is Target.GOOGLE_DOC else "PULSE_EMAIL_TO"
            report = TargetReport(
                target, tool, TargetStatus.NOT_CONFIGURED, None, {}, f"{missing} is not set"
            )
            steps.append(_Step(report, {}))
            continue
        status, detail = _decide(
            entries, lock, target, key, force=force, dry_run=dry_run, recovery=recovery
        )
        report = TargetReport(
            target,
            tool,
            status or TargetStatus.NOT_ATTEMPTED,
            key,
            _preview(arguments),
            detail,
        )
        steps.append(_Step(report, arguments, document_id if target is Target.GOOGLE_DOC else None))
    return steps


def _pending(step: _Step) -> bool:
    return step.report.status is TargetStatus.NOT_ATTEMPTED


# ---------------------------------------------------------------- publish
def _require_live(settings: Settings, confirm: bool) -> None:
    if not confirm:
        raise DeliveryError("live delivery needs the explicit --confirm flag")
    if settings.delivery_mode is not DeliveryMode.CONFIRM:
        raise DeliveryError(
            "live delivery needs DELIVERY_MODE=confirm in the configuration as well as --confirm"
        )
    settings.require_delivery()
    if settings.mcp_auth_token is None:
        raise SecurityGateError(
            "MCP_AUTH_TOKEN is not set. Live delivery is only allowed to an MCP server that "
            "authenticates its clients (docs/mcp-server-security.md)"
        )


def mcp_client_for(settings: Settings) -> McpClient:
    """The configured client (``MCP_SERVER_URL``, optional ``MCP_AUTH_TOKEN``)."""
    token = settings.mcp_auth_token.get_secret_value() if settings.mcp_auth_token else None
    return McpClient(
        str(settings.mcp_server_url), auth_token=token, timeout=settings.mcp_timeout_seconds
    )


def _record(
    ledger: DeliveryLedger,
    step: _Step,
    status: LedgerStatus,
    pulse: PublishablePulse,
    run_id: str,
    *,
    forced: bool,
    result: ToolCallResult | None = None,
) -> None:
    if step.report.key is None:  # pragma: no cover — unconfigured targets are never called
        raise DeliveryError(f"{step.report.target} has no idempotency key")
    ledger.record(
        LedgerEntry.make(
            key=step.report.key,
            target=step.report.target,
            status=status,
            iso_week=pulse.iso_week,
            content_hash=pulse.content_hash,
            run_id=run_id,
            forced=forced,
            error_code=result.error_code if result else None,
            response=result.excerpt if result else None,
        )
    )


_LEDGER_STATUS = {
    ToolOutcome.SUCCEEDED: LedgerStatus.SUCCEEDED,
    ToolOutcome.FAILED: LedgerStatus.FAILED,
    ToolOutcome.UNKNOWN: LedgerStatus.UNKNOWN,
}
_TARGET_STATUS = {
    ToolOutcome.SUCCEEDED: TargetStatus.DELIVERED,
    ToolOutcome.FAILED: TargetStatus.FAILED,
    ToolOutcome.UNKNOWN: TargetStatus.OUTCOME_UNKNOWN,
}


def _apply_result(
    ledger: DeliveryLedger, step: _Step, result: ToolCallResult, pulse: PublishablePulse,
    run_id: str, forced: bool,
) -> None:  # fmt: skip
    _record(ledger, step, _LEDGER_STATUS[result.outcome], pulse, run_id, forced=forced,
            result=result)  # fmt: skip
    step.report.status = _TARGET_STATUS[result.outcome]
    step.report.result = result
    if result.google_auth_failure:
        step.report.detail = (
            "the MCP server's own Google authorization failed; the server owner must "
            "re-authorize it"
        )
    elif result.outcome is not ToolOutcome.SUCCEEDED:
        step.report.detail = f"{result.error_code}: {result.error_message}"
    log.info(
        "MCP tool call finished",
        extra={"tool": result.tool, "outcome": result.outcome.value, "code": result.error_code},
    )


async def _deliver(
    client: McpClient,
    steps: list[_Step],
    ledger: DeliveryLedger,
    pulse: PublishablePulse,
    run_id: str,
    *,
    forced: bool,
) -> HealthReport:
    health = await client.check(probe_auth=True)
    if not health.healthy:
        error = health.error or "required MCP tools are missing or changed"
        if health.credentials_rejected:
            raise MCPAuthError(error)
        raise MCPUnavailableError(error)
    if not health.security_gate_passed:
        raise SecurityGateError(
            f"live delivery refused: {health.auth_detail}. The MCP server must reject "
            "unauthenticated clients first (docs/mcp-server-security.md)"
        )

    # The step whose tools/call may have reached the server. Transport errors inside the
    # SDK's task group surface when the session closes, not from call_tool, so the step is
    # tracked here and classified in the except block.
    in_flight: _Step | None = None
    try:
        async with client.open() as session:
            for step in steps:
                if not _pending(step):
                    continue
                _record(ledger, step, LedgerStatus.PENDING, pulse, run_id, forced=forced)
                in_flight = step
                result = await _call(session, step)
                in_flight = None
                _apply_result(ledger, step, result, pulse, run_id, forced)
                if result.outcome is ToolOutcome.UNKNOWN:
                    break  # the connection may be unhealthy; do not risk the next write
    except Exception as exc:
        if in_flight is not None:
            failure = call_failure(in_flight.report.tool, exc)
            _apply_result(ledger, in_flight, failure, pulse, run_id, forced)
        elif any(s.report.result for s in steps):
            log.warning("MCP session closed with an error", extra={"error": describe(exc)})
        elif isinstance(exc, DeliveryError):
            raise
        else:
            raise MCPUnavailableError(f"MCP session failed: {describe(exc)}") from None
    for step in steps:
        if _pending(step):
            step.report.detail = "not attempted: an earlier step did not complete"
    return health


async def _call(session: ToolSession, step: _Step) -> ToolCallResult:
    return await session.call(
        step.report.tool, step.arguments, expected_document_id=step.expected_document_id
    )


def publish(
    settings: Settings,
    *,
    run_id: str,
    pulse_path: Path | None = None,
    confirm: bool = False,
    force: bool = False,
    client: McpClient | None = None,
    allow_synthetic: bool = False,
) -> DeliveryReport:
    """Dry run (default) or live delivery of a validated pulse; see the module docstring.

    Raises :class:`DeliveryError` (or a subclass) when delivery is refused before any write.
    Write outcomes, including failures, are returned in the report, recorded in the ledger,
    and saved to ``runs/{run_id}/delivery.json``.
    """
    path = pulse_path or find_latest_pulse(settings.data_dir)
    pulse = load_publishable_pulse(path, allow_synthetic=allow_synthetic)
    dry_run = not confirm
    if not dry_run:
        _require_live(settings, confirm)
    ledger = DeliveryLedger(settings.data_dir / LEDGER_FILE)
    steps = _plan(settings, pulse, ledger, force=force, dry_run=dry_run)
    report = DeliveryReport(
        run_id=run_id,
        mode=DeliveryMode.DRY_RUN if dry_run else DeliveryMode.CONFIRM,
        pulse_run_id=pulse.run_id,
        iso_week=pulse.iso_week,
        content_hash=pulse.content_hash,
        targets=[s.report for s in steps],
        forced=force,
    )
    if not dry_run and any(_pending(s) for s in steps):
        mcp = client or mcp_client_for(settings)
        health = asyncio.run(_deliver(mcp, steps, ledger, pulse, run_id, forced=force))
        report.health = health.as_dict()

    report.output_path = settings.data_dir / "runs" / run_id / DELIVERY_FILE
    atomic_write_lines(
        report.output_path, [mask(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))]
    )
    log.info(
        "Publish finished",
        extra={
            "mode": report.mode.value,
            "ok": report.ok,
            "statuses": {t.target.value: t.status.value for t in report.targets},
        },
    )
    return report
