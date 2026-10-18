# SYNTHETIC TEST FIXTURE — not real review data
"""Phase 6 Streamlit dashboard, driven with ``AppTest``. No network, no real data.

Pages with a pulse use one built by the real Phases 2-4 from labelled fixtures. The app
refuses synthetic pulses, so those tests patch :func:`groww_pulse.dashboard.load_dashboard`
to opt in, exactly as the dashboard unit tests do.
"""

from __future__ import annotations

import functools
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from groww_pulse import dashboard
from groww_pulse.config import DeliveryMode, Settings, load_settings
from groww_pulse.delivery.ledger import (
    LEDGER_FILE,
    DeliveryLedger,
    LedgerEntry,
    LedgerStatus,
    Target,
)
from groww_pulse.delivery.publisher import DeliveryReport
from groww_pulse.report.render import quote_display
from tests.fixtures.report import build_pulse, build_variant

APP = str(Path(__file__).parents[2] / "app" / "streamlit_app.py")
TOKEN = "fake-mcp-token-for-ui-test-0123456789"
DOC_ID = "c" * 44


@pytest.fixture(scope="module")
def built_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data = tmp_path_factory.mktemp("ui") / "data"
    build_pulse(load_settings(env_file=None, data_dir=data, log_level="WARNING"))
    return data


@pytest.fixture
def no_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty working directory (no .env) and none of the app's variables set."""
    monkeypatch.chdir(tmp_path)
    for name in (
        "DELIVERY_MODE", "MCP_SERVER_URL", "MCP_AUTH_TOKEN", "GOOGLE_DOC_ID", "PULSE_EMAIL_TO",
        "ANTHROPIC_API_KEY", "GROQ_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "DATA_DIR",
    ):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    return tmp_path


@pytest.fixture
def with_pulse(no_env: Path, built_data: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(built_data, no_env / "data")
    monkeypatch.setattr(
        dashboard,
        "load_dashboard",
        functools.partial(dashboard.load_dashboard, allow_synthetic=True),
    )
    return no_env / "data"


def _configure_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_MODE", "confirm")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.test/")
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("GOOGLE_DOC_ID", DOC_ID)
    monkeypatch.setenv("PULSE_EMAIL_TO", "pm@example.com")


def _run() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception
    return at


def _all_text(at: AppTest) -> str:
    elements: list[Any] = [
        *at.markdown, *at.caption, *at.info, *at.success, *at.warning, *at.error, *at.code,
    ]  # fmt: skip
    parts = [str(e.value) for e in elements]
    parts += [f"{m.label} {m.value}" for m in at.metric]
    parts += [str(t.value) for t in (*at.table, *at.dataframe)]
    parts += [w.label for w in (*at.button, *at.checkbox)]
    return " ".join(parts)


def _fake_report(settings: Settings, mode: DeliveryMode) -> DeliveryReport:
    return DeliveryReport(
        run_id="ui-run",
        mode=mode,
        pulse_run_id="report-run",
        iso_week="2026-W42",
        content_hash="0" * 64,
        targets=[],
    )


# ---------------------------------------------------------------- empty and error states
def test_empty_state_without_secrets_or_data(no_env: Path) -> None:
    at = _run()
    assert at.title[0].value == "Groww Review Pulse Agent"
    text = _all_text(at)
    assert "dry_run" in text
    assert "No validated weekly pulse yet" in text
    assert "groww-pulse report" in text
    assert len(at.warning) == 3  # LLM, MCP and delivery not configured
    assert not at.tabs
    assert not at.button  # nothing to preview or deliver


