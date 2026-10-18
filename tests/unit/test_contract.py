"""Tests for the MCP tool contract and allowlist (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from groww_pulse.delivery.contract import (
    ALLOWED_TOOLS,
    DOC_APPEND_TOOL,
    DRAFT_TOOL,
    SEND_TOOL,
    ToolSpec,
    assert_allowed,
    evaluate_tools,
)
from groww_pulse.errors import ForbiddenToolError

OBSERVED = Path(__file__).parents[2] / "docs" / "mcp-tools-observed.json"


def _spec(name: str, props: dict[str, Any]) -> ToolSpec:
    return ToolSpec(name, {"type": "object", "properties": props})


GOOD_TOOLS = [
    _spec(DOC_APPEND_TOOL, {"documentId": {"type": "string"}, "content": {"type": "string"}}),
    _spec(
        DRAFT_TOOL,
        {"to": {"type": "array"}, "subject": {"type": "string"}, "body": {"type": "string"}},
    ),
]


def test_send_tool_is_never_allowed() -> None:
    assert SEND_TOOL not in ALLOWED_TOOLS
    with pytest.raises(ForbiddenToolError, match=SEND_TOOL):
        assert_allowed(SEND_TOOL)


@pytest.mark.parametrize("name", ["", "unknown_tool", "GMAIL_CREATE_DRAFT", "gmail_send"])
def test_unknown_tools_are_rejected(name: str) -> None:
    with pytest.raises(ForbiddenToolError):
        assert_allowed(name)


@pytest.mark.parametrize("name", sorted(ALLOWED_TOOLS))
def test_allowed_tools_pass(name: str) -> None:
    assert_allowed(name)


def test_compatible_contract() -> None:
    report = evaluate_tools(GOOD_TOOLS)
    assert report.compatible
    assert report.forbidden_tools_present == []


def test_missing_tool_is_incompatible() -> None:
    report = evaluate_tools(GOOD_TOOLS[:1])
    assert not report.compatible
    assert report.missing_tools == [DRAFT_TOOL]


def test_schema_drift_is_reported() -> None:
    drifted = [
        GOOD_TOOLS[0],
        _spec(DRAFT_TOOL, {"to": {"type": "string"}, "subject": {"type": "string"}}),
    ]
    report = evaluate_tools(drifted)
    assert not report.compatible
    assert any("'to'" in p and "'array'" in p for p in report.schema_problems)
    assert any("'body'" in p for p in report.schema_problems)


def test_nullable_type_lists_are_understood() -> None:
    tools = [
        _spec(
            DOC_APPEND_TOOL,
            {"documentId": {"type": ["string", "null"]}, "content": {"type": "string"}},
        ),
        GOOD_TOOLS[1],
    ]
    assert evaluate_tools(tools).compatible


def test_forbidden_tool_presence_is_reported_not_used() -> None:
    tools = [*GOOD_TOOLS, _spec(SEND_TOOL, {})]
    report = evaluate_tools(tools)
    assert report.compatible
    assert report.forbidden_tools_present == [SEND_TOOL]


@pytest.mark.skipif(not OBSERVED.exists(), reason="observed contract not recorded yet")
def test_recorded_live_contract_is_compatible() -> None:
    """The tools/list output recorded in Phase 0 must satisfy the contract."""
    data = json.loads(OBSERVED.read_text(encoding="utf-8"))
    specs = [ToolSpec(t["name"], t["inputSchema"]) for t in data["tools"]]
    report = evaluate_tools(specs)
    assert report.compatible, report.schema_problems
    assert report.forbidden_tools_present == [SEND_TOOL]
