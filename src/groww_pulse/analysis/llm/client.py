"""Validated, cached, budgeted structured-output calls (architecture §6.4).

:meth:`LLMClient.complete_json` is the only way analysis code talks to an LLM:

1. **Cache.** The key is ``sha256(provider, model, prompt id, system, user, schema)``. A hit
   that still validates costs 0 provider calls, so a re-run on the same input reproduces
   the same analysis (NFR-4).
2. **Budget.** Before every call the client checks that tokens used so far plus an upper
   estimate for this call (input characters / 3 + ``max_tokens``) fit
   ``LLM_MAX_TOKENS_PER_RUN``; otherwise :class:`BudgetExceededError` is raised and no call
   is made. Actual usage is recorded after the call.
3. **Validation.** The reply must be JSON, must validate against the Pydantic output model,
   and must pass the caller's semantic validator (unknown IDs, theme count, word limits...).
4. **One repair retry.** If validation fails, the reply and a description of the problem
   are sent back once. A second failure raises :class:`LLMOutputError`. Only validated
   replies are cached.
5. **Per-request limit** (optional, Groq free tier). With ``request_token_limit`` set,
   ``max_tokens`` is capped at ``max_output_tokens`` and a request whose estimate exceeds
   the limit raises :class:`LLMProviderError` without a call. Callers use :meth:`fits` to
   size requests (discovery sample, assignment batches) so that they, plus
   :data:`REPAIR_RESERVE_TOKENS` for a repair turn, stay under it. Without a limit (the
   Anthropic default) requests are sent exactly as built.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from groww_pulse.analysis.llm.base import ChatMessage, LLMProvider, PromptRequest
from groww_pulse.errors import BudgetExceededError, LLMOutputError, LLMProviderError
from groww_pulse.storage.jsonl_store import atomic_write_lines

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

#: Rough upper bound for tokens per character of prompt text (English averages ~4 chars
#: per token; 3 over-estimates on purpose so the budget check errs on the safe side).
CHARS_PER_TOKEN_ESTIMATE = 3
#: Head-room :meth:`LLMClient.fits` keeps under the per-request limit for a repair turn
#: (the rejected reply plus the validator's note are added to the conversation).
REPAIR_RESERVE_TOKENS = 1_000


class OutputRejectedError(ValueError):
    """A semantic validator rejected an LLM reply; the message is sent back for repair.

    Messages must describe the problem with IDs and counts only, never review text.
    """


@dataclass
class TokenBudget:
    limit: int
    used: int = 0

    def check(self, estimated: int, prompt_id: str) -> None:
        if self.used + estimated > self.limit:
            raise BudgetExceededError(
                f"LLM token budget exceeded before calling {prompt_id}: {self.used} used + "
                f"~{estimated} estimated > LLM_MAX_TOKENS_PER_RUN={self.limit}"
            )

    def record(self, tokens: int) -> None:
        self.used += tokens


class ResponseCache:
    """One JSON file per validated reply under ``cache_dir`` (``data/cache/llm``)."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def get(self, key: str) -> str | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            text = entry["text"]
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("Ignoring unreadable LLM cache entry", extra={"cache_key": key})
            return None
        return text if isinstance(text, str) else None

    def put(self, key: str, text: str, meta: dict[str, Any]) -> None:
        entry = {"key": key, **meta, "text": text}
        atomic_write_lines(self._path(key), [json.dumps(entry, ensure_ascii=False)])


@dataclass
class LLMUsage:
    provider_calls: int = 0
    cache_hits: int = 0
    repair_retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    served_models: set[str] = field(default_factory=set)
    prompts: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_calls": self.provider_calls,
            "cache_hits": self.cache_hits,
            "repair_retries": self.repair_retries,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "served_models": sorted(self.served_models),
            "prompts": sorted(self.prompts),
        }


