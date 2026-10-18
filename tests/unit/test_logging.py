from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from groww_pulse.logging_setup import (
    SecretMasker,
    bind_run_id,
    configure_logging,
    current_run_id,
    new_run_id,
)

# All values below are fake and exist only to exercise the masker.
FAKE_API_KEY = "sk-ant-api03-FAKEFAKEFAKEFAKEFAKE"  # pragma: allowlist secret
FAKE_DOC_ID = "1FAKEdocIDfakeDOCidFAKEdocID0000000000000"


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging("DEBUG", "json", secrets=[FAKE_DOC_ID], stream=stream)
    yield stream
    configure_logging("WARNING", "json", stream=io.StringIO())


def _records(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


@pytest.mark.parametrize(
    ("raw", "leaked"),
    [
        (f"using key {FAKE_API_KEY}", "FAKEFAKEFAKE"),
        ("Authorization: Bearer abcdefghijklmnop123", "abcdefghijklmnop123"),
        ("access ya29.a0AfH6SMBfake-token_value", "a0AfH6SMBfake"),
        ("refresh 1//0gFAKErefreshTOKENvalue", "0gFAKErefresh"),
        ('{"refresh_token": "opaque-refresh-value"}', "opaque-refresh-value"),
        ("api_key=plainvalue123", "plainvalue123"),
        ("client_secret: GOCSPX-fakesecret", "GOCSPX-fakesecret"),
        ("https://docs.google.com/document/d/AnyDocId_123-xyz/edit", "AnyDocId_123-xyz"),
        ("send to someone.real@example.com now", "someone.real@example.com"),
    ],
)
def test_masker_patterns(raw: str, leaked: str) -> None:
    masked = SecretMasker().mask(raw)
    assert leaked not in masked
    assert "REDACTED" in masked or "[EMAIL]" in masked


def test_masker_registered_literals() -> None:
    masker = SecretMasker(["my-literal-secret", "abc"])  # "abc" is too short to register
    assert masker.mask("x my-literal-secret y abc") == "x [REDACTED] y abc"


def test_masker_leaves_ordinary_text_alone() -> None:
    text = "Imported 1,204 reviews for com.nextbillion.groww in window 2026-W30..W41"
    assert SecretMasker().mask(text) == text


def test_log_message_args_and_extras_are_masked(log_stream: io.StringIO) -> None:
    log = logging.getLogger("groww_pulse.test")
    log.info("calling with %s", FAKE_API_KEY, extra={"document_id": FAKE_DOC_ID})
    out = log_stream.getvalue()
    assert FAKE_API_KEY not in out
    assert FAKE_DOC_ID not in out
    rec = _records(log_stream)[0]
    assert rec["level"] == "INFO"
    assert rec["logger"] == "groww_pulse.test"
    assert rec["document_id"] == "[REDACTED]"


def test_exception_traceback_is_masked(log_stream: io.StringIO) -> None:
    log = logging.getLogger("groww_pulse.test")
    try:
        raise RuntimeError(f"upstream rejected token={FAKE_API_KEY}")
    except RuntimeError:
        log.exception("delivery failed")
    out = log_stream.getvalue()
    assert "FAKEFAKEFAKE" not in out
    assert "RuntimeError" in _records(log_stream)[0]["exc"]  # type: ignore[operator]


def test_run_id_is_bound_and_reset(log_stream: io.StringIO) -> None:
    log = logging.getLogger("groww_pulse.test")
    with bind_run_id("run-123") as rid:
        assert rid == "run-123"
        assert current_run_id() == "run-123"
        log.info("inside")
    log.info("outside")
    inside, outside = _records(log_stream)
    assert inside["run_id"] == "run-123"
    assert outside["run_id"] == "-"


def test_new_run_ids_are_unique_and_sortable() -> None:
    ids = {new_run_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i.split("-")) == 2 and i[8] == "T" for i in ids)


def test_text_format_is_masked() -> None:
    stream = io.StringIO()
    configure_logging("INFO", "text", stream=stream)
    logging.getLogger("groww_pulse.test").info("key %s", FAKE_API_KEY, extra={"stage": "x"})
    out = stream.getvalue()
    assert FAKE_API_KEY not in out
    assert "stage=x" in out
    configure_logging("WARNING", "json", stream=io.StringIO())


def test_configure_logging_is_idempotent() -> None:
    for _ in range(3):
        configure_logging("INFO", "json", stream=io.StringIO())
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_groww_pulse_handler", False)]
    assert len(ours) == 1
