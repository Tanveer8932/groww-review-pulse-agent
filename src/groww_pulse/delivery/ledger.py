"""Append-only delivery ledger: the source of truth for idempotency (architecture §6.6).

The MCP server has no tool to read a Doc or list drafts, so this file decides whether a
pulse was already delivered. One JSON object per line in ``DATA_DIR/runs/ledger.jsonl``.
The latest entry for a key wins.

**Keys** (one per target, so a failed draft is retried without re-appending the Doc):

* Doc append: ``sha256("google_docs_append_content|{iso_week}|{content_hash}|{document_id}")``
* Draft: ``sha256("gmail_create_draft|{iso_week}|{content_hash}|{sorted lower-case recipients}")``

Keys are hashes, so neither the Doc ID nor a recipient address is stored in the ledger.

**Statuses.** ``pending`` is written *before* each tool call; the result then writes
``succeeded``, ``failed`` (the server definitely did not write), or ``unknown`` (it may have).
A key whose latest status is ``pending`` (the process stopped mid-call) or ``unknown`` is
**blocked**: it is never retried automatically, because both write tools are
non-idempotent. The operator checks the Doc or Drafts folder and then re-runs with
``--force``.

**One version per ISO week** (:func:`week_lock`). Keys include the content hash, so a
regenerated pulse for the same week would get fresh keys and pass the per-key check. The
week lock stops that. Live delivery of target ``T`` for content ``H`` is refused when
another content hash of the same week:

1. has ever ``succeeded`` on ``T`` (a second Doc section or a second draft), or
2. has ever ``succeeded`` on any target and ``H`` has succeeded on none (a new version of a
   week that is already delivered).

Rule 2 is what keeps a partial delivery recoverable: once version ``A`` reached the Doc,
its draft can still be created (``A`` has a success), while every target of a regenerated
version ``B`` is refused. These two rules cannot be overridden, not even with ``--force``.

A different version whose latest outcome is ``pending`` or ``unknown`` (and that never
succeeded) may have been written, so it blocks in the same way, but ``--force`` overrides
it once the operator has checked that nothing was written. ``failed`` never blocks.

A corrupt line makes the whole ledger unreadable (:class:`DeliveryError`). Guessing would
risk duplicate writes. Run one publish at a time; there is no file lock.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from groww_pulse.delivery.contract import DOC_APPEND_TOOL, DRAFT_TOOL
from groww_pulse.errors import DeliveryError

LEDGER_FILE = Path("runs") / "ledger.jsonl"


class LedgerStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class Target(StrEnum):
    GOOGLE_DOC = "google_doc"
    GMAIL_DRAFT = "gmail_draft"


TOOL_FOR_TARGET: Mapping[Target, str] = {
    Target.GOOGLE_DOC: DOC_APPEND_TOOL,
    Target.GMAIL_DRAFT: DRAFT_TOOL,
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def doc_key(iso_week: str, content_hash: str, document_id: str) -> str:
    return _sha256(f"{DOC_APPEND_TOOL}|{iso_week}|{content_hash}|{document_id}")


def draft_key(iso_week: str, content_hash: str, recipients: Iterable[str]) -> str:
    normalized = ",".join(sorted({r.strip().lower() for r in recipients}))
    return _sha256(f"{DRAFT_TOOL}|{iso_week}|{content_hash}|{normalized}")


@dataclass(frozen=True)
class LedgerEntry:
    key: str
    target: str
    tool: str
    status: str
    iso_week: str
    content_hash: str
    run_id: str
    timestamp: str
    forced: bool = False
    error_code: str | None = None
    response: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def make(
        cls,
        *,
        key: str,
        target: Target,
        status: LedgerStatus,
        iso_week: str,
        content_hash: str,
        run_id: str,
        forced: bool = False,
        error_code: str | None = None,
        response: Mapping[str, Any] | None = None,
    ) -> LedgerEntry:
        return cls(
            key=key,
            target=target.value,
            tool=TOOL_FOR_TARGET[target],
            status=status.value,
            iso_week=iso_week,
            content_hash=content_hash,
            run_id=run_id,
            timestamp=datetime.now(UTC).isoformat(),
            forced=forced,
            error_code=error_code,
            response=dict(response or {}),
        )


class DeliveryLedger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> list[LedgerEntry]:
        if not self.path.exists():
            return []
        entries = []
        with self.path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    entries.append(LedgerEntry(**json.loads(line)))
                except (ValueError, TypeError) as exc:
                    raise DeliveryError(
                        f"{self.path} line {line_no} is corrupt ({type(exc).__name__}); "
                        "delivery is refused because idempotency cannot be guaranteed. "
                        "Repair the line by hand."
                    ) from None
        return entries

    def latest(self, key: str) -> LedgerEntry | None:
        found = None
        for entry in self.entries():
            if entry.key == key:
                found = entry
        return found

    def record(self, entry: LedgerEntry) -> None:
        """Append one entry and fsync it, so it survives a crash right after the call."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(entry), ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


