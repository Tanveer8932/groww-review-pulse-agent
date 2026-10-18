"""Environment-driven application configuration (architecture §8).

All settings come from environment variables (or a local ``.env`` file, or Streamlit secrets,
which Streamlit Community Cloud exposes as environment variables). Nothing sensitive has a
default value. Validation errors are re-raised as :class:`ConfigError` with field names
only — never the offending values, which may be secrets.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, HttpUrl, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from groww_pulse.errors import ConfigError

DEFAULT_APP_ID = "com.nextbillion.groww"
MIN_WINDOW_WEEKS = 8
MAX_WINDOW_WEEKS = 12

_APP_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
_DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{25,128}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
#: Free-tier Groq model with strict (constrained-decoding) JSON-schema outputs.
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"


class EmojiPolicy(StrEnum):
    STRICT = "strict"
    LENIENT = "lenient"


class DeliveryMode(StrEnum):
    DRY_RUN = "dry_run"
    CONFIRM = "confirm"


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    GROQ = "groq"


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["json", "text"]


class Settings(BaseSettings):
    """Validated runtime configuration. Construct via :func:`load_settings`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    # Collection
    app_id: str = DEFAULT_APP_ID
    analysis_window_weeks: int = Field(default=12, ge=MIN_WINDOW_WEEKS, le=MAX_WINDOW_WEEKS)
    enable_play_collector: bool = False

    # Preprocessing
    min_words_exclusive: int = Field(default=8, ge=1)
    emoji_policy: EmojiPolicy = EmojiPolicy.STRICT
    lang_min_confidence: float = Field(default=0.80, ge=0.0, le=1.0)
    lang_lexical_rescue: bool = True

    # LLM
    llm_provider: LLMProvider = LLMProvider.ANTHROPIC
    llm_model: str = Field(default=DEFAULT_ANTHROPIC_MODEL, min_length=1)
    anthropic_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    # Groq free tier: one request's prompt + max_completion_tokens must fit the per-minute
    # token limit, and requests are paced to stay under it (docs/architecture.md §6.4).
    groq_tokens_per_minute: int = Field(default=8_000, ge=2_000)
    groq_max_output_tokens: int = Field(default=2_500, ge=256)
    llm_max_tokens_per_run: int = Field(default=400_000, gt=0)
    llm_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    analysis_min_reviews: int = Field(default=20, ge=3)

    # MCP delivery
    mcp_server_url: HttpUrl | None = None
    mcp_auth_token: SecretStr | None = None
    mcp_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    google_doc_id: SecretStr | None = None
    pulse_email_to: str | None = None
    delivery_mode: DeliveryMode = DeliveryMode.DRY_RUN

    # Storage / ops
    data_dir: Path = Path("./data")
    # Read-only dashboard snapshot used when DATA_DIR has no pulse (hosted UI). Unset: the
    # app uses the `snapshot/` directory next to `app/`.
    snapshot_dir: Path | None = None
    raw_retention_days: int = Field(default=90, ge=1)
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "json"

    # ------------------------------------------------------------ validators
    @model_validator(mode="before")
    @classmethod
    def _default_model_for_provider(cls, data: Any) -> Any:
        """``LLM_MODEL`` defaults per provider, so ``LLM_PROVIDER=groq`` alone never sends
        a Claude model name to Groq."""
        if not isinstance(data, dict):
            return data
        provider = str(data.get("llm_provider") or "").strip().lower()
        if provider == LLMProvider.GROQ and not data.get("llm_model"):
            return {**data, "llm_model": DEFAULT_GROQ_MODEL}
        return data

    @model_validator(mode="after")
    def _check_model_matches_provider(self) -> Settings:
        if self.llm_provider is LLMProvider.GROQ and self.llm_model.startswith("claude-"):
            raise ValueError("LLM_MODEL is a Claude model but LLM_PROVIDER=groq")
        if self.groq_max_output_tokens >= self.groq_tokens_per_minute:
            raise ValueError("GROQ_MAX_OUTPUT_TOKENS must be below GROQ_TOKENS_PER_MINUTE")
        return self

    @field_validator("app_id")
    @classmethod
    def _check_app_id(cls, value: str) -> str:
        if not _APP_ID_RE.fullmatch(value):
            raise ValueError("must be an Android package name such as com.example.app")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("mcp_server_url")
    @classmethod
    def _check_mcp_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and value.scheme != "https" and value.host not in _LOCAL_HOSTS:
            raise ValueError("must use https (plain http is only allowed for localhost)")
        return value

    @field_validator("google_doc_id")
    @classmethod
    def _check_doc_id(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        if "docs.google.com" in raw or "/" in raw:
            raise ValueError("must be the bare document ID, not the document URL")
        if not _DOC_ID_RE.fullmatch(raw):
            raise ValueError("does not look like a Google Doc ID ([A-Za-z0-9_-], 25-128 chars)")
        return SecretStr(raw)

    @field_validator("pulse_email_to")
    @classmethod
    def _check_recipients(cls, value: str | None) -> str | None:
        if value is None:
            return None
        recipients = [part.strip() for part in value.split(",") if part.strip()]
        if not recipients:
            return None
        invalid = [r for r in recipients if not _EMAIL_RE.fullmatch(r)]
        if invalid:
            raise ValueError(f"contains {len(invalid)} invalid email address(es)")
        return ",".join(recipients)

    # ------------------------------------------------------------ derived values
    @property
    def pulse_recipients(self) -> list[str]:
        """Draft recipients as a list (the MCP ``to`` field is an array)."""
        return self.pulse_email_to.split(",") if self.pulse_email_to else []

    def secret_values(self) -> list[str]:
        """Literal values that the log masker must never emit."""
        secrets = [
            self.anthropic_api_key,
            self.groq_api_key,
            self.mcp_auth_token,
            self.google_doc_id,
        ]
        return [s.get_secret_value() for s in secrets if s is not None]

    def public_summary(self) -> dict[str, str]:
        """Non-sensitive view for the UI and logs; secrets are reported only as set/not set."""

        def flag(value: object) -> str:
            return "set" if value else "not set"

        return {
            "APP_ID": self.app_id,
            "ANALYSIS_WINDOW_WEEKS": str(self.analysis_window_weeks),
            "ENABLE_PLAY_COLLECTOR": str(self.enable_play_collector).lower(),
            "MIN_WORDS_EXCLUSIVE": str(self.min_words_exclusive),
            "EMOJI_POLICY": self.emoji_policy.value,
            "LANG_MIN_CONFIDENCE": f"{self.lang_min_confidence:.2f}",
            "LANG_LEXICAL_RESCUE": str(self.lang_lexical_rescue).lower(),
            "LLM_PROVIDER": self.llm_provider.value,
            "LLM_MODEL": self.llm_model,
            "ANTHROPIC_API_KEY": flag(self.anthropic_api_key),
            "GROQ_API_KEY": flag(self.groq_api_key),
            "LLM_MAX_TOKENS_PER_RUN": str(self.llm_max_tokens_per_run),
            "ANALYSIS_MIN_REVIEWS": str(self.analysis_min_reviews),
            "MCP_SERVER_URL": str(self.mcp_server_url) if self.mcp_server_url else "not set",
            "MCP_AUTH_TOKEN": flag(self.mcp_auth_token),
            "GOOGLE_DOC_ID": flag(self.google_doc_id),
            "PULSE_EMAIL_TO": f"{len(self.pulse_recipients)} recipient(s)",
            "DELIVERY_MODE": self.delivery_mode.value,
            "DATA_DIR": str(self.data_dir),
            "SNAPSHOT_DIR": str(self.snapshot_dir) if self.snapshot_dir else "(default)",
            "RAW_RETENTION_DAYS": str(self.raw_retention_days),
            "LOG_LEVEL": self.log_level,
        }

    # ------------------------------------------------------------ stage requirements
    def require_llm(self) -> None:
        """Raise :class:`ConfigError` unless the LLM provider is fully configured."""
        missing = []
        if self.llm_provider is LLMProvider.ANTHROPIC and self.anthropic_api_key is None:
            missing.append("ANTHROPIC_API_KEY")
        if self.llm_provider is LLMProvider.GROQ and self.groq_api_key is None:
            missing.append("GROQ_API_KEY")
        _raise_if_missing("LLM analysis", missing)

    def require_mcp(self) -> None:
        """Raise :class:`ConfigError` unless the MCP endpoint is configured."""
        _raise_if_missing("MCP access", [] if self.mcp_server_url else ["MCP_SERVER_URL"])

    def require_delivery(self) -> None:
        """Raise :class:`ConfigError` unless every delivery target is configured."""
        missing = [] if self.mcp_server_url else ["MCP_SERVER_URL"]
        if self.google_doc_id is None:
            missing.append("GOOGLE_DOC_ID")
        if not self.pulse_recipients:
            missing.append("PULSE_EMAIL_TO")
        _raise_if_missing("delivery", missing)


def _raise_if_missing(purpose: str, missing: list[str]) -> None:
    if missing:
        raise ConfigError(f"Missing required configuration for {purpose}: {', '.join(missing)}")


def load_settings(env_file: str | Path | None = ".env", **overrides: Any) -> Settings:
    """Load and validate settings from the environment (and ``env_file`` if it exists).

    Raises:
        ConfigError: if any value is invalid. The message names fields, never values.
    """
    try:
        return Settings(_env_file=env_file, **overrides)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']).upper() or 'settings'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"Invalid configuration — {problems}") from None