def test_invalid_configuration_is_reported(no_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANALYSIS_WINDOW_WEEKS", "20")
    at = _run()
    assert at.error
    assert "ANALYSIS_WINDOW_WEEKS" in at.error[0].value


def test_synthetic_pulse_is_refused_without_the_test_opt_in(no_env: Path, built_data: Path) -> None:
    shutil.copytree(built_data, no_env / "data")
    at = _run()
    text = _all_text(at)
    assert "failed verification" in at.expander[0].label
    assert "synthetic" in text
    assert "No validated weekly pulse yet" in text
    assert not at.tabs


# ---------------------------------------------------------------- content
def test_pulse_themes_quotes_actions_and_statistics_render(with_pulse: Path) -> None:
    at = _run()
    assert [t.label for t in at.tabs] == [
        "Weekly pulse",
        "Themes, quotes & actions",
        "Review statistics",
        "Delivery",
        "Settings",
    ]
    board = dashboard.load_dashboard(load_settings(env_file=None, data_dir=with_pulse))
    assert board.pulse is not None
    pulse = board.pulse.pulse
    text = _all_text(at)
    assert "SYNTHETIC TEST DATA" in text
    assert pulse.iso_week in text
    assert "Validators passed" in text
    assert "6 / 6" in text
    for v in board.pulse.validators:
        assert v["name"] in text
    for q in pulse.quotes:
        assert q.review_id in text
    # st.code drops the final newline when displaying.
    assert board.pulse.verified.full_text.rstrip("\n") in [c.value.rstrip("\n") for c in at.code]
    for theme in pulse.themes:
        assert theme.name in text
    for action in pulse.actions:
        assert action.action.split()[0] in text
    assert "Preprocessing funnel" in [s.value for s in at.subheader]
    assert "Ranking: score = count" in text


def test_quotes_are_shown_verbatim_with_the_ellipsis_outside(with_pulse: Path) -> None:
    at = _run()
    board = dashboard.load_dashboard(load_settings(env_file=None, data_dir=with_pulse))
    assert board.pulse is not None
    # Escaped for Markdown; unescaping restores exactly the stored quote display.
    shown = [re.sub(r"\\(.)", r"\1", m.value) for m in at.markdown]
    quotes = board.pulse.pulse.quotes
    for q in quotes:
        assert quote_display(q) in shown
    truncated = [q for q in quotes if q.trailing_ellipsis]
    assert truncated  # the fixture pulse has one
    for q in truncated:
        assert quote_display(q) == f'"{q.text}"…'


# ---------------------------------------------------------------- delivery safety
def test_delivery_is_disabled_in_dry_run_mode(with_pulse: Path) -> None:
    at = _run()
    assert at.button(key="deliver").disabled
    assert at.checkbox(key="confirm_delivery").disabled
    assert "DELIVERY_MODE is not 'confirm'" in _all_text(at)
    assert at.button(key="health").disabled  # no MCP_SERVER_URL


def test_no_send_control_exists(with_pulse: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_live(monkeypatch)
    at = _run()
    labels = [w.label.lower() for w in (*at.button, *at.checkbox)]
    assert labels
    assert not any("send" in label for label in labels if "no email will be sent" not in label)
    assert all("force" not in label for label in labels)


def test_preview_then_confirmation_gates_live_delivery(
    with_pulse: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_live(monkeypatch)
    calls: list[str] = []

    def preview(settings: Settings, pulse_dir: Path, *, run_id: str) -> DeliveryReport:
        calls.append("preview")
        return _fake_report(settings, DeliveryMode.DRY_RUN)

    def deliver(settings: Settings, pulse_dir: Path, *, run_id: str) -> DeliveryReport:
        calls.append("deliver")
        return _fake_report(settings, DeliveryMode.CONFIRM)

    monkeypatch.setattr(dashboard, "preview_delivery", preview)
    monkeypatch.setattr(dashboard, "deliver", deliver)
    # The synthetic-data blocker is real; drop it to exercise the UI gate itself.
    real = dashboard.live_delivery_blockers
    monkeypatch.setattr(
        dashboard,
        "live_delivery_blockers",
        lambda s, b, v=None: [x for x in real(s, b, v) if "synthetic" not in x],
    )

    at = _run()
    assert at.button(key="deliver").disabled
    assert at.checkbox(key="confirm_delivery").disabled
    assert "preview this pulse with a dry run first" in _all_text(at)

    at.button(key="preview").click().run()
    assert calls == ["preview"]
    assert not at.checkbox(key="confirm_delivery").disabled
    assert at.button(key="deliver").disabled  # not confirmed yet

    at.checkbox(key="confirm_delivery").check().run()
    assert not at.button(key="deliver").disabled

    at.button(key="deliver").click().run()
    assert calls == ["preview", "deliver"]
    assert "Delivered" in _all_text(at)
    assert not at.checkbox(key="confirm_delivery").value  # confirmation resets


def test_refused_delivery_shows_the_reason(
    with_pulse: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_live(monkeypatch)
    at = _run()
    # The real publisher refuses the synthetic pulse even for a dry run.
    at.button(key="preview").click().run()
    assert any("Preview refused" in e.value and "synthetic" in e.value for e in at.error)
    assert at.button(key="deliver").disabled


def test_secret_values_are_never_displayed(
    with_pulse: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_live(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-value-for-ui-test")
    at = _run()
    rendered = _all_text(at)
    assert "sk-ant-fake" not in rendered
    assert TOKEN not in rendered
    assert DOC_ID not in rendered
    assert "pm@example.com" not in rendered


# ---------------------------------------------------------------- one version per ISO week
def _record(data: Path, target: Target, status: LedgerStatus, content_hash: str) -> None:
    board = dashboard.load_dashboard(load_settings(env_file=None, data_dir=data))
    assert board.pulse is not None
    DeliveryLedger(data / LEDGER_FILE).record(
        LedgerEntry.make(
            key=f"{target.value}-{content_hash}",
            target=target,
            status=status,
            iso_week=board.pulse.verified.iso_week,
            content_hash=content_hash,
            run_id="earlier-delivery",
        )
    )


def _delivery_statuses(at: AppTest) -> list[str]:
    """The Status column of the Delivery tab's ledger table."""
    table = next(d.value for d in at.dataframe if "Status" in d.value.columns)
    return [str(s) for s in table["Status"].tolist()]


def _original(data: Path) -> dashboard.DashboardPulse:
    board = dashboard.load_dashboard(load_settings(env_file=None, data_dir=data))
    assert board.pulse is not None
    return board.pulse


def _drop_synthetic_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    real = dashboard.live_delivery_blockers
    monkeypatch.setattr(
        dashboard,
        "live_delivery_blockers",
        lambda s, b, v=None: [x for x in real(s, b, v) if "synthetic" not in x],
    )


def test_same_week_different_hash_is_shown_blocked_without_override(
    with_pulse: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_live(monkeypatch)
    _drop_synthetic_blocker(monkeypatch)
    original = _original(with_pulse)
    for target in Target:
        _record(with_pulse, target, LedgerStatus.SUCCEEDED, original.verified.content_hash)
    build_variant(original.directory, "report-run-v2")
    calls: list[str] = []
    monkeypatch.setattr(dashboard, "deliver", lambda *a, **k: calls.append("deliver"))

    at = _run()
    errors = " ".join(e.value for e in at.error)
    assert "Blocked: a different version" in errors
    assert "was already delivered" in errors
    assert "no override" in errors
    statuses = _delivery_statuses(at)
    assert statuses == ["blocked: week already delivered"] * 2
    assert "duplicate-week protection" in _all_text(at)

    # Even after a successful dry run, the confirmation and the button stay disabled.
    monkeypatch.setattr(
        dashboard,
        "preview_delivery",
        lambda s, d, *, run_id: _fake_report(s, DeliveryMode.DRY_RUN),
    )
    at.button(key="preview").click().run()
    assert at.checkbox(key="confirm_delivery").disabled
    assert at.button(key="deliver").disabled
    assert calls == []
    labels = [w.label.lower() for w in (*at.button, *at.checkbox)]
    assert not any("force" in label or "override" in label for label in labels)
    # Nothing to finish: the earlier version reached both targets.
    assert not [b for b in at.button if b.key and b.key.startswith("recovery_")]


def test_partly_delivered_week_can_be_finished_from_the_ui(
    with_pulse: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_live(monkeypatch)
    _drop_synthetic_blocker(monkeypatch)
    original = _original(with_pulse)
    _record(with_pulse, Target.GOOGLE_DOC, LedgerStatus.SUCCEEDED, original.verified.content_hash)
    _record(with_pulse, Target.GMAIL_DRAFT, LedgerStatus.FAILED, original.verified.content_hash)
    build_variant(original.directory, "report-run-v2")
    delivered: list[Path] = []

    def deliver(settings: Settings, pulse_dir: Path, *, run_id: str) -> DeliveryReport:
        delivered.append(pulse_dir)
        return _fake_report(settings, DeliveryMode.CONFIRM)

    monkeypatch.setattr(
        dashboard,
        "preview_delivery",
        lambda s, d, *, run_id: _fake_report(s, DeliveryMode.DRY_RUN),
    )
    monkeypatch.setattr(dashboard, "deliver", deliver)

    at = _run()
    assert "Blocked: a different version" in " ".join(e.value for e in at.error)
    assert any("Finish the delivered" in s.value for s in at.subheader)
    assert at.button(key="deliver").disabled  # the regenerated version stays blocked

    at.button(key="recovery_preview").click().run()
    assert at.button(key="deliver").disabled
    at.checkbox(key="recovery_confirm_delivery").check().run()
    assert not at.button(key="recovery_deliver").disabled
    at.button(key="recovery_deliver").click().run()
    assert [p.resolve() for p in delivered] == [original.directory.resolve()]  # never the new one


def test_existing_delivery_of_this_pulse_is_not_shown_as_blocked(
    with_pulse: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_live(monkeypatch)
    original = _original(with_pulse)
    _record(with_pulse, Target.GOOGLE_DOC, LedgerStatus.SUCCEEDED, original.verified.content_hash)
    at = _run()
    statuses = _delivery_statuses(at)
    assert statuses == ["succeeded", "not delivered"]
    assert not any("Blocked" in e.value for e in at.error)
    assert "duplicate-week protection" not in _all_text(at)
