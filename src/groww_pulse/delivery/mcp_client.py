"""Streamable HTTP MCP client for the Google Workspace MCP server (Phase 5).

Built on the official ``mcp`` Python SDK. The endpoint is the server's **root URL**
(verified in Phase 0; ``docs/mcp-contract.md``). Rules:

* **Allowlist first.** :func:`contract.assert_allowed` runs before any network I/O, so
  ``gmail_send_email`` (or any unlisted tool) raises ``ForbiddenToolError`` and never leaves
  the process.
* **Health is a handshake.** ``initialize`` + ``tools/list`` + the contract check
  (:func:`contract.evaluate_tools`). A 200 from ``GET /`` is never a health signal. The
  contract is re-checked on every session because the server sets ``tools.listChanged``.
* **Retries only before a write can have happened.** Opening a session (connect,
  ``initialize``, ``tools/list``) is retried on transport errors with backoff. A
  ``tools/call`` is **never** retried: both write tools are ``idempotentHint=false``.
* **Strict results.** A call succeeds only if ``isError`` is not set, the payload has
  ``ok: true``, the expected ``status`` (``appended`` / ``draft_created``), and, for the Doc,
  the requested document. ``isError: true`` is a definite failure; anything else that is
  malformed or ambiguous is :attr:`ToolOutcome.UNKNOWN`, because the write may have happened.
* **Auth probe.** :meth:`McpClient.probe_auth` repeats the handshake without credentials and
  with a wrong token. Only if both are rejected with 401/403 does the server enforce auth.
* **No secrets out.** Error text is passed through :func:`logging_setup.mask` and the Doc ID
  is dropped from stored response excerpts.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, TextContent

from groww_pulse.delivery.contract import (
    DOC_APPEND_TOOL,
    DRAFT_TOOL,
    ContractReport,
    ToolSpec,
    assert_allowed,
    evaluate_tools,
)
from groww_pulse.errors import GrowwPulseError, MCPAuthError, MCPUnavailableError
from groww_pulse.logging_setup import mask

#: ``status`` value that confirms success, per allowed tool (docs/mcp-contract.md §4).
EXPECTED_STATUS: Mapping[str, str] = {
    DOC_APPEND_TOOL: "appended",
    DRAFT_TOOL: "draft_created",
}
#: Server error codes that mean the server's own Google authorization is broken.
GOOGLE_AUTH_CODES = frozenset(
    {
        "AUTH_NOT_CONFIGURED",
        "GOOGLE_AUTH_REQUIRED",
        "GOOGLE_TOKEN_REFRESH_FAILED",
        "INSUFFICIENT_SCOPE",
    }
)
#: Response fields safe to keep in the ledger and reports (no Doc ID, no message bodies).
_EXCERPT_FIELDS = (
    "ok",
    "action",
    "status",
    "charactersAppended",
    "revisionId",
    "draftId",
    "threadId",
)
_MAX_MESSAGE_CHARS = 200

HttpClientFactory = Callable[[dict[str, str], httpx.Timeout], httpx.AsyncClient]
Sleep = Callable[[float], Awaitable[None]]


def default_http_client(headers: dict[str, str], timeout: httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False)


# ---------------------------------------------------------------- error classification
def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaf_exceptions(inner)]
    return [exc]


def _own_error(exc: BaseException) -> GrowwPulseError | None:
    """The first application error inside ``exc`` (possibly an ExceptionGroup), if any."""
    return next((e for e in _leaf_exceptions(exc) if isinstance(e, GrowwPulseError)), None)


def _http_status(exc: BaseException) -> int | None:
    for leaf in _leaf_exceptions(exc):
        if isinstance(leaf, httpx.HTTPStatusError):
            return leaf.response.status_code
    return None


def is_auth_rejection(exc: BaseException) -> bool:
    return _http_status(exc) in (401, 403)


def is_connect_failure(exc: BaseException) -> bool:
    """True if no request bytes can have reached the server (connection never opened)."""
    leaves = _leaf_exceptions(exc)
    return bool(leaves) and all(isinstance(e, httpx.ConnectError) for e in leaves)


def _is_transient(exc: BaseException) -> bool:
    status = _http_status(exc)
    if status is not None:
        return status >= 500 or status == 429
    return any(isinstance(e, httpx.TransportError) for e in _leaf_exceptions(exc))


def describe(exc: BaseException) -> str:
    """Short, masked description of an exception (types and HTTP status only)."""
    status = _http_status(exc)
    names = sorted({type(e).__name__ for e in _leaf_exceptions(exc)})
    text = ", ".join(names) + (f" (HTTP {status})" if status is not None else "")
    return mask(text)


# ---------------------------------------------------------------- results
class ToolOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # the server definitely did not perform the write
    UNKNOWN = "unknown"  # the write may or may not have happened


@dataclass(frozen=True)
class ToolCallResult:
    tool: str
    outcome: ToolOutcome
    status: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool | None = None
    excerpt: dict[str, Any] = field(default_factory=dict)

    @property
    def google_auth_failure(self) -> bool:
        return self.error_code in GOOGLE_AUTH_CODES

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "outcome": self.outcome.value,
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "google_auth_failure": self.google_auth_failure,
            "response": self.excerpt,
        }


def _payload(result: CallToolResult) -> dict[str, Any] | None:
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    for item in result.content:  # the server also returns the envelope as JSON text
        if isinstance(item, TextContent):
            try:
                parsed = json.loads(item.text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _excerpt(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    excerpt = {k: payload[k] for k in _EXCERPT_FIELDS if k in payload}
    error = payload.get("error")
    if isinstance(error, Mapping):
        excerpt["error"] = {
            "code": str(error.get("code", "")),
            "retryable": error.get("retryable"),
            "message": mask(str(error.get("message", "")))[:_MAX_MESSAGE_CHARS],
        }
    return excerpt


def parse_tool_result(
    tool: str, result: CallToolResult, *, expected_document_id: str | None = None
) -> ToolCallResult:
    """Classify a ``CallToolResult`` strictly (see the module docstring)."""
    payload = _payload(result)
    excerpt = _excerpt(payload)
    if result.isError:
        error = payload.get("error") if payload else None
        error = error if isinstance(error, Mapping) else {}
        return ToolCallResult(
            tool=tool,
            outcome=ToolOutcome.FAILED,
            error_code=str(error.get("code") or "TOOL_ERROR"),
            error_message=excerpt.get("error", {}).get("message") or "isError=true",
            retryable=error.get("retryable") if isinstance(error.get("retryable"), bool) else None,
            excerpt=excerpt,
        )

    def unknown(code: str, message: str) -> ToolCallResult:
        return ToolCallResult(
            tool=tool, outcome=ToolOutcome.UNKNOWN, error_code=code, error_message=message,
            excerpt=excerpt,
        )  # fmt: skip

    if payload is None:
        return unknown("MALFORMED_RESPONSE", "success result without a JSON envelope")
    if payload.get("ok") is not True:
        return unknown("MALFORMED_RESPONSE", "success result without ok=true")
    if payload.get("action") not in (None, tool):
        return unknown("MALFORMED_RESPONSE", "result is for a different tool")
    status = payload.get("status")
    if status != EXPECTED_STATUS[tool]:
        return unknown("UNEXPECTED_STATUS", f"status is {mask(str(status))[:40]!r}")
    if (
        expected_document_id is not None
        and "documentId" in payload
        and payload["documentId"] != expected_document_id
    ):
        return unknown("DOCUMENT_MISMATCH", "the server appended to a different document")
    return ToolCallResult(tool=tool, outcome=ToolOutcome.SUCCEEDED, status=status, excerpt=excerpt)


# ---------------------------------------------------------------- health
@dataclass
class HealthReport:
    endpoint: str
    auth_header_sent: bool
    handshake_ok: bool = False
    protocol_version: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    contract: ContractReport | None = None
    auth_enforced: bool | None = None
    auth_detail: str = "not checked"
    credentials_rejected: bool = False
    error: str | None = None

    @property
    def healthy(self) -> bool:
        return self.handshake_ok and self.contract is not None and self.contract.compatible

    @property
    def security_gate_passed(self) -> bool:
        return self.healthy and self.auth_header_sent and self.auth_enforced is True

    def as_dict(self) -> dict[str, Any]:
        contract = self.contract
        return {
            "endpoint": self.endpoint,
            "healthy": self.healthy,
            "handshake_ok": self.handshake_ok,
            "protocol_version": self.protocol_version,
            "server": {"name": self.server_name, "version": self.server_version},
            "contract": None
            if contract is None
            else {
                "compatible": contract.compatible,
                "observed_tools": contract.observed_tools,
                "missing_tools": contract.missing_tools,
                "schema_problems": contract.schema_problems,
                "forbidden_tools_present": contract.forbidden_tools_present,
            },
            "auth_header_sent": self.auth_header_sent,
            "auth_enforced": self.auth_enforced,
            "auth_detail": self.auth_detail,
            "credentials_rejected": self.credentials_rejected,
            "security_gate_passed": self.security_gate_passed,
            "error": self.error,
        }


# ---------------------------------------------------------------- client
class ToolSession:
    """An open, contract-checked MCP session. Only allowlisted tools can be called."""

    def __init__(self, session: ClientSession, contract: ContractReport, timeout: float) -> None:
        self._session = session
        self.contract = contract
        self._timeout = timeout

    async def call(
        self, tool: str, arguments: dict[str, Any], *, expected_document_id: str | None = None
    ) -> ToolCallResult:
        assert_allowed(tool)  # before any I/O: gmail_send_email can never be called
        try:
            result = await self._session.call_tool(
                tool, arguments, read_timeout_seconds=timedelta(seconds=self._timeout)
            )
        except Exception as exc:  # noqa: BLE001 — classified below, never swallowed silently
            return call_failure(tool, exc)
        return parse_tool_result(tool, result, expected_document_id=expected_document_id)


def call_failure(tool: str, exc: BaseException) -> ToolCallResult:
    """Outcome of a ``tools/call`` that raised: definite only if the server never saw it."""
    if is_auth_rejection(exc):
        return ToolCallResult(
            tool, ToolOutcome.FAILED, error_code="MCP_AUTH_REJECTED", error_message=describe(exc)
        )
    if is_connect_failure(exc):
        return ToolCallResult(
            tool, ToolOutcome.FAILED, error_code="CONNECT_FAILED", error_message=describe(exc)
        )
    if isinstance(exc, McpError):
        code = "MCP_ERROR"
        message = mask(str(exc.error.message))[:_MAX_MESSAGE_CHARS]
    else:
        code, message = "TRANSPORT_ERROR", describe(exc)
    return ToolCallResult(tool, ToolOutcome.UNKNOWN, error_code=code, error_message=message)


class McpClient:
    """Client for one MCP endpoint. ``http_client_factory`` lets tests inject a transport."""

    def __init__(
        self,
        url: str,
        *,
        auth_token: str | None,
        timeout: float = 30.0,
        retries: int = 2,
        backoff_seconds: float = 0.5,
        http_client_factory: HttpClientFactory = default_http_client,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.url = url
        self._auth_token = auth_token
        self._timeout = timeout
        self._retries = retries
        self._backoff = backoff_seconds
        self._factory = http_client_factory
        self._sleep = sleep

    @property
    def auth_header_sent(self) -> bool:
        return self._auth_token is not None

    def _headers(self, token: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"} if token is not None else {}

    @asynccontextmanager
    async def _session(
        self, token: str | None
    ) -> AsyncIterator[tuple[ClientSession, Any, list[ToolSpec]]]:
        """Connect, ``initialize``, and ``tools/list``, retrying transient handshake failures.

        The SDK's transport runs in anyio task groups, so the contexts are nested with
        ``async with`` (structured), never held open manually: a failure inside them then
        surfaces as the real error when the group exits. Errors before the handshake
        completes are classified and maybe retried; errors after ``yield`` (the caller's
        tool calls) propagate unchanged and are never retried.
        """
        http_timeout = httpx.Timeout(self._timeout, read=max(self._timeout, 60.0))
        attempt = 0
        while True:
            handshake_done = False
            try:
                async with (
                    self._factory(self._headers(token), http_timeout) as http,
                    streamable_http_client(self.url, http_client=http) as (read, write, _),
                    ClientSession(read, write) as session,
                ):
                    # A server that answers 200 with a non-MCP body makes the SDK wait for a
                    # reply that never comes, so the whole handshake is time-bounded.
                    with anyio.fail_after(self._timeout):
                        init = await session.initialize()
                        listed = await session.list_tools()
                    specs = [
                        ToolSpec(t.name, t.inputSchema, t.description or "") for t in listed.tools
                    ]
                    handshake_done = True
                    yield session, init, specs
                return
            except Exception as exc:
                if handshake_done:
                    # Our own typed errors raised inside the session (ForbiddenToolError, a
                    # changed contract) come back wrapped in the SDK's ExceptionGroup.
                    typed = _own_error(exc)
                    if typed is not None:
                        raise typed from None
                    raise
                if is_auth_rejection(exc):
                    raise MCPAuthError(
                        f"MCP server rejected the credentials: {describe(exc)}"
                    ) from None
                if attempt < self._retries and _is_transient(exc):
                    attempt += 1
                    await self._sleep(self._backoff * attempt)
                    continue
                raise MCPUnavailableError(f"MCP handshake failed: {describe(exc)}") from None

    @asynccontextmanager
    async def open(self) -> AsyncIterator[ToolSession]:
        """An authenticated, contract-checked session; raises if the contract changed."""
        async with self._session(self._auth_token) as (session, _init, specs):
            contract = evaluate_tools(specs)
            if not contract.compatible:
                problems = contract.missing_tools + contract.schema_problems
                raise MCPUnavailableError("MCP tool contract changed: " + "; ".join(problems))
            yield ToolSession(session, contract, self._timeout)

    async def check(self, *, probe_auth: bool = True) -> HealthReport:
        """Handshake + ``tools/list`` + contract (+ the auth-enforcement probe). No tool calls."""
        report = HealthReport(endpoint=mask(self.url), auth_header_sent=self.auth_header_sent)
        try:
            async with self._session(self._auth_token) as (_session, init, specs):
                report.handshake_ok = True
                report.protocol_version = str(init.protocolVersion)
                report.server_name = init.serverInfo.name
                report.server_version = init.serverInfo.version
                report.contract = evaluate_tools(specs)
        except MCPUnavailableError as exc:
            report.credentials_rejected = isinstance(exc, MCPAuthError)
            report.error = str(exc)
            return report
        if probe_auth:
            report.auth_enforced, report.auth_detail = await self.probe_auth()
        return report

    async def probe_auth(self) -> tuple[bool | None, str]:
        """Does the server reject clients without valid credentials?

        Returns ``(True, …)`` only if a handshake with no ``Authorization`` header **and** one
        with a random wrong token are both rejected with HTTP 401/403. ``(False, …)`` if
        either is accepted; ``(None, …)`` if a probe failed for another reason.
        """
        probes = {"no credentials": None, "wrong token": secrets.token_urlsafe(32)}
        for label, token in probes.items():
            try:
                async with self._session(token):
                    pass
            except MCPAuthError:
                continue
            except MCPUnavailableError as exc:
                return None, f"auth probe ({label}) failed inconclusively: {exc}"
            return False, f"the server accepted a handshake with {label}"
        return True, "handshakes without credentials and with a wrong token were rejected"
