"""LLM provider abstraction: structured outputs, validation with one repair retry, cache,
and token budget. Anthropic Claude is the default provider; Groq Cloud is optional."""

from __future__ import annotations

from pathlib import Path

from groww_pulse.analysis.llm.anthropic_provider import AnthropicProvider
from groww_pulse.analysis.llm.base import (
    ChatMessage,
    Effort,
    LLMProvider,
    PromptRequest,
    ProviderResponse,
)
from groww_pulse.analysis.llm.client import (
    LLMClient,
    LLMUsage,
    OutputRejectedError,
    ResponseCache,
    TokenBudget,
)
from groww_pulse.analysis.llm.groq_provider import GroqProvider
from groww_pulse.config import LLMProvider as ProviderName
from groww_pulse.config import Settings
from groww_pulse.errors import ConfigError

CACHE_DIR = Path("cache") / "llm"

__all__ = [
    "AnthropicProvider",
    "ChatMessage",
    "Effort",
    "GroqProvider",
    "LLMClient",
    "LLMProvider",
    "LLMUsage",
    "OutputRejectedError",
    "PromptRequest",
    "ProviderResponse",
    "ResponseCache",
    "TokenBudget",
    "build_client",
    "provider_from_settings",
]


def provider_from_settings(settings: Settings) -> LLMProvider:
    """The configured provider. Raises ``ConfigError`` if it is not fully configured."""
    settings.require_llm()
    if settings.llm_provider is ProviderName.ANTHROPIC and settings.anthropic_api_key:
        return AnthropicProvider(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
        )
    if settings.llm_provider is ProviderName.GROQ and settings.groq_api_key:
        return GroqProvider(
            api_key=settings.groq_api_key.get_secret_value(),
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
            tokens_per_minute=settings.groq_tokens_per_minute,
        )
    raise ConfigError(f"Unsupported LLM_PROVIDER: {settings.llm_provider.value}")


def build_client(settings: Settings, provider: LLMProvider) -> LLMClient:
    """A client with the run's token budget and the on-disk cache under ``DATA_DIR``.

    With ``LLM_PROVIDER=groq`` every request is also kept under the free tier's
    per-minute token limit (see :mod:`.groq_provider`).
    """
    groq = settings.llm_provider is ProviderName.GROQ
    return LLMClient(
        provider,
        budget=TokenBudget(settings.llm_max_tokens_per_run),
        cache=ResponseCache(settings.data_dir / CACHE_DIR),
        request_token_limit=settings.groq_tokens_per_minute if groq else None,
        max_output_tokens=settings.groq_max_output_tokens if groq else None,
    )
