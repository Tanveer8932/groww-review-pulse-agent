# SYNTHETIC TEST FIXTURE — not real review data
"""Groq provider: request shape, error mapping, free-tier pacing; client per-request limits;
settings wiring; limit-aware theme sampling and batching. No network."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import groq
import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from groww_pulse.analysis import themes
from groww_pulse.analysis.llm import (
    AnthropicProvider,
    GroqProvider,
    LLMClient,
    PromptRequest,
    ProviderResponse,
    TokenBudget,
    build_client,
    provider_from_settings,
)
from groww_pulse.analysis.llm.client import REPAIR_RESERVE_TOKENS
from groww_pulse.analysis.llm.groq_provider import TokenRateLimiter, estimate_prompt_tokens
from groww_pulse.config import DEFAULT_GROQ_MODEL, LLMProvider, load_settings
from groww_pulse.errors import ConfigError, LLMOutputError, LLMProviderError
from groww_pulse.models import Review
from tests.fixtures.analysis import make_review, scripted_provider

SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


# ---------------------------------------------------------------- fakes
def _completion(**overrides: Any) -> Any:
    data: dict[str, Any] = {
        "choices": [
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"value": 1}'))
        ],
        "model": DEFAULT_GROQ_MODEL,
        "usage": SimpleNamespace(prompt_tokens=11, completion_tokens=4),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


class FakeCompletions:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def provider_with(
    outcome: Any, model: str = DEFAULT_GROQ_MODEL, limiter: TokenRateLimiter | None = None
) -> tuple[GroqProvider, FakeCompletions]:
    completions = FakeCompletions(outcome)
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = GroqProvider(
        api_key="unused", model=model, client=cast(groq.Groq, fake), limiter=limiter
    )
    return provider, completions


def _generate(provider: GroqProvider, max_tokens: int = 64) -> ProviderResponse:
    return provider.generate(
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        schema=SCHEMA,
        max_tokens=max_tokens,
        effort="medium",
    )


# ---------------------------------------------------------------- request / response
def test_groq_request_shape_and_response_parsing() -> None:
    provider, completions = provider_with(_completion())
    response = _generate(provider)
    assert response.text == '{"value": 1}'
    assert (response.input_tokens, response.output_tokens) == (11, 4)
    assert (response.model, response.stop_reason) == (DEFAULT_GROQ_MODEL, "stop")
    sent = completions.kwargs
    assert sent["model"] == DEFAULT_GROQ_MODEL
    assert sent["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    assert sent["max_completion_tokens"] == 64
    assert sent["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "strict": True, "schema": SCHEMA},
    }
    assert sent["reasoning_effort"] == "low"
    assert sent["include_reasoning"] is False
    assert sent["stream"] is False
    # Never switches capacity or model, and the LLM never gets tool access.
    for absent in ("service_tier", "tools", "fallbacks"):
        assert absent not in sent


def test_non_strict_model_uses_best_effort_without_reasoning_effort() -> None:
    provider, completions = provider_with(_completion(), model="some/other-model")
    _generate(provider)
    assert completions.kwargs["response_format"]["json_schema"]["strict"] is False
    assert completions.kwargs["reasoning_effort"] is groq.omit


@pytest.mark.parametrize(
    ("completion", "match"),
    [
        (_completion(choices=[]), "no choices"),
        (
            _completion(
                choices=[
                    SimpleNamespace(
                        finish_reason="length", message=SimpleNamespace(content='{"val')
                    )
                ]
            ),
            "GROQ_MAX_OUTPUT_TOKENS",
        ),
        (
            _completion(
                choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=""))]
            ),
            "empty reply",
        ),
    ],
)
def test_unusable_replies_are_output_errors(completion: Any, match: str) -> None:
    provider, _ = provider_with(completion)
    with pytest.raises(LLMOutputError, match=match):
        _generate(provider)


_REQUEST = httpx.Request("POST", "https://api.groq.invalid/openai/v1/chat/completions")


def _status_error(
    cls: type[groq.APIStatusError], code: int, body: object = None
) -> groq.APIStatusError:
    response = httpx.Response(code, request=_REQUEST, headers={"x-request-id": "req_test"})
    return cls("synthetic failure", response=response, body=body)


@pytest.mark.parametrize(
    ("exc", "match"),
    [
        (_status_error(groq.AuthenticationError, 401), "check GROQ_API_KEY"),
        (_status_error(groq.PermissionDeniedError, 403), "check GROQ_API_KEY"),
        (_status_error(groq.NotFoundError, 404), "check GROQ_API_KEY and LLM_MODEL"),
        (_status_error(groq.RateLimitError, 429), "rate limit persisted"),
        (_status_error(groq.APIStatusError, 413), "per-minute token limit"),
        (_status_error(groq.BadRequestError, 400, {"error": {"code": "x"}}), "HTTP 400"),
        (_status_error(groq.InternalServerError, 500), "HTTP 500"),
        (groq.APITimeoutError(request=_REQUEST), "Could not reach"),
        (groq.APIConnectionError(request=_REQUEST), "Could not reach"),
    ],
)
def test_groq_errors_map_to_provider_error(exc: Exception, match: str) -> None:
    provider, _ = provider_with(exc)
    with pytest.raises(LLMProviderError, match=match) as info:
        _generate(provider)
    assert "sys" not in str(info.value)  # prompts never leak into errors


def test_schema_mismatch_is_an_output_error() -> None:
    body = {"error": {"code": "json_validate_failed", "failed_generation": "SECRET"}}
    provider, _ = provider_with(_status_error(groq.BadRequestError, 400, body))
    with pytest.raises(LLMOutputError, match="did not match the JSON schema") as info:
        _generate(provider)
    assert "SECRET" not in str(info.value)


def test_sdk_client_gets_timeout_and_retries() -> None:
    provider = GroqProvider(api_key="gsk_fake_key_for_tests", model="m", timeout=7.0)
    client = cast(Any, provider)._client
    assert client.timeout == 7.0
    assert client.max_retries == 4


# ---------------------------------------------------------------- rate limiter
def test_limiter_waits_for_the_window_instead_of_failing() -> None:
    clock = FakeClock()
    limiter = TokenRateLimiter(1000, clock=clock, sleep=clock.sleep)
    limiter.acquire(600)
    limiter.acquire(300)
    assert clock.slept == []
    limiter.acquire(500)  # 600 + 300 + 500 > 1000: wait until the first entry expires
    assert clock.slept
    assert clock.now >= 1060.0


def test_limiter_rejects_a_request_that_can_never_fit() -> None:
    limiter = TokenRateLimiter(1000)
    with pytest.raises(LLMProviderError, match="GROQ_TOKENS_PER_MINUTE=1000"):
        limiter.acquire(1001)


def test_provider_paces_on_requested_tokens() -> None:
    clock = FakeClock()
    limiter = TokenRateLimiter(2000, clock=clock, sleep=clock.sleep)
    provider, _ = provider_with(_completion(), limiter=limiter)
    _generate(provider, max_tokens=1500)
    assert clock.slept == []
    _generate(provider, max_tokens=1500)  # second request must wait for the window
    assert clock.slept


def test_oversized_request_is_rejected_before_any_network_call() -> None:
    provider, completions = provider_with(_completion(), limiter=TokenRateLimiter(100))
    with pytest.raises(LLMProviderError):
        _generate(provider, max_tokens=200)
    assert completions.kwargs == {}


def test_prompt_estimate_is_conservative() -> None:
    assert estimate_prompt_tokens("a" * 30, [{"role": "user", "content": "b" * 30}]) >= 20


# ---------------------------------------------------------------- client limits
def _request(user: str = "SYNTHETIC FIXTURE question", max_tokens: int = 16_000) -> PromptRequest:
    return PromptRequest(
        name="p", version="v1", system="s", user=user, schema=SCHEMA, max_tokens=max_tokens
    )


def test_client_caps_output_tokens_and_checks_request_limit() -> None:
    provider = scripted_provider(['{"value": 2}'])
    client = LLMClient(
        provider, budget=TokenBudget(100_000), request_token_limit=8_000, max_output_tokens=2_500
    )
    assert client.complete_json(_request(), Answer).value == 2
    assert cast(Any, provider).calls[0].max_tokens == 2_500

    huge = _request(user="x" * 30_000)
    assert not client.fits(huge)
    with pytest.raises(LLMProviderError, match="per-request limit of 8000"):
        client.complete_json(huge, Answer)
    assert len(cast(Any, provider).calls) == 1


def test_fits_keeps_repair_headroom() -> None:
    # estimate = ceil(len(system + user) / 3) + max_tokens = 100 + max_tokens
    limit = 100 + 50 + REPAIR_RESERVE_TOKENS
    client = LLMClient(scripted_provider([]), budget=TokenBudget(1), request_token_limit=limit)
    assert client.fits(_request(user="u" * 299, max_tokens=50))
    assert not client.fits(_request(user="u" * 299, max_tokens=51))


def test_client_without_limits_sends_requests_unchanged() -> None:
    provider = scripted_provider(['{"value": 3}'])
    client = LLMClient(provider, budget=TokenBudget(100_000))
    assert client.fits(_request(user="x" * 1_000_000, max_tokens=1))
    client.complete_json(_request(), Answer)
    assert cast(Any, provider).calls[0].max_tokens == 16_000


# ---------------------------------------------------------------- settings
def test_groq_settings_default_model_and_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    settings = load_settings(env_file=None, data_dir=tmp_path)
    assert settings.llm_provider is LLMProvider.GROQ
    assert settings.llm_model == DEFAULT_GROQ_MODEL
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        provider_from_settings(settings)

    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake_key_for_tests")
    settings = load_settings(env_file=None, data_dir=tmp_path, groq_tokens_per_minute=6_000)
    provider = provider_from_settings(settings)
    assert isinstance(provider, GroqProvider)
    assert (provider.name, provider.model) == ("groq", DEFAULT_GROQ_MODEL)
    client = build_client(settings, provider)
    assert (client.request_token_limit, client.max_output_tokens) == (6_000, 2_500)
    assert "gsk_fake_key_for_tests" in settings.secret_values()
    assert settings.public_summary()["GROQ_API_KEY"] == "set"  # pragma: allowlist secret
    assert "gsk_fake" not in json.dumps(settings.public_summary())


def test_groq_env_file_is_loaded(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "LLM_PROVIDER=groq\nLLM_MODEL=openai/gpt-oss-20b\n"  # pragma: allowlist secret
        "GROQ_API_KEY=gsk_fake_key_for_tests\n",  # pragma: allowlist secret
        encoding="utf-8",
    )
    settings = load_settings(env_file=env)
    assert settings.llm_model == "openai/gpt-oss-20b"
    assert settings.groq_api_key is not None


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"llm_model": "claude-sonnet-5-5"}, "Claude model"),
        ({"groq_max_output_tokens": 8_000}, "GROQ_MAX_OUTPUT_TOKENS"),
    ],
)
def test_invalid_groq_settings(
    overrides: dict[str, Any], match: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    with pytest.raises(ConfigError, match=match):
        load_settings(env_file=None, **overrides)


def test_anthropic_remains_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-key-for-tests")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake_key_for_tests")
    settings = load_settings(env_file=None, data_dir=tmp_path)
    assert settings.llm_model == "claude-sonnet-5-5"
    provider = provider_from_settings(settings)
    assert isinstance(provider, AnthropicProvider)
    client = build_client(settings, provider)
    assert (client.request_token_limit, client.max_output_tokens) == (None, None)


# ---------------------------------------------------------------- limit-aware theme sizing
class RecordingProvider:
    """Answers discovery and assignment prompts minimally; records request sizes."""

    name = "fake"
    model = "fake-model"

    def __init__(self) -> None:
        self.sizes: list[tuple[str, int, int]] = []  # (kind, n_ids, estimate)

    def generate(self, **kwargs: Any) -> ProviderResponse:
        schema, messages = kwargs["schema"], kwargs["messages"]
        chars = len(kwargs["system"]) + sum(len(m["content"]) for m in messages)
        estimate = -(-chars // 3) + kwargs["max_tokens"]
        if "themes" in schema["properties"]:
            ids = [p.split('"')[0] for p in messages[0]["content"].split('"id": "')[1:]]
            self.sizes.append(("discovery", len(ids), estimate))
            out: dict[str, Any] = {
                "themes": [
                    {
                        "name": f"Theme {letter}",
                        "description": "SYNTHETIC FIXTURE theme",
                        "evidence_review_ids": ids[i * 2 : i * 2 + 2],
                    }
                    for i, letter in enumerate("abc")
                ]
            }
        else:
            ids = schema["properties"]["assignments"]["items"]["properties"]["review_id"]["enum"]
            self.sizes.append(("assignment", len(ids), estimate))
            out = {"assignments": [{"review_id": i, "theme_id": "T1"} for i in ids]}
        return ProviderResponse(json.dumps(out), 1, 1, "fake-model")


def _reviews(n: int) -> list[Review]:
    text = "SYNTHETIC FIXTURE the app keeps crashing when I open my portfolio page " * 5
    return [
        make_review(f"00000000-0000-4000-8000-{i:012d}", f"{text}{i}", rating=1 + i % 5, day=i)
        for i in range(n)
    ]


def test_theme_requests_shrink_to_fit_the_limit() -> None:
    reviews = _reviews(67)
    provider = RecordingProvider()
    client = LLMClient(
        provider, budget=TokenBudget(10**7), request_token_limit=8_000, max_output_tokens=2_500
    )
    definitions, n_sample = themes.discover_themes(client, reviews)
    assignments, defaulted = themes.assign_themes(client, reviews, definitions)

    assert themes.MIN_SAMPLE <= n_sample < len(reviews)
    assert len(assignments) == 67
    assert defaulted == 0
    assigned = sum(n for kind, n, _ in provider.sizes if kind == "assignment")
    assert assigned == 67
    assert all(est + REPAIR_RESERVE_TOKENS <= 8_000 for _, _, est in provider.sizes)


def test_theme_requests_unchanged_without_a_limit() -> None:
    reviews = _reviews(67)
    provider = RecordingProvider()
    client = LLMClient(provider, budget=TokenBudget(10**7))
    _, n_sample = themes.discover_themes(client, reviews)
    definitions, _ = themes.discover_themes(client, reviews)
    themes.assign_themes(client, reviews, definitions)
    assert n_sample == 67
    assert [n for kind, n, _ in provider.sizes if kind == "assignment"] == [50, 17]
