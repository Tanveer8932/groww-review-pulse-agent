"""Groq Cloud provider (``LLM_PROVIDER=groq``), sized for the free tier.

* Structured outputs: every request carries ``response_format`` ``json_schema`` with the
  prompt's JSON Schema. Models in :data:`STRICT_SCHEMA_MODELS` use ``strict: true``
  (constrained decoding); any other model gets best-effort mode. The client still
  validates every reply either way.
* Free-tier limits: Groq counts a request's prompt **plus** ``max_completion_tokens``
  against the per-minute token limit (TPM). :class:`TokenRateLimiter` paces requests so
  the requested tokens in any 60-second window stay under ``GROQ_TOKENS_PER_MINUTE``
  (waiting, not failing), and a single request that could never fit raises
  :class:`LLMProviderError` without a network call. The client keeps each request under
  the limit (smaller discovery sample / assignment batches) before it gets here.
* Rate limits and transient errors (408/409/429/5xx, timeouts, connection errors) are
  retried by the SDK with exponential backoff, honouring ``retry-after``. Anything still
  failing becomes :class:`LLMProviderError`; messages carry the status and request ID,
  never prompts. HTTP 413 (request over the TPM limit) is not retried.
* The configured ``LLM_MODEL`` is the only model ever called: no fallback models, and no
  ``service_tier`` is sent, so nothing switches to another model or paid capacity.
* ``effort`` is ignored: reasoning models always run at ``reasoning_effort=low`` and the
  reasoning text is not returned (``include_reasoning=False``).
* A truncated reply (``finish_reason == "length"``), a best-effort reply Groq rejects as
  not matching the schema, or an empty reply raises :class:`LLMOutputError`.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import groq
from groq.types.chat import ChatCompletionMessageParam

from groww_pulse.analysis.llm.base import ChatMessage, Effort, ProviderResponse
from groww_pulse.errors import LLMOutputError, LLMProviderError

DEFAULT_MAX_RETRIES = 4
WINDOW_SECONDS = 60.0
#: Same deliberate over-estimate as the client budget (``client.CHARS_PER_TOKEN_ESTIMATE``).
CHARS_PER_TOKEN_ESTIMATE = 3
#: Per-message chat-template overhead, in tokens.
MESSAGE_OVERHEAD_TOKENS = 8

#: Models with strict (constrained-decoding) JSON-schema support on Groq.
STRICT_SCHEMA_MODELS = frozenset({"openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"})
#: Models that accept ``reasoning_effort`` low/medium/high. Reasoning tokens count against
#: ``max_completion_tokens`` and the per-minute limit, so these always run at ``low``:
#: the tasks are extraction/classification whose output code validates anyway.
REASONING_EFFORT_MODELS = frozenset({"openai/gpt-oss-20b", "openai/gpt-oss-120b"})


def _describe(exc: groq.APIError) -> str:
    status = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None)
    parts = [type(exc).__name__]
    if status is not None:
        parts.append(f"HTTP {status}")
    if request_id:
        parts.append(f"request_id={request_id}")
    return ", ".join(parts)


def _error_code(exc: groq.APIStatusError) -> str | None:
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return str(error["code"])
    return None


def estimate_prompt_tokens(system: str, messages: list[ChatMessage]) -> int:
    chars = len(system) + sum(len(m["content"]) for m in messages)
    return math.ceil(chars / CHARS_PER_TOKEN_ESTIMATE) + MESSAGE_OVERHEAD_TOKENS * (
        len(messages) + 1
    )


class TokenRateLimiter:
    """Sliding 60-second window over *requested* tokens (prompt + max completion)."""

    def __init__(
        self,
        tokens_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.tokens_per_minute = tokens_per_minute
        self._clock = clock
        self._sleep = sleep
        self._events: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def _used(self, now: float) -> int:
        while self._events and now - self._events[0][0] >= WINDOW_SECONDS:
            self._events.popleft()
        return sum(tokens for _, tokens in self._events)

    def acquire(self, tokens: int) -> None:
        """Block until ``tokens`` fit in the window, then record them."""
        if tokens > self.tokens_per_minute:
            raise LLMProviderError(
                f"LLM request needs ~{tokens} tokens, over GROQ_TOKENS_PER_MINUTE="
                f"{self.tokens_per_minute}; it cannot be sent on this plan"
            )
        with self._lock:
            while True:
                now = self._clock()
                if self._used(now) + tokens <= self.tokens_per_minute:
                    self._events.append((now, tokens))
                    return
                self._sleep(max(self._events[0][0] + WINDOW_SECONDS - now, 0.0) + 0.5)


class GroqProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        tokens_per_minute: int = 8_000,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: groq.Groq | None = None,
        limiter: TokenRateLimiter | None = None,
    ) -> None:
        self._model = model
        self._client = client or groq.Groq(
            api_key=api_key, timeout=timeout, max_retries=max_retries
        )
        self._limiter = limiter or TokenRateLimiter(tokens_per_minute)

    @property
    def name(self) -> str:
        return "groq"

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
        self._limiter.acquire(estimate_prompt_tokens(system, messages) + max_tokens)
        reasoning = self._model in REASONING_EFFORT_MODELS
        chat: list[ChatCompletionMessageParam] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                chat.append({"role": "user", "content": m["content"]})
            else:
                chat.append({"role": "assistant", "content": m["content"]})
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=chat,
                max_completion_tokens=max_tokens,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "response",
                        "strict": self._model in STRICT_SCHEMA_MODELS,
                        "schema": schema,
                    },
                },
                reasoning_effort="low" if reasoning else groq.omit,
                include_reasoning=False if reasoning else groq.omit,
                stream=False,
            )
        except (groq.AuthenticationError, groq.PermissionDeniedError, groq.NotFoundError) as exc:
            raise LLMProviderError(
                f"LLM request rejected; check GROQ_API_KEY and LLM_MODEL ({_describe(exc)})"
            ) from None
        except groq.RateLimitError as exc:
            raise LLMProviderError(
                f"LLM rate limit persisted after retries ({_describe(exc)})"
            ) from None
        except groq.BadRequestError as exc:
            if _error_code(exc) == "json_validate_failed":
                raise LLMOutputError(
                    f"The model's reply did not match the JSON schema ({_describe(exc)})"
                ) from None
            raise LLMProviderError(f"LLM request failed ({_describe(exc)})") from None
        except groq.APIStatusError as exc:
            if exc.status_code == 413:
                raise LLMProviderError(
                    "LLM request is over the plan's per-minute token limit; lower "
                    f"GROQ_MAX_OUTPUT_TOKENS or GROQ_TOKENS_PER_MINUTE ({_describe(exc)})"
                ) from None
            raise LLMProviderError(f"LLM request failed ({_describe(exc)})") from None
        except groq.APIConnectionError as exc:
            raise LLMProviderError(f"Could not reach the LLM provider ({_describe(exc)})") from None

        if not response.choices:
            raise LLMOutputError("The model returned no choices")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMOutputError(
                f"The model's reply was cut off at max_completion_tokens={max_tokens}; "
                "raise GROQ_MAX_OUTPUT_TOKENS"
            )
        text = choice.message.content or ""
        if not text.strip():
            raise LLMOutputError(f"The model returned an empty reply ({choice.finish_reason})")
        usage = response.usage
        return ProviderResponse(
            text=text,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=response.model,
            stop_reason=choice.finish_reason,
        )
