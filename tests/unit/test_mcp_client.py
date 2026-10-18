# SYNTHETIC TEST FIXTURE — not real review data
"""Phase 5 MCP client against the in-process fake server (no network)."""

from __future__ import annotations

import asyncio
import re
import secrets
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, ErrorData, TextContent

from groww_pulse.delivery.contract import DOC_APPEND_TOOL, DRAFT_TOOL, SEND_TOOL
from groww_pulse.delivery.mcp_client import (
    HealthReport,
    ToolOutcome,
    call_failure,
    parse_tool_result,
)
from groww_pulse.errors import ForbiddenToolError, MCPAuthError, MCPUnavailableError
from groww_pulse.logging_setup import register_secrets
from tests.fixtures.fake_mcp import FakeMcpServer

TOKEN = secrets.token_urlsafe(24)  # random per run: never a committed credential
DOC_ID = "fakeDocumentId_" + "x" * 20
DOC_ARGS = {"documentId": DOC_ID, "content": "pulse text", "ensureLeadingNewline": True}
DRAFT_ARGS = {"to": ["pm@example.com"], "subject": "s", "body": "pulse text", "bodyType": "text"}


def check(fake: FakeMcpServer, token: str | None = TOKEN, **kwargs: Any) -> HealthReport:
    return asyncio.run(fake.client(token, **kwargs).check())


# ---------------------------------------------------------------- health and security gate
def test_healthy_server_that_enforces_auth_passes_the_gate() -> None:
    fake = FakeMcpServer(token=TOKEN)
    report = check(fake)
    assert report.healthy
    assert report.auth_enforced is True
    assert report.security_gate_passed
    assert report.server_name == "google-workspace-mcp-server"
    assert report.contract is not None
    assert report.contract.forbidden_tools_present == [SEND_TOOL]
    assert fake.calls == []  # a health check never calls a tool
    # The probes went out without credentials and with a wrong token.
    auths = [r["authorization"] for r in fake.requests if r["method"] == "initialize"]
    assert auths[0] == f"Bearer {TOKEN}"
    assert None in auths
    assert any(a and a != f"Bearer {TOKEN}" for a in auths)


def test_unauthenticated_server_fails_the_gate() -> None:
    report = check(FakeMcpServer(token=None))
    assert report.healthy
    assert report.auth_enforced is False
    assert not report.security_gate_passed
    assert "accepted a handshake with no credentials" in report.auth_detail


def test_gate_needs_a_configured_token() -> None:
    report = check(FakeMcpServer(token=None), token=None)
    assert report.healthy
    assert not report.auth_header_sent
    assert not report.security_gate_passed


def test_rejected_credentials_are_reported() -> None:
    report = check(FakeMcpServer(token=TOKEN), token="wrong-" + TOKEN)
    assert not report.healthy
    assert report.credentials_rejected
    assert "rejected the credentials" in (report.error or "")


def test_missing_tool_makes_the_server_unhealthy_and_open_refuses() -> None:
    fake = FakeMcpServer(token=TOKEN)
    fake.drop_tool(DRAFT_TOOL)
    report = check(fake)
    assert not report.healthy
    assert report.contract is not None
    assert report.contract.missing_tools == [DRAFT_TOOL]

    async def open_session() -> None:
        async with fake.client(TOKEN).open():
            pass

    with pytest.raises(MCPUnavailableError, match="contract changed"):
        asyncio.run(open_session())


def test_changed_input_schema_is_incompatible() -> None:
    fake = FakeMcpServer(token=TOKEN)
    draft = next(t for t in fake.tools if t["name"] == DRAFT_TOOL)
    draft["inputSchema"]["properties"]["to"] = {"type": "string"}
    report = check(fake)
    assert report.contract is not None
    assert "input 'to' has type 'string'" in report.contract.schema_problems[0]


def test_root_url_200_without_mcp_is_unhealthy() -> None:
    fake = FakeMcpServer(token=TOKEN, not_mcp=True)
    report = check(fake, timeout=1.0)
    assert not report.healthy
    assert "handshake failed" in (report.error or "")


def test_unreachable_server_is_retried_then_unhealthy() -> None:
    fake = FakeMcpServer(token=TOKEN, unreachable=True)
    report = check(fake)
    assert not report.healthy
    assert "ConnectError" in (report.error or "")
    assert len(fake.requests) == 3  # 1 attempt + 2 retries


def test_transient_503s_during_handshake_are_retried() -> None:
    fake = FakeMcpServer(token=TOKEN, handshake_503s=2)
    report = asyncio.run(fake.client(TOKEN).check(probe_auth=False))
    assert report.healthy


# ---------------------------------------------------------------- allowlist
def _call(fake: FakeMcpServer, tool: str, args: dict[str, Any]) -> Any:
    async def go() -> Any:
        async with fake.client(TOKEN).open() as session:
            return await session.call(tool, args)

    return asyncio.run(go())


@pytest.mark.parametrize("tool", [SEND_TOOL, "delete_everything"])
def test_forbidden_tools_never_reach_the_server(tool: str) -> None:
    fake = FakeMcpServer(token=TOKEN)
    with pytest.raises(ForbiddenToolError):
        _call(fake, tool, DRAFT_ARGS)
    assert fake.calls == []
    assert fake.tool_call_requests == []


def test_the_send_tool_is_referenced_only_by_the_deny_path() -> None:
    """The send tool's name exists as code only in contract.py (allowlist + FORBIDDEN_TOOLS)."""
    src = Path(__file__).resolve().parents[2] / "src"
    pattern = re.compile(r"[\"']gmail_send_email[\"']|\bSEND_TOOL\b")
    hits = sorted(
        str(p.relative_to(src)).replace("\\", "/")
        for p in src.rglob("*.py")
        if pattern.search(p.read_text("utf-8"))
    )
    assert hits == ["groww_pulse/delivery/contract.py"]