def cache_key(provider: str, model: str, request: PromptRequest) -> str:
    material = json.dumps(
        {
            "provider": provider,
            "model": model,
            "prompt": request.prompt_id,
            "system": request.system,
            "user": request.user,
            "schema": request.schema,
            "effort": request.effort,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def describe_invalid(exc: ValueError) -> str:
    """A repair hint that names fields and problems, never the offending values."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'root'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)[:10]
        )
    if isinstance(exc, json.JSONDecodeError):
        return f"not valid JSON ({exc.msg} at position {exc.pos})"
    return str(exc)


class LLMClient:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        budget: TokenBudget,
        cache: ResponseCache | None = None,
        request_token_limit: int | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        self.provider = provider
        self.budget = budget
        self.cache = cache
        self.request_token_limit = request_token_limit
        self.max_output_tokens = max_output_tokens
        self.usage = LLMUsage()

    def _max_tokens(self, request: PromptRequest) -> int:
        if self.max_output_tokens is None:
            return request.max_tokens
        return min(request.max_tokens, self.max_output_tokens)

    def _estimate(self, request: PromptRequest, messages: list[ChatMessage]) -> int:
        chars = len(request.system) + sum(len(m["content"]) for m in messages)
        return math.ceil(chars / CHARS_PER_TOKEN_ESTIMATE) + self._max_tokens(request)

    def fits(self, request: PromptRequest) -> bool:
        """Whether ``request`` (plus repair head-room) fits the per-request token limit."""
        if self.request_token_limit is None:
            return True
        estimate = self._estimate(request, [{"role": "user", "content": request.user}])
        return estimate + REPAIR_RESERVE_TOKENS <= self.request_token_limit

    def complete_json(
        self,
        request: PromptRequest,
        output_type: type[T],
        validate: Callable[[T], None] | None = None,
    ) -> T:
        """Return a validated ``output_type`` for ``request`` (see module docstring)."""
        self.usage.prompts.add(request.prompt_id)
        key = cache_key(self.provider.name, self.provider.model, request)
        if self.cache is not None and (cached := self.cache.get(key)) is not None:
            try:
                result = self._parse(cached, output_type, validate)
            except ValueError:
                log.warning("Cached LLM reply no longer validates; calling the provider")
            else:
                self.usage.cache_hits += 1
                return result

        messages: list[ChatMessage] = [{"role": "user", "content": request.user}]
        text = self._call(request, messages)
        try:
            result = self._parse(text, output_type, validate)
        except ValueError as exc:
            problem = describe_invalid(exc)
            log.warning(
                "LLM reply failed validation; sending one repair request",
                extra={"prompt": request.prompt_id, "problem": problem},
            )
            self.usage.repair_retries += 1
            messages += [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        "Your previous reply was rejected by the validator: "
                        f"{problem}\nReply again with corrected JSON that follows every rule "
                        "in the original instructions and matches the schema exactly."
                    ),
                },
            ]
            text = self._call(request, messages)
            try:
                result = self._parse(text, output_type, validate)
            except ValueError as exc2:
                raise LLMOutputError(
                    f"{request.prompt_id}: LLM output invalid after one repair retry: "
                    f"{describe_invalid(exc2)}"
                ) from None

        if self.cache is not None:
            self.cache.put(
                key,
                text,
                {
                    "provider": self.provider.name,
                    "model": self.provider.model,
                    "prompt": request.prompt_id,
                },
            )
        return result

    def _call(self, request: PromptRequest, messages: list[ChatMessage]) -> str:
        estimate = self._estimate(request, messages)
        if self.request_token_limit is not None and estimate > self.request_token_limit:
            raise LLMProviderError(
                f"{request.prompt_id}: request needs ~{estimate} tokens, over the provider's "
                f"per-request limit of {self.request_token_limit}"
            )
        self.budget.check(estimate, request.prompt_id)
        response = self.provider.generate(
            system=request.system,
            messages=messages,
            schema=request.schema,
            max_tokens=self._max_tokens(request),
            effort=request.effort,
        )
        self.usage.provider_calls += 1
        self.usage.input_tokens += response.input_tokens
        self.usage.output_tokens += response.output_tokens
        self.usage.served_models.add(response.model)
        self.budget.record(response.total_tokens)
        log.info(
            "LLM call finished",
            extra={
                "prompt": request.prompt_id,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "stop_reason": response.stop_reason,
                "budget_used": self.budget.used,
            },
        )
        return response.text

    @staticmethod
    def _parse(text: str, output_type: type[T], validate: Callable[[T], None] | None) -> T:
        result = output_type.model_validate(json.loads(text))
        if validate is not None:
            validate(result)
        return result
