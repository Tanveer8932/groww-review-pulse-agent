"""Typed exception hierarchy (architecture §7).

Every layer raises only its own subclass, so callers can catch precisely and the UI can
show a human-readable message alongside the run ID.
"""

from __future__ import annotations


class GrowwPulseError(Exception):
    """Base class for all application errors."""


class ConfigError(GrowwPulseError):
    """Missing or invalid configuration (environment variables / secrets)."""


# ---------------------------------------------------------------- collection
class CollectionError(GrowwPulseError):
    """Review acquisition failed."""


class ImportSchemaError(CollectionError):
    """An import file is unreadable or structurally invalid (row-level issues are quarantined)."""


class CollectorDisabledError(CollectionError):
    """Automated collection was requested; it is rejected (see docs/data-source-decision.md)."""


# ---------------------------------------------------------------- preprocessing
class PreprocessingError(GrowwPulseError):
    """Filtering, deduplication, or PII redaction failed."""


# ---------------------------------------------------------------- analysis
class AnalysisError(GrowwPulseError):
    """Theme analysis failed."""


class LLMProviderError(AnalysisError):
    """Transport, authentication, or rate-limit failure talking to the LLM provider."""


class LLMOutputError(AnalysisError):
    """LLM output failed schema validation after the repair retry."""


class BudgetExceededError(AnalysisError):
    """The per-run LLM token budget would be exceeded."""


class InsufficientDataError(AnalysisError):
    """Too few reviews, themes, or quotable reviews to produce an honest analysis."""


class QuoteVerificationError(AnalysisError):
    """A selected quote is not a verbatim span of a stored review with verified provenance."""


# ---------------------------------------------------------------- report
class ReportValidationError(GrowwPulseError):
    """The rendered pulse failed a validator (word limit, 3/3/3, PII, quote verification)."""


# ---------------------------------------------------------------- delivery
class DeliveryError(GrowwPulseError):
    """MCP delivery failed."""


class MCPUnavailableError(DeliveryError):
    """MCP handshake or tools/list failed, or required tools are missing."""


class MCPAuthError(MCPUnavailableError):
    """The MCP server rejected the client's credentials (HTTP 401/403)."""


class SecurityGateError(DeliveryError):
    """Live delivery refused: the MCP server does not provably authenticate its clients."""


class ForbiddenToolError(DeliveryError):
    """A tool outside the allowlist was requested (for example gmail_send_email)."""


class MCPToolError(DeliveryError):
    """A tool call returned isError=true or a malformed result."""
