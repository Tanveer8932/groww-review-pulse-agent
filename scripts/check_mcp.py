"""Phase 0 MCP contract spike.

Verifies the deployed Google Workspace MCP server with a real MCP handshake
(``initialize`` + ``tools/list``). A 200 from the root URL or ``/health`` is reported for
context but is never treated as proof that MCP works.

Default mode is read-only: no tool is called. Two opt-in flags perform one-off test writes
against a TEST Google Doc / the operator's own address. Both go through the allowlist, and
``gmail_send_email`` is unreachable.

Usage:
    python scripts/check_mcp.py                       # handshake + tools/list only
    python scripts/check_mcp.py --json-out docs/mcp-tools-observed.json
    python scripts/check_mcp.py --test-draft --test-append --i-understand-this-writes

Reads MCP_SERVER_URL, MCP_AUTH_TOKEN (optional), and for test writes GOOGLE_DOC_ID and
PULSE_EMAIL_TO from the environment / .env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import InitializeResult, TextContent

from groww_pulse.config import Settings, load_settings
from groww_pulse.delivery.contract import (
    DOC_APPEND_TOOL,
    DRAFT_TOOL,
    ToolSpec,
    assert_allowed,
    evaluate_tools,
)
from groww_pulse.errors import ConfigError, GrowwPulseError, MCPToolError, MCPUnavailableError
from groww_pulse.logging_setup import bind_run_id, configure_logging, mask

log = logging.getLogger("groww_pulse.scripts.check_mcp")


def _probe_http(base_url: str, timeout: float) -> dict[str, Any]:
    """Context only: record what plain GET / and /health return."""
    out: dict[str, Any] = {}
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for path in ("/", "/health"):
            try:
                resp = client.get(httpx.URL(base_url).join(path))
                out[path] = {"status": resp.status_code, "body": resp.text[:300]}
            except httpx.HTTPError as exc:
                out[path] = {"error": type(exc).__name__}
    return out


@asynccontextmanager
async def _session(
    url: str, headers: dict[str, str], timeout: float
) -> AsyncIterator[tuple[ClientSession, InitializeResult]]:
    """Open a Streamable-HTTP MCP session and complete the ``initialize`` handshake."""
    http_timeout = httpx.Timeout(timeout, read=max(timeout, 60.0))
    async with (
        httpx.AsyncClient(headers=headers, timeout=http_timeout) as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write, _sid),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        yield session, init


async def _handshake(
    url: str, headers: dict[str, str], timeout: float
) -> tuple[dict[str, Any], list[ToolSpec], list[dict[str, Any]]]:
    async with _session(url, headers, timeout) as (session, init):
        listed = await session.list_tools()
    server = {
        "protocolVersion": str(init.protocolVersion),
        "serverInfo": init.serverInfo.model_dump(mode="json"),
        "capabilities": init.capabilities.model_dump(mode="json", exclude_none=True),
    }
    specs = [ToolSpec(t.name, t.inputSchema, t.description or "") for t in listed.tools]
    raw = [t.model_dump(mode="json", exclude_none=True) for t in listed.tools]
    return server, specs, raw


def _auth_enforced(url: str, headers: dict[str, str], timeout: float) -> bool:
    """Return True only if the server rejects an MCP handshake that carries no credentials.

    Called after an authenticated (or header-less) handshake has already succeeded.
    """
    if "Authorization" not in headers:
        return False  # the handshake we just completed carried no credentials
    unauthenticated = {k: v for k, v in headers.items() if k != "Authorization"}
    try:
        asyncio.run(_handshake(url, unauthenticated, timeout))
    except Exception:  # noqa: BLE001 — any rejection of the credential-less handshake counts
        return True
    return False


async def _call_allowed(
    url: str, headers: dict[str, str], timeout: float, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    assert_allowed(tool)  # raises ForbiddenToolError before any network I/O
    async with _session(url, headers, timeout) as (session, _init):
        result = await session.call_tool(tool, arguments)
    payload: dict[str, Any] = {
        "isError": bool(result.isError),
        "structuredContent": result.structuredContent,
        "text": [c.text for c in result.content if isinstance(c, TextContent)],
    }
    if result.isError:
        raise MCPToolError(f"{tool} returned isError=true: {mask(json.dumps(payload))}")
    return payload


def _test_writes(
    settings: Settings, url: str, headers: dict[str, str], args: argparse.Namespace
) -> dict[str, Any]:
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    results: dict[str, Any] = {}
    if args.test_append:
        if settings.google_doc_id is None:
            raise ConfigError("GOOGLE_DOC_ID is required for --test-append (use a TEST doc)")
        results[DOC_APPEND_TOOL] = asyncio.run(
            _call_allowed(
                url,
                headers,
                settings.mcp_timeout_seconds,
                DOC_APPEND_TOOL,
                {
                    "documentId": settings.google_doc_id.get_secret_value(),
                    "content": f"[groww-pulse:contract-test] MCP verification {stamp}. "
                    "Safe to delete.",
                    "ensureLeadingNewline": True,
                },
            )
        )
    if args.test_draft:
        if not settings.pulse_recipients:
            raise ConfigError("PULSE_EMAIL_TO is required for --test-draft (use your own address)")
        results[DRAFT_TOOL] = asyncio.run(
            _call_allowed(
                url,
                headers,
                settings.mcp_timeout_seconds,
                DRAFT_TOOL,
                {
                    "to": settings.pulse_recipients,
                    "subject": f"[groww-pulse contract test] {stamp}",
                    "body": "MCP contract verification draft. Not sent. Safe to delete.",
                    "bodyType": "text",
                },
            )
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json-out", type=Path, help="write the observed contract as JSON")
    parser.add_argument("--test-append", action="store_true", help="append one line to a TEST doc")
    parser.add_argument("--test-draft", action="store_true", help="create one Gmail draft")
    parser.add_argument("--i-understand-this-writes", action="store_true", dest="confirmed")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        configure_logging(settings.log_level, "text", settings.secret_values())
        settings.require_mcp()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    url = str(settings.mcp_server_url)
    headers = {}
    if settings.mcp_auth_token is not None:
        headers["Authorization"] = f"Bearer {settings.mcp_auth_token.get_secret_value()}"

    with bind_run_id():
        report: dict[str, Any] = {
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "endpoint": url,
            "auth_header_sent": bool(headers),
            "http_context_not_a_health_signal": _probe_http(url, settings.mcp_timeout_seconds),
        }
        try:
            server, specs, raw_tools = asyncio.run(
                _handshake(url, headers, settings.mcp_timeout_seconds)
            )
        except Exception as exc:  # noqa: BLE001 — any transport/protocol failure means unhealthy
            log.error("MCP handshake failed", extra={"error": type(exc).__name__})
            report["mcp_handshake"] = {"ok": False, "error": mask(f"{type(exc).__name__}: {exc}")}
            print(json.dumps(report, indent=2))
            return 1

        report["auth_enforced"] = _auth_enforced(url, headers, settings.mcp_timeout_seconds)
        if not report["auth_enforced"]:
            log.warning(
                "MCP server accepts unauthenticated clients; see docs/mcp-server-security.md"
            )

        contract = evaluate_tools(specs)
        report["mcp_handshake"] = {"ok": True, **server}
        report["tools"] = raw_tools
        report["contract"] = {
            "compatible": contract.compatible,
            "observed_tools": contract.observed_tools,
            "missing_tools": contract.missing_tools,
            "schema_problems": contract.schema_problems,
            "forbidden_tools_present": contract.forbidden_tools_present,
        }

        if args.test_append or args.test_draft:
            if not args.confirmed:
                print("Refusing test writes without --i-understand-this-writes", file=sys.stderr)
                return 2
            try:
                report["test_writes"] = _test_writes(settings, url, headers, args)
            except GrowwPulseError as exc:
                report["test_writes"] = {"ok": False, "error": mask(str(exc))}
                print(json.dumps(report, indent=2))
                return 1

        if args.json_out:
            args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        if not contract.compatible:
            log.error(str(MCPUnavailableError("required MCP tools missing or changed")))
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
