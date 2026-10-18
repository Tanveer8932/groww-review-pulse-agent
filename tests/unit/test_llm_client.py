# SYNTHETIC TEST FIXTURE — not real review data
"""LLM client: cache, budget, validation, repair retry; Anthropic provider mapping."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import httpx2
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from groww_pulse.analysis.llm import (
    AnthropicProvider,
    LLMClient,
    OutputRejectedError,
    PromptRequest,
    ResponseCache,
    TokenBudget,
    build_client,
    provider_from_settings,
)
from groww_pulse.analysis.llm.anthropic_provider import FALLBACK_BETA
from groww_pulse.analysis.llm.client import cache_key, describe_invalid
from groww_pulse.config import load_settings
from groww_pulse.errors import BudgetExceededError, ConfigError, LLMOutputError, LLMProviderError
from tests.fixtures.analysis import scripted_provider


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}


def request(user: str = "SYNTHETIC FIXTURE question", max_tokens: int = 100) -> PromptRequest:
    return PromptRequest(
        name="test_prompt",
        version="v1",
        system="system",
        user=user,
        schema=SCHEMA,
        max_tokens=max_tokens,
        effort="low",
    )


def client_for(replies: list[str], tmp_path: Path, limit: int = 100_000) -> LLMClient:
    return LLMClient(
        scripted_provider(replies),
        budget=TokenBudget(limit),
        cache=ResponseCache(tmp_path / "cache"),
    )


def test_valid_reply_is_returned_and_cached(tmp_path: Path) -> None:
    client = client_for(['{"value": 7}'], tmp_path)
    assert client.complete_json(request(), Answer).value == 7
    assert client.usage.provider_calls == 1
    assert len(list((tmp_path / "cache").glob("*.json"))) == 1

    # Identical input on a fresh client: served from disk, 0 provider calls.
    again = client_for([], tmp_path)
    assert again.complete_json(request(), Answer).value == 7
    assert again.usage.provider_calls == 0
    assert again.usage.cache_hits == 1


def test_different_prompt_misses_the_cache(tmp_path: Path) -> None:
    client_for(['{"value": 1}'], tmp_path).complete_json(request("a"), Answer)
    other = client_for(['{"value": 2}'], tmp_path)
    assert other.complete_json(request("b"), Answer).value == 2
    assert other.usage.provider_calls == 1


def test_malformed_json_triggers_one_repair_then_succeeds(tmp_path: Path) -> None:
    client = client_for(["not json", '{"value": 3}'], tmp_path)
    assert client.complete_json(request(), Answer).value == 3
    assert client.usage.repair_retries == 1
    calls = cast(Any, client.provider).calls
    assert len(calls) == 2
    repair = calls[1].messages
    assert [m["role"] for m in repair] == ["user", "assistant", "user"]
    assert "not valid JSON" in repair[-1]["content"]


def test_malformed_twice_raises_llm_output_error(tmp_path: Path) -> None:
    client = client_for(["not json", '{"value": "x"}'], tmp_path)
    with pytest.raises(LLMOutputError, match="after one repair retry"):
        client.complete_json(request(), Answer)
    assert not (tmp_path / "cache").exists()  # invalid output is never cached


def test_semantic_validator_drives_repair(tmp_path: Path) -> None:
    def must_be_even(answer: Answer) -> None:
        if answer.value % 2:
            raise OutputRejectedError("value must be even")

    client = client_for(['{"value": 3}', '{"value": 4}'], tmp_path)
    assert client.complete_json(request(), Answer, must_be_even).value == 4
    assert "value must be even" in cast(Any, client.provider).calls[1].last_user

    client = client_for(['{"value": 3}', '{"value": 5}'], tmp_path / "b")
    with pytest.raises(LLMOutputError, match="value must be even"):
        client.complete_json(request(), Answer, must_be_even)


def test_schema_errors_never_echo_values() -> None:
    with pytest.raises(ValidationError) as info:
        Answer.model_validate({"value": "SECRET-LOOKING TEXT"})
    assert "SECRET-LOOKING" not in describe_invalid(info.value)


def test_budget_is_checked_before_the_call(tmp_path: Path) -> None:
    client = client_for(['{"value": 1}'], tmp_path, limit=50)
    with pytest.raises(BudgetExceededError, match="LLM_MAX_TOKENS_PER_RUN=50"):
        client.complete_json(request(max_tokens=100), Answer)
    assert cast(Any, client.provider).calls == []


def test_budget_accumulates_actual_usage(tmp_path: Path) -> None:
    # Each fake call reports 150 tokens; the estimate for the second call no longer fits.
    client = client_for(['{"value": 1}', '{"value": 2}'], tmp_path, limit=300)
    client.complete_json(request("first", max_tokens=10), Answer)
    assert client.budget.used == 150
    with pytest.raises(BudgetExceededError):
        client.complete_json(request("second " * 50, max_tokens=100), Answer)


def test_corrupt_or_stale_cache_entries_are_ignored(tmp_path: Path) -> None:
    key = cache_key("fake", "fake-model", request())
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / f"{key}.json").write_text("{broken", encoding="utf-8")
    client = client_for(['{"value": 5}'], tmp_path)
    assert client.complete_json(request(), Answer).value == 5

    (cache_dir / f"{key}.json").write_text(json.dumps({"text": '{"value": "bad"}'}), "utf-8")
    client = client_for(['{"value": 6}'], tmp_path)
    assert client.complete_json(request(), Answer).value == 6
    assert client.usage.provider_calls == 1

    (cache_dir / f"{key}.json").write_text(json.dumps({"text": 12}), "utf-8")
    assert ResponseCache(cache_dir).get(key) is None


def test_client_without_cache(tmp_path: Path) -> None:
    client = LLMClient(scripted_provider(['{"value": 1}']), budget=TokenBudget(1000))
    assert client.complete_json(request(), Answer).value == 1
    assert client.usage.as_dict()["prompts"] == ["test_prompt.v1"]


# ---------------------------------------------------------------- settings wiring
def test_provider_requires_api_key(tmp_path: Path) -> None:
    settings = load_settings(env_file=None, data_dir=tmp_path)
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        provider_from_settings(settings)


def test_provider_and_client_from_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-key-for-tests")
    settings = load_settings(env_file=None, data_dir=tmp_path, llm_max_tokens_per_run=1234)
    provider = provider_from_settings(settings)
    assert (provider.name, provider.model) == ("anthropic", settings.llm_model)
    client = build_client(settings, provider)
    assert client.budget.limit == 1234
    assert client.cache is not None
    assert client.cache.cache_dir == tmp_path / "cache" / "llm"


# ---------------------------------------------------------------- Anthropic provider
def _message(**overrides: Any) -> Any:
    data: dict[str, Any] = {
        "content": [SimpleNamespace(type="text", text='{"value": 1}')],
        "stop_reason": "end_turn",
        "stop_details": None,
        "model": "claude-test",
        "usage": SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=2,
        ),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


class FakeMessages:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def provider_with(outcome: Any) -> tuple[AnthropicProvider, FakeMessages]:
    messages = FakeMessages(outcome)
    fake = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    provider = AnthropicProvider(
        api_key="unused", model="claude-test", client=cast(anthropic.Anthropic, fake)
    )
    return provider, messages


def _generate(provider: AnthropicProvider) -> Any:
    return provider.generate(
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
        max_tokens=64,
        effort="low",
    )


def test_anthropic_request_shape_and_response_parsing() -> None:
    provider, messages = provider_with(_message())
    response = _generate(provider)
    assert response.text == '{"value": 1}'
    assert (response.input_tokens, response.output_tokens) == (12, 5)
    assert response.model == "claude-test"
    sent = messages.kwargs
    assert sent["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    assert sent["betas"] == [FALLBACK_BETA]
    assert sent["fallbacks"] == "default"
    assert "temperature" not in sent
    assert "tools" not in sent  # the LLM never gets tool access


def test_anthropic_refusal_is_an_output_error() -> None:
    provider, _ = provider_with(
        _message(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))
    )
    with pytest.raises(LLMOutputError, match="declined"):
        _generate(provider)


_REQUEST = httpx2.Request("POST", "https://api.anthropic.invalid/v1/messages")


def _status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    response = httpx2.Response(code, request=_REQUEST, headers={"request-id": "req_test"})
    return cls("synthetic failure", response=response, body=None)


@pytest.mark.parametrize(
    ("exc", "match"),
    [
        (_status_error(anthropic.AuthenticationError, 401), "check ANTHROPIC_API_KEY"),
        (_status_error(anthropic.NotFoundError, 404), "check ANTHROPIC_API_KEY and LLM_MODEL"),
        (_status_error(anthropic.RateLimitError, 429), "rate limit persisted"),
        (_status_error(anthropic.InternalServerError, 500), "HTTP 500"),
        (anthropic.APIConnectionError(request=_REQUEST), "Could not reach"),
    ],
)
def test_anthropic_errors_map_to_provider_error(exc: Exception, match: str) -> None:
    provider, _ = provider_with(exc)
    with pytest.raises(LLMProviderError, match=match):
        _generate(provider)
