"""Provider-neutral LLM types (architecture §6.4).

A :class:`PromptRequest` is everything needed to ask for one structured (JSON) answer. An
:class:`LLMProvider` turns it into one network call and returns the raw text plus token
usage; validation, repair, caching, and budgeting live in :class:`~.client.LLMClient`, so
every provider gets them for free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict

Effort = Literal["low", "medium", "high"]


class ChatMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class PromptRequest:
    """One structured-output request built from a versioned prompt template."""

    name: str  # template name, e.g. "theme_assignment"
    version: str  # template version, e.g. "v1"
    system: str
    user: str
    schema: dict[str, Any]  # JSON Schema the provider must constrain output to
    max_tokens: int
    effort: Effort = "medium"

    @property
    def prompt_id(self) -> str:
        return f"{self.name}.{self.version}"


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str  # the model that actually served the request
    stop_reason: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMProvider(Protocol):
    """Transport to one LLM vendor. Implementations raise only ``LLMProviderError``
    (transport, auth, rate limit) or ``LLMOutputError`` (the model declined to answer)."""

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    def generate(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        schema: dict[str, Any],
        max_tokens: int,
        effort: Effort,
    ) -> ProviderResponse: ...
