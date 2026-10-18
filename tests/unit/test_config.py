from __future__ import annotations

from pathlib import Path

import pytest

from groww_pulse.config import DeliveryMode, EmojiPolicy, LLMProvider, load_settings
from groww_pulse.errors import ConfigError

VALID_DOC_ID = "a" * 44  # shape of a Google Doc ID; not a real document


def test_defaults_apply_with_empty_environment() -> None:
    s = load_settings(env_file=None)
    assert s.app_id == "com.nextbillion.groww"
    assert s.analysis_window_weeks == 12
    assert s.enable_play_collector is False
    assert s.min_words_exclusive == 8
    assert s.emoji_policy is EmojiPolicy.STRICT
    assert s.lang_min_confidence == pytest.approx(0.80)
    assert s.llm_provider is LLMProvider.ANTHROPIC
    assert s.delivery_mode is DeliveryMode.DRY_RUN
    assert s.data_dir == Path("./data")
    assert s.anthropic_api_key is None
    assert s.mcp_server_url is None
    assert s.google_doc_id is None
    assert s.pulse_recipients == []


@pytest.mark.parametrize("weeks", ["8", "10", "12"])
def test_window_within_range_accepted(monkeypatch: pytest.MonkeyPatch, weeks: str) -> None:
    monkeypatch.setenv("ANALYSIS_WINDOW_WEEKS", weeks)
    assert load_settings(env_file=None).analysis_window_weeks == int(weeks)


@pytest.mark.parametrize("weeks", ["0", "7", "13", "52", "-1", "twelve"])
def test_window_outside_range_rejected(monkeypatch: pytest.MonkeyPatch, weeks: str) -> None:
    monkeypatch.setenv("ANALYSIS_WINDOW_WEEKS", weeks)
    with pytest.raises(ConfigError, match="ANALYSIS_WINDOW_WEEKS"):
        load_settings(env_file=None)


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("EMOJI_POLICY", "strip"),
        ("DELIVERY_MODE", "send"),
        ("LANG_MIN_CONFIDENCE", "1.5"),
        ("LLM_PROVIDER", "unknown"),
        ("LOG_LEVEL", "VERBOSE"),
        ("APP_ID", "not a package"),
        ("MCP_SERVER_URL", "http://example.com/"),
        ("MCP_SERVER_URL", "not-a-url"),
        ("GOOGLE_DOC_ID", "https://docs.google.com/document/d/" + "a" * 44 + "/edit"),
        ("GOOGLE_DOC_ID", "short"),
        ("PULSE_EMAIL_TO", "ops@example.com,not-an-email"),
    ],
)
def test_invalid_values_raise_config_error(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(ConfigError, match=var):
        load_settings(env_file=None)


def test_config_error_never_echoes_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_doc = "SUPERSECRETDOCIDVALUE1234567890"  # pragma: allowlist secret
    secret_like = f"https://docs.google.com/document/d/{fake_doc}/edit"
    monkeypatch.setenv("GOOGLE_DOC_ID", secret_like)
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)
    assert "SUPERSECRETDOCIDVALUE" not in str(info.value)
    assert info.value.__cause__ is None


def test_http_allowed_only_for_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_SERVER_URL", "http://localhost:3000/")
    assert str(load_settings(env_file=None).mcp_server_url) == "http://localhost:3000/"


def test_empty_values_are_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "MCP_SERVER_URL", "GOOGLE_DOC_ID", "PULSE_EMAIL_TO"):
        monkeypatch.setenv(var, "")
    s = load_settings(env_file=None)
    assert s.anthropic_api_key is None
    assert s.mcp_server_url is None


def test_recipients_are_parsed_and_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_EMAIL_TO", " pm@example.com , lead@example.org ,")
    assert load_settings(env_file=None).pulse_recipients == ["pm@example.com", "lead@example.org"]


def test_log_level_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert load_settings(env_file=None).log_level == "DEBUG"


def test_reads_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ANALYSIS_WINDOW_WEEKS=9\nEMOJI_POLICY=lenient\n", encoding="utf-8")
    s = load_settings(env_file=env_file)
    assert s.analysis_window_weeks == 9
    assert s.emoji_policy is EmojiPolicy.LENIENT


# ------------------------------------------------------------------ required-var checks
def test_require_llm_reports_missing_api_key() -> None:
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_settings(env_file=None).require_llm()


def test_require_llm_passes_when_key_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    load_settings(env_file=None).require_llm()


def test_require_mcp_reports_missing_url() -> None:
    with pytest.raises(ConfigError, match="MCP_SERVER_URL"):
        load_settings(env_file=None).require_mcp()


def test_require_delivery_lists_every_missing_var() -> None:
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None).require_delivery()
    for var in ("MCP_SERVER_URL", "GOOGLE_DOC_ID", "PULSE_EMAIL_TO"):
        assert var in str(info.value)


def test_require_delivery_passes_when_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.com/")
    monkeypatch.setenv("GOOGLE_DOC_ID", VALID_DOC_ID)
    monkeypatch.setenv("PULSE_EMAIL_TO", "pm@example.com")
    load_settings(env_file=None).require_delivery()


# ------------------------------------------------------------------ secret handling
def test_public_summary_hides_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-0000000000000000")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "token-value-123456")
    monkeypatch.setenv("GOOGLE_DOC_ID", VALID_DOC_ID)
    monkeypatch.setenv("PULSE_EMAIL_TO", "pm@example.com")
    s = load_settings(env_file=None)
    summary = s.public_summary()
    rendered = " ".join(summary.values())
    for secret in ("sk-ant-test", "token-value", VALID_DOC_ID, "pm@example.com"):
        assert secret not in rendered
    assert summary["ANTHROPIC_API_KEY"] == "set"  # pragma: allowlist secret
    assert summary["GOOGLE_DOC_ID"] == "set"
    assert summary["PULSE_EMAIL_TO"] == "1 recipient(s)"
    assert "sk-ant-test" not in repr(s)


def test_secret_values_lists_only_set_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "token-value-123456")
    assert load_settings(env_file=None).secret_values() == ["token-value-123456"]


def test_settings_are_immutable() -> None:
    s = load_settings(env_file=None)
    with pytest.raises(ValueError, match="frozen"):
        s.analysis_window_weeks = 9  # type: ignore[misc]
