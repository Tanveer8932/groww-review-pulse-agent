# SYNTHETIC TEST FIXTURE — not real review data
"""An in-process fake of the Google Workspace MCP server for the Phase 5 tests.

* It lists the **exact tool definitions observed on the deployed server**
  (``docs/mcp-tools-observed.json``), including ``gmail_send_email``. The SDK's low-level
  ``Server`` validates arguments against those schemas, as the real server does.
* It speaks real MCP over Streamable HTTP. The client under test uses the SDK's
  ``streamable_http_client``, and an ``httpx`` transport routes requests to the fake
  in-process, so nothing touches the network.
* The transport also simulates what MCP alone cannot: bearer auth (401), an unreachable
  host, 503s during the handshake, a connection dropped **after** the server performed a
  write, a refused connection on a tool call, and a plain 200 from a non-MCP root URL.
* Every HTTP request and every ``tools/call`` is recorded, so tests can prove that nothing
  was sent (dry run, refusals) and that ``gmail_send_email`` was never called.

Results follow the envelope in docs/mcp-contract.md §4. The Doc and draft "succeed" only
inside this fake; no Google API is involved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from groww_pulse.delivery.contract import DOC_APPEND_TOOL, DRAFT_TOOL, SEND_TOOL
from groww_pulse.delivery.mcp_client import McpClient

OBSERVED_TOOLS_FILE = Path(__file__).resolve().parents[2] / "docs" / "mcp-tools-observed.json"
URL = "https://mcp.test/"

#: Per-tool behaviours: "ok"; "error:<CODE>"; "malformed" (ok without status);
#: "text_only" (success only as JSON text); "wrong_doc"; "read_error_after_write" (the
#: server writes, then the connection drops); "connect_error" (refused before sending);
#: "auth_401" (rejected by auth before the server sees it).
OK = "ok"


def observed_tools() -> list[dict[str, Any]]:
    data = json.loads(OBSERVED_TOOLS_FILE.read_text("utf-8"))
    return [dict(t) for t in data["tools"]]


@dataclass
class FakeMcpServer:
    token: str | None  # None: accept every client, like the deployed server today
    tools: list[dict[str, Any]] = field(default_factory=observed_tools)
    behaviour: dict[str, str] = field(default_factory=dict)
    unreachable: bool = False
    handshake_503s: int = 0
    not_mcp: bool = False
    requests: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    # ------------------------------------------------------------ inspection
    def calls_to(self, tool: str) -> list[dict[str, Any]]:
        return [args for name, args in self.calls if name == tool]

    @property
    def tool_call_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r.get("method") == "tools/call"]

    def drop_tool(self, name: str) -> None:
        self.tools = [t for t in self.tools if t["name"] != name]

    # ------------------------------------------------------------ MCP server
    def _server(self) -> Server[Any, Any]:
        server: Server[Any, Any] = Server("google-workspace-mcp-server", version="1.0.0")

        @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
        async def list_tools() -> list[types.Tool]:
            return [types.Tool.model_validate(t) for t in self.tools]

        @server.call_tool()  # type: ignore[untyped-decorator]
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            self.calls.append((name, dict(arguments)))
            return self._result(name, arguments)

        return server

    def _result(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        mode = self.behaviour.get(name, OK)
        if mode.startswith("error:"):
            code = mode.split(":", 1)[1]
            envelope: dict[str, Any] = {
                "ok": False,
                "action": name,
                # The message deliberately includes the Doc ID and an address, which the
                # client must mask before storing or printing it.
                "error": {
                    "code": code,
                    "message": f"{code} for {arguments.get('documentId', 'n/a')} "
                    "(owner admin@example.com)",
                    "retryable": False,
                },
            }
            return _envelope(envelope, is_error=True)
        if name == DOC_APPEND_TOOL:
            envelope = {
                "ok": True,
                "action": name,
                "status": "appended",
                "documentId": "another-document-id-0000000000"
                if mode == "wrong_doc"
                else arguments["documentId"],
                "charactersAppended": len(arguments["content"]),
                "revisionId": "rev-0001",
            }
        elif name == DRAFT_TOOL:
            envelope = {
                "ok": True,
                "action": name,
                "status": "draft_created",
                "draftId": "draft-0001",
                "threadId": "thread-0001",
            }
        else:  # gmail_send_email: recorded so tests can prove it never happens
            envelope = {"ok": True, "action": name, "status": "sent"}
        if mode == "malformed":
            envelope.pop("status")
        if mode == "text_only":
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(envelope))]
            )
        return _envelope(envelope, is_error=False)

    async def _asgi(self, scope: Any, receive: Any, send: Any) -> None:
        # Stateless + JSON responses: a fresh manager per request works in any event loop.
        manager = StreamableHTTPSessionManager(
            app=self._server(), stateless=True, json_response=True
        )
        async with manager.run():
            await manager.handle_request(scope, receive, send)

    # ------------------------------------------------------------ transport
    def transport(self) -> httpx.AsyncBaseTransport:
        return _FakeTransport(self)

    def http_client_factory(
        self, headers: dict[str, str], timeout: httpx.Timeout
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport(), headers=headers, timeout=timeout)

    def client(self, token: str | None, *, timeout: float = 5.0) -> McpClient:
        async def no_sleep(_: float) -> None:
            return None

        return McpClient(
            URL,
            auth_token=token,
            timeout=timeout,
            backoff_seconds=0.0,
            http_client_factory=self.http_client_factory,
            sleep=no_sleep,
        )


def _envelope(envelope: dict[str, Any], *, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(envelope))],
        structuredContent=envelope,
        isError=is_error,
    )


class _FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, fake: FakeMcpServer) -> None:
        self.fake = fake
        self.asgi = httpx.ASGITransport(app=fake._asgi)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        fake = self.fake
        body = await request.aread()
        message = _json_object(body)
        record = {
            "http_method": request.method,
            "method": message.get("method") if message else None,
            "tool": (message.get("params") or {}).get("name") if message else None,
            "authorization": request.headers.get("authorization"),
        }
        fake.requests.append(record)
        if fake.unreachable:
            raise httpx.ConnectError("connection refused (fake)", request=request)
        if fake.not_mcp:
            return httpx.Response(200, json={"service": "something", "status": "running"})
        if fake.handshake_503s > 0:
            fake.handshake_503s -= 1
            return httpx.Response(503, json={"error": "unavailable"})
        tool = record["tool"]
        mode = fake.behaviour.get(tool or "", OK)
        if record["method"] == "tools/call" and mode == "connect_error":
            raise httpx.ConnectError("connection refused (fake)", request=request)
        expected = f"Bearer {fake.token}" if fake.token is not None else None
        rejected = expected is not None and request.headers.get("authorization") != expected
        if rejected or (record["method"] == "tools/call" and mode == "auth_401"):
            return httpx.Response(401, json={"error": "unauthorized"})
        response = await self.asgi.handle_async_request(request)
        if record["method"] == "tools/call" and mode == "read_error_after_write":
            await response.aread()  # the server has already performed the write
            raise httpx.ReadError("connection reset after the write (fake)", request=request)
        return response


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = ["DOC_APPEND_TOOL", "DRAFT_TOOL", "SEND_TOOL", "URL", "FakeMcpServer", "observed_tools"]
