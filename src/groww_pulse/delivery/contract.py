"""MCP tool contract for the Google Workspace MCP server (Phase 0 spike).

Holds the tool allowlist and a pure evaluator for a ``tools/list`` response. The Phase 5
client (``mcp_client.py``) builds on these definitions; nothing here performs network I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from groww_pulse.errors import ForbiddenToolError

DOC_APPEND_TOOL = "google_docs_append_content"
DRAFT_TOOL = "gmail_create_draft"
SEND_TOOL = "gmail_send_email"

#: The only tools this application may ever call.
ALLOWED_TOOLS: frozenset[str] = frozenset({DOC_APPEND_TOOL, DRAFT_TOOL})

#: Known tools that must never be called. Listed so their presence is reported, not to use them.
FORBIDDEN_TOOLS: frozenset[str] = frozenset({SEND_TOOL})

#: Input fields each allowed tool must accept, with the JSON-schema type we rely on.
REQUIRED_INPUTS: Mapping[str, Mapping[str, str]] = {
    DOC_APPEND_TOOL: {"documentId": "string", "content": "string"},
    DRAFT_TOOL: {"to": "array", "subject": "string", "body": "string"},
}


def assert_allowed(tool_name: str) -> None:
    """Raise :class:`ForbiddenToolError` unless ``tool_name`` is on the allowlist."""
    if tool_name not in ALLOWED_TOOLS:
        raise ForbiddenToolError(
            f"Tool '{tool_name}' is not allowed; permitted tools: {sorted(ALLOWED_TOOLS)}"
        )


@dataclass(frozen=True)
class ToolSpec:
    name: str
    input_schema: Mapping[str, Any]
    description: str = ""


@dataclass
class ContractReport:
    """Outcome of comparing an observed ``tools/list`` result with :data:`REQUIRED_INPUTS`."""

    observed_tools: list[str]
    missing_tools: list[str] = field(default_factory=list)
    schema_problems: list[str] = field(default_factory=list)
    forbidden_tools_present: list[str] = field(default_factory=list)

    @property
    def compatible(self) -> bool:
        return not self.missing_tools and not self.schema_problems


def _schema_type(prop: Mapping[str, Any]) -> str | None:
    value = prop.get("type")
    if isinstance(value, str):
        return value
    if isinstance(value, list):  # e.g. ["string", "null"]
        non_null = [v for v in value if v != "null"]
        return non_null[0] if len(non_null) == 1 else None
    return None


def evaluate_tools(tools: Iterable[ToolSpec]) -> ContractReport:
    """Check that every allowed tool exists and accepts the inputs the publisher will send."""
    by_name = {t.name: t for t in tools}
    report = ContractReport(observed_tools=sorted(by_name))
    report.forbidden_tools_present = sorted(FORBIDDEN_TOOLS & by_name.keys())

    for tool_name, required in REQUIRED_INPUTS.items():
        spec = by_name.get(tool_name)
        if spec is None:
            report.missing_tools.append(tool_name)
            continue
        properties = spec.input_schema.get("properties") or {}
        for field_name, expected_type in required.items():
            prop = properties.get(field_name)
            if not isinstance(prop, Mapping):
                report.schema_problems.append(f"{tool_name}: missing input '{field_name}'")
                continue
            actual = _schema_type(prop)
            if actual != expected_type:
                report.schema_problems.append(
                    f"{tool_name}: input '{field_name}' has type {actual!r}, "
                    f"expected {expected_type!r}"
                )
    return report
