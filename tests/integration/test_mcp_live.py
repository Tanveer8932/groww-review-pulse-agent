"""Live, read-only MCP contract check. Runs only with RUN_LIVE_TESTS=1 and MCP_SERVER_URL set.

Performs ``initialize`` + ``tools/list`` only; no tool is ever called.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.mark.live
def test_deployed_server_satisfies_contract(live_env: dict[str, str]) -> None:
    if not live_env.get("MCP_SERVER_URL"):
        pytest.skip("MCP_SERVER_URL not set")
    result = subprocess.run(  # noqa: S603 — fixed interpreter and script path
        [sys.executable, str(ROOT / "scripts" / "check_mcp.py")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=live_env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"compatible": true' in result.stdout


@pytest.mark.live
def test_phase5_client_health_and_security_gate(live_env: dict[str, str]) -> None:
    """Phase 5 client against the deployed server: handshake, contract, auth probe only.

    No tool is called. The gate must be reported exactly as observed; while the server
    accepts unauthenticated clients, ``security_gate_passed`` is false.
    """
    import asyncio

    from groww_pulse.delivery.mcp_client import McpClient

    url = live_env.get("MCP_SERVER_URL")
    if not url:
        pytest.skip("MCP_SERVER_URL not set")
    client = McpClient(url, auth_token=live_env.get("MCP_AUTH_TOKEN") or None, timeout=30.0)
    report = asyncio.run(client.check(probe_auth=True))
    assert report.healthy, report.as_dict()
    if report.auth_enforced is not True:
        assert not report.security_gate_passed