# ---------------------------------------------------------------- week lock
_TARGET_NOUN = {Target.GOOGLE_DOC.value: "Doc section", Target.GMAIL_DRAFT.value: "Gmail draft"}
_UNCERTAIN = {LedgerStatus.PENDING.value, LedgerStatus.UNKNOWN.value}


@dataclass(frozen=True)
class WeekLock:
    """Other versions of one ISO week in the ledger, seen from one content hash.

    ``delivered``: the first ``succeeded`` entry per (target, other hash).
    ``uncertain``: the latest entry per key of another hash that is ``pending`` or
    ``unknown`` and never succeeded. ``own_succeeded``: targets this hash already reached.
    """

    iso_week: str
    content_hash: str
    delivered: tuple[LedgerEntry, ...] = ()
    uncertain: tuple[LedgerEntry, ...] = ()
    own_succeeded: frozenset[str] = frozenset()

    @property
    def delivered_hashes(self) -> list[str]:
        """Other content hashes delivered this week, oldest first."""
        return list(dict.fromkeys(e.content_hash for e in self.delivered))

    def reason(self, target: Target, *, force: bool = False) -> str | None:
        """Why ``target`` must not be delivered for this hash, or ``None``. See module doc."""
        noun = _TARGET_NOUN[target.value]
        if hit := self._conflict(self.delivered, target):
            return (
                f"the {self.iso_week} pulse was already delivered with different content "
                f"({_describe(hit)}). Delivering this version would add a second {noun} for "
                "the week. Duplicate-week protection cannot be overridden, not even with --force."
            )
        if not force and (hit := self._conflict(self.uncertain, target)):
            return (
                f"an attempt to deliver a different version of the {self.iso_week} pulse "
                f"({_describe(hit)}) ended '{hit.status}': it may have been written. Check the "
                "Doc and the Drafts folder. If it was written, the week is delivered: finish "
                "that version instead. Only if nothing was written, re-run this version with "
                "--force (CLI only)."
            )
        return None

    def _conflict(self, entries: tuple[LedgerEntry, ...], target: Target) -> LedgerEntry | None:
        same_target = [e for e in entries if e.target == target.value]
        if same_target:
            return same_target[0]
        if entries and not self.own_succeeded:
            return entries[0]
        return None


def _describe(entry: LedgerEntry) -> str:
    return (
        f"{_TARGET_NOUN.get(entry.target, entry.target)}, content {entry.content_hash[:12]}, "
        f"run {entry.run_id}, {entry.timestamp[:16].replace('T', ' ')} UTC"
    )


def week_lock(entries: Iterable[LedgerEntry], iso_week: str, content_hash: str) -> WeekLock:
    """The :class:`WeekLock` for ``content_hash`` from ledger ``entries`` (oldest first)."""
    week = [e for e in entries if e.iso_week == iso_week]
    succeeded_keys = {e.key for e in week if e.status == LedgerStatus.SUCCEEDED}
    delivered: dict[tuple[str, str], LedgerEntry] = {}
    latest: dict[str, LedgerEntry] = {}
    own: set[str] = set()
    for e in week:
        if e.content_hash == content_hash:
            if e.status == LedgerStatus.SUCCEEDED:
                own.add(e.target)
            continue
        latest[e.key] = e
        if e.status == LedgerStatus.SUCCEEDED:
            delivered.setdefault((e.target, e.content_hash), e)
    uncertain = [
        e for key, e in latest.items() if e.status in _UNCERTAIN and key not in succeeded_keys
    ]
    return WeekLock(
        iso_week=iso_week,
        content_hash=content_hash,
        delivered=tuple(delivered.values()),
        uncertain=tuple(uncertain),
        own_succeeded=frozenset(own),
    )