# ---------------------------------------------------------------- tool results
@pytest.mark.parametrize(
    ("mode", "outcome", "code"),
    [
        ("ok", ToolOutcome.SUCCEEDED, None),
        ("text_only", ToolOutcome.SUCCEEDED, None),
        ("error:DOC_NOT_FOUND", ToolOutcome.FAILED, "DOC_NOT_FOUND"),
        ("error:GOOGLE_AUTH_REQUIRED", ToolOutcome.FAILED, "GOOGLE_AUTH_REQUIRED"),
        ("malformed", ToolOutcome.UNKNOWN, "UNEXPECTED_STATUS"),
        ("wrong_doc", ToolOutcome.UNKNOWN, "DOCUMENT_MISMATCH"),
    ],
)
def test_doc_append_results_are_parsed_strictly(
    mode: str, outcome: ToolOutcome, code: str | None
) -> None:
    fake = FakeMcpServer(token=TOKEN, behaviour={DOC_APPEND_TOOL: mode})

    async def go() -> Any:
        async with fake.client(TOKEN).open() as session:
            return await session.call(DOC_APPEND_TOOL, DOC_ARGS, expected_document_id=DOC_ID)

    result = asyncio.run(go())
    assert (result.outcome, result.error_code) == (outcome, code)
    assert result.google_auth_failure is (code == "GOOGLE_AUTH_REQUIRED")
    assert "documentId" not in result.excerpt  # the Doc ID is never kept


def test_error_messages_are_masked() -> None:
    register_secrets([DOC_ID])
    fake = FakeMcpServer(token=TOKEN, behaviour={DOC_APPEND_TOOL: "error:DOC_PERMISSION_DENIED"})

    async def go() -> Any:
        async with fake.client(TOKEN).open() as session:
            return await session.call(DOC_APPEND_TOOL, DOC_ARGS)

    result = asyncio.run(go())
    assert DOC_ID not in (result.error_message or "")
    assert "admin@example.com" not in (result.error_message or "")
    assert "DOC_PERMISSION_DENIED" in (result.error_message or "")


def test_draft_success() -> None:
    result = _call(FakeMcpServer(token=TOKEN), DRAFT_TOOL, DRAFT_ARGS)
    assert result.outcome is ToolOutcome.SUCCEEDED
    assert result.status == "draft_created"
    assert result.excerpt["draftId"] == "draft-0001"


def test_invalid_arguments_are_rejected_by_the_observed_schema() -> None:
    fake = FakeMcpServer(token=TOKEN)
    result = _call(fake, DRAFT_TOOL, {**DRAFT_ARGS, "unexpected": 1})
    assert result.outcome is ToolOutcome.FAILED  # isError from the server
    assert fake.calls == []  # the schema rejected it before the tool ran


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (CallToolResult(content=[]), "MALFORMED_RESPONSE"),
        (
            CallToolResult(content=[TextContent(type="text", text="done")]),
            "MALFORMED_RESPONSE",
        ),
        (
            CallToolResult(content=[], structuredContent={"ok": False, "status": "appended"}),
            "MALFORMED_RESPONSE",
        ),
        (
            CallToolResult(
                content=[], structuredContent={"ok": True, "action": DRAFT_TOOL, "status": "x"}
            ),
            "MALFORMED_RESPONSE",
        ),
    ],
)
def test_malformed_success_results_are_unknown(result: CallToolResult, code: str) -> None:
    parsed = parse_tool_result(DOC_APPEND_TOOL, result)
    assert (parsed.outcome, parsed.error_code) == (ToolOutcome.UNKNOWN, code)


def test_is_error_without_envelope_is_a_definite_failure() -> None:
    parsed = parse_tool_result(DRAFT_TOOL, CallToolResult(content=[], isError=True))
    assert (parsed.outcome, parsed.error_code) == (ToolOutcome.FAILED, "TOOL_ERROR")


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://mcp.test/")
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(status))


@pytest.mark.parametrize(
    ("exc", "outcome", "code"),
    [
        (
            ExceptionGroup("g", [httpx.ConnectError("refused")]),
            ToolOutcome.FAILED,
            "CONNECT_FAILED",
        ),
        (ExceptionGroup("g", [_status_error(401)]), ToolOutcome.FAILED, "MCP_AUTH_REJECTED"),
        (_status_error(403), ToolOutcome.FAILED, "MCP_AUTH_REJECTED"),
        (ExceptionGroup("g", [httpx.ReadError("reset")]), ToolOutcome.UNKNOWN, "TRANSPORT_ERROR"),
        (_status_error(502), ToolOutcome.UNKNOWN, "TRANSPORT_ERROR"),
        (McpError(ErrorData(code=-1, message="timed out")), ToolOutcome.UNKNOWN, "MCP_ERROR"),
        (TimeoutError(), ToolOutcome.UNKNOWN, "TRANSPORT_ERROR"),
    ],
)
def test_call_failures_are_definite_only_when_nothing_was_sent(
    exc: BaseException, outcome: ToolOutcome, code: str
) -> None:
    result = call_failure(DOC_APPEND_TOOL, exc)
    assert (result.outcome, result.error_code) == (outcome, code)


def test_auth_rejection_is_typed() -> None:
    async def go() -> None:
        async with FakeMcpServer(token=TOKEN).client("nope").open():
            pass

    with pytest.raises(MCPAuthError):
        asyncio.run(go())
