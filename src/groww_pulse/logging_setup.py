"""Structured logging with run IDs and secret masking (architecture §10, NFR-5).

Every formatted record — message, extra fields, and traceback — passes through
:class:`SecretMasker` before it is written, so API keys, bearer tokens, Google OAuth tokens,
document IDs, and email addresses never reach log output.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TextIO

_HANDLER_MARKER = "_groww_pulse_handler"
_NO_RUN = "-"

_run_id: ContextVar[str] = ContextVar("run_id", default=_NO_RUN)

# Attributes present on every LogRecord; anything else came from `extra=` and is emitted.
_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._\-]+"), "[REDACTED_GOOGLE_TOKEN]"),
    (re.compile(r"\b1//[A-Za-z0-9._\-]{10,}"), "[REDACTED_GOOGLE_TOKEN]"),
    (
        re.compile(r"(docs\.google\.com/document/(?:u/\d+/)?d/)[A-Za-z0-9_-]+"),
        r"\1[REDACTED_DOC_ID]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
            r"client[_-]?secret|password|secret|token)"
            r"([\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
)


class SecretMasker:
    """Replaces registered literal secrets and well-known token shapes in text."""

    _MIN_LITERAL_LEN = 4

    def __init__(self, literals: Iterable[str] = ()) -> None:
        self._literals: set[str] = set()
        self.register(literals)

    def register(self, literals: Iterable[str]) -> None:
        """Add literal values (API keys, doc IDs, ...) that must never be logged."""
        self._literals.update(v for v in literals if v and len(v) >= self._MIN_LITERAL_LEN)

    def mask(self, text: str) -> str:
        # Longest first so a secret containing another secret is fully replaced.
        for literal in sorted(self._literals, key=len, reverse=True):
            text = text.replace(literal, "[REDACTED]")
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text


_masker = SecretMasker()


def register_secrets(values: Iterable[str]) -> None:
    """Register additional literal secrets with the process-wide masker."""
    _masker.register(values)


def mask(text: str) -> str:
    """Mask secrets in arbitrary text using the process-wide masker."""
    return _masker.mask(text)


class _MaskingFormatter(logging.Formatter):
    def __init__(self, masker: SecretMasker, fmt: str) -> None:
        super().__init__()
        self._masker = masker
        self._fmt_kind = fmt

    def format(self, record: logging.LogRecord) -> str:
        run_id = getattr(record, "run_id", None) or _run_id.get()
        extras = {
            k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS and k != "run_id"
        }
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat()
        if self._fmt_kind == "json":
            payload: dict[str, object] = {
                "ts": timestamp,
                "level": record.levelname,
                "logger": record.name,
                "run_id": run_id,
                "msg": record.getMessage(),
                **extras,
            }
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            line = json.dumps(payload, default=str, ensure_ascii=False)
        else:
            extra_txt = " ".join(f"{k}={v}" for k, v in extras.items())
            line = (
                f"{timestamp} {record.levelname:<8} [{run_id}] {record.name}: "
                f"{record.getMessage()}{' ' + extra_txt if extra_txt else ''}"
            )
            if record.exc_info:
                line += "\n" + self.formatException(record.exc_info)
        return self._masker.mask(line)


class _CurrentStderrHandler(logging.Handler):
    """Writes to whatever ``sys.stderr`` is at emit time.

    ``StreamHandler(sys.stderr)`` binds the stream once; if a host (a CLI test runner,
    Streamlit) later swaps and closes it, every subsequent log call errors.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(self.format(record) + "\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 — logging must never raise into callers
            self.handleError(record)


def configure_logging(
    level: str = "INFO",
    fmt: str = "json",
    secrets: Iterable[str] = (),
    stream: TextIO | None = None,
) -> None:
    """Install (or reinstall) the masking handler on the root logger. Safe to call repeatedly."""
    register_secrets(secrets)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARKER, False):
            root.removeHandler(existing)
    handler: logging.Handler = (
        logging.StreamHandler(stream) if stream is not None else _CurrentStderrHandler()
    )
    handler.setFormatter(_MaskingFormatter(_masker, fmt))
    setattr(handler, _HANDLER_MARKER, True)
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Third-party loggers stay at WARNING or above. The MCP SDK logs full tool arguments
    # (Doc ID, recipients) at DEBUG; the formatter masks those too, but they are kept out.
    for noisy in ("httpx", "httpcore", "urllib3", "anthropic", "mcp"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))


def new_run_id() -> str:
    """Return a sortable, unique run identifier such as ``20261014T081500Z-1a2b3c``."""
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def current_run_id() -> str:
    return _run_id.get()


@contextmanager
def bind_run_id(run_id: str | None = None) -> Iterator[str]:
    """Attach a run ID to every log record emitted inside the block."""
    value = run_id or new_run_id()
    token = _run_id.set(value)
    try:
        yield value
    finally:
        _run_id.reset(token)
