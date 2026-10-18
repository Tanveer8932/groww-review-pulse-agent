"""Anthropic Claude provider (the default, ``LLM_PROVIDER=anthropic``).

* Structured outputs: every request carries ``output_config.format`` with the prompt's JSON
  Schema, so the reply is schema-shaped JSON text. The client still validates it.
* Rate limits and transient errors (408/409/429/5xx, connection errors) are retried by the
  SDK with exponential backoff (``max_retries``). Anything still failing becomes
  :class:`LLMProviderError`; error messages carry the status and request ID, never prompts.
* ``temperature`` is not sent: current Claude models reject non-default sampling
  parameters. Reproducibility (NFR-4) comes from the response cache instead.
* Server-side refusal fallback (``fallbacks="default"``) lets the API retry a declined
  request on a fallback model inside the same call. A request the whole chain declines
  raises :class:`LLMOutputError`.
"""

from __future__ import annotations

from typing import Any

import anthropic

from groww_pulse.analysis.llm.base import ChatMessage, Effort, ProviderResponse
from groww_pulse.errors import LLMOutputError, LLMProviderError

FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_MAX_RETRIES = 4


def _describe(exc: anthropic.APIError) -> str:
    status = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None)
    parts = [type(exc).__name__]
    if status is not None:
        parts.append(f"HTTP {status}")
    if request_id:
        parts.append(f"request_id={request_id}")
    return ", ".join(parts)


class AnthropicProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self._model = model
        self._client = client or anthropic.Anthropic(
            api_key=api_key, timeout=timeout, max_retries=max_retries
        )

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def model(self) -> str:
        return self._model

    def generate(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        schema: dict[str, Any],
        max_tokens: int,
        effort: Effort,
    ) -> ProviderResponse:
        try:
            response = self._client.beta.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": m["role"], "content": m["content"]} for m in messages],
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
        ) as exc:
            raise LLMProviderError(
                f"LLM request rejected; check ANTHROPIC_API_KEY and LLM_MODEL ({_describe(exc)})"
            ) from None
        except anthropic.RateLimitError as exc:
            raise LLMProviderError(
                f"LLM rate limit persisted after retries ({_describe(exc)})"
            ) from None
        except anthropic.APIStatusError as exc:
            raise LLMProviderError(f"LLM request failed ({_describe(exc)})") from None
        except anthropic.APIConnectionError as exc:
            raise LLMProviderError(f"Could not reach the LLM provider ({_describe(exc)})") from None

        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise LLMOutputError(f"The model declined the request (refusal, category={category})")
        text = "".join(block.text for block in response.content if block.type == "text")
        usage = response.usage
        input_tokens = (
            usage.input_tokens
            + (usage.cache_creation_input_tokens or 0)
            + (usage.cache_read_input_tokens or 0)
        )
        return ProviderResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=usage.output_tokens,
            model=response.model,
            stop_reason=response.stop_reason,
        )
