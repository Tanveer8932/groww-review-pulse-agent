# SYNTHETIC TEST FIXTURE — not real review data
"""Phase 5 publisher and ledger, end to end against the in-process fake MCP server.

The pulse comes from the real Phases 2-4 on the labelled synthetic corpus, so it carries
``publishable: false``; tests opt in with ``allow_synthetic=True`` (the CLI never does).
No test touches the network or a Google API.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
from pathlib import Path
from typing import Any

import pytest

from groww_pulse.config import Settings, load_settings
from groww_pulse.delivery.contract import DOC_APPEND_TOOL, DRAFT_TOOL, SEND_TOOL
from groww_pulse.delivery.ledger import (
    LEDGER_FILE,
    DeliveryLedger,
    LedgerEntry,
    LedgerStatus,
    Target,
    doc_key,
    draft_key,
    week_lock,
)
from groww_pulse.delivery.publisher import (
    DeliveryReport,
    load_publishable_pulse,
    publish,
)
from groww_pulse.errors import (
    ConfigError,
    DeliveryError,
    MCPAuthError,
    MCPUnavailableError,
    SecurityGateError,
)
from tests.fixtures.fake_mcp import URL, FakeMcpServer
from tests.fixtures.report import build_pulse, build_variant

DOC_ID = "fakeTestDocument_" + "a" * 20
RECIPIENTS = ["pm@example.com", "lead@example.com"]


@pytest.fixture(scope="module")
def built_pulse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data = tmp_path_factory.mktemp("pulse") / "data"
    return build_pulse(load_settings(env_file=None, data_dir=data, log_level="WARNING"))


@pytest.fixture
def token() -> str:
    return secrets.token_urlsafe(24)  # random per test: never a committed credential


def make_settings(tmp_path: Path, token: str | None, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "log_level": "WARNING",
        "mcp_server_url": URL,
        "mcp_auth_token": token,
        "google_doc_id": DOC_ID,
        "pulse_email_to": ",".join(RECIPIENTS),
        "delivery_mode": "confirm",
    }
    values.update(overrides)
    return load_settings(env_file=None, **{k: v for k, v in values.items() if v is not None})


@pytest.fixture
def settings(tmp_path: Path, token: str, built_pulse: Path) -> Settings:
    s = make_settings(tmp_path, token)
    shutil.copytree(built_pulse, s.data_dir / "runs" / built_pulse.name)
    return s


@pytest.fixture
def pulse_text(settings: Settings, built_pulse: Path) -> str:
    return (built_pulse / "pulse.txt").read_text("utf-8")


def run(
    settings: Settings, fake: FakeMcpServer, token: str | None, run_id: str = "pub-1", **kw: Any
) -> DeliveryReport:
    kw.setdefault("confirm", True)
    return publish(settings, run_id=run_id, client=fake.client(token), allow_synthetic=True, **kw)


def statuses(report: DeliveryReport) -> dict[str, str]:
    return {t.target.value: t.status.value for t in report.targets}


def ledger(settings: Settings) -> list[LedgerEntry]:
    return DeliveryLedger(settings.data_dir / LEDGER_FILE).entries()


# ---------------------------------------------------------------- pulse input
def test_synthetic_pulse_is_refused_for_production(settings: Settings, token: str) -> None:
    fake = FakeMcpServer(token=token)
    with pytest.raises(DeliveryError, match="synthetic"):
        publish(settings, run_id="r", confirm=True, client=fake.client(token))
    assert fake.requests == []


def test_missing_pulse_is_refused(tmp_path: Path, token: str) -> None:
    with pytest.raises(DeliveryError, match=r"no pulse\.json found"):
        publish(make_settings(tmp_path, token), run_id="r", allow_synthetic=True)


def _pulse_dir(settings: Settings) -> Path:
    return next((settings.data_dir / "runs").glob("*/pulse.json")).parent


def _edit_pulse_json(settings: Settings, mutate: Any) -> None:
    path = _pulse_dir(settings) / "pulse.json"
    data = json.loads(path.read_text("utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d["validators"][4].update(passed=False), "validators did not all pass"),
        (lambda d: d["pulse"].update(content_hash="0" * 64), "content_hash does not match"),
        (lambda d: d.update(audit_marker="[groww-pulse:x]"), "audit marker"),
        (lambda d: d.update(schema_version=9), "schema_version 9"),
        (lambda d: d.update(detail_level=7), "detail_level 7"),
        (lambda d: d["pulse"].update(n_analyzed=999), "does not re-render"),
        (lambda d: d.pop("pulse"), "not a valid Phase 4 pulse"),
    ],
)
def test_tampered_pulse_json_is_refused(settings: Settings, mutate: Any, match: str) -> None:
    _edit_pulse_json(settings, mutate)
    with pytest.raises(DeliveryError, match=match):
        load_publishable_pulse(_pulse_dir(settings), allow_synthetic=True)


def test_pii_added_to_the_pulse_is_refused_even_if_rehashed(settings: Settings) -> None:
    import hashlib

    from groww_pulse.report.render import audit_marker

    def plant(data: dict[str, Any]) -> None:
        pulse = data["pulse"]
        pulse["body_text"] = pulse["body_text"].replace(
            "Action ideas", "Call 9876543210\nAction ideas"
        )
        pulse["content_hash"] = hashlib.sha256(pulse["body_text"].encode()).hexdigest()
        data["audit_marker"] = audit_marker(pulse["iso_week"], pulse["content_hash"])

    _edit_pulse_json(settings, plant)
    directory = _pulse_dir(settings)
    data = json.loads((directory / "pulse.json").read_text("utf-8"))
    marker, body = data["audit_marker"], data["pulse"]["body_text"]
    (directory / "pulse.txt").write_text(f"{marker}\n{body}", encoding="utf-8")
    # The delivered body must be exactly the rendering of the validated content.
    with pytest.raises(DeliveryError, match="does not re-render"):
        load_publishable_pulse(directory, allow_synthetic=True)


def test_pii_in_structured_content_fails_the_pii_validator(settings: Settings) -> None:
    """A fully consistent, re-signed pulse with PII in an action is still refused."""
    import hashlib

    from groww_pulse.models import Pulse
    from groww_pulse.report.render import PulseContent, audit_marker, render

    directory = _pulse_dir(settings)
    data = json.loads((directory / "pulse.json").read_text("utf-8"))
    data["pulse"]["actions"][0]["action"] = "Call the reviewer back on 9876543210"
    pulse = Pulse.model_validate(data["pulse"])
    rendered = render(
        PulseContent(
            iso_week=pulse.iso_week,
            window_start=pulse.window_start,
            window_end=pulse.window_end,
            n_collected=pulse.n_collected,
            n_analyzed=pulse.n_analyzed,
            themes=tuple(pulse.themes),
            quotes=tuple(pulse.quotes),
            actions=tuple(pulse.actions),
            synthetic=True,
        ),
        data["detail_level"],
    )
    digest = hashlib.sha256(rendered.body_text.encode()).hexdigest()
    marker = audit_marker(pulse.iso_week, digest)
    data["pulse"].update(
        body_text=rendered.body_text, body_markdown=rendered.body_markdown, content_hash=digest
    )
    data["audit_marker"] = marker
    (directory / "pulse.json").write_text(json.dumps(data), encoding="utf-8")
    (directory / "pulse.txt").write_text(f"{marker}\n{rendered.body_text}", encoding="utf-8")
    (directory / "pulse.md").write_text(f"{marker}\n\n{rendered.body_markdown}", encoding="utf-8")
    with pytest.raises(DeliveryError, match="no_pii: action 1 matches PHONE") as excinfo:
        load_publishable_pulse(directory, allow_synthetic=True)
    assert "9876543210" not in str(excinfo.value)


def test_edited_pulse_txt_is_refused(settings: Settings) -> None:
    path = _pulse_dir(settings) / "pulse.txt"
    path.write_text(path.read_text("utf-8").replace("reviews", "posts", 1), encoding="utf-8")
    with pytest.raises(DeliveryError, match=r"pulse\.txt is missing or differs"):
        load_publishable_pulse(_pulse_dir(settings), allow_synthetic=True)


# ---------------------------------------------------------------- dry run
def test_dry_run_is_the_default_and_makes_no_network_calls(
    settings: Settings, token: str, pulse_text: str
) -> None:
    fake = FakeMcpServer(token=token)
    report = publish(settings, run_id="dry", client=fake.client(token), allow_synthetic=True)
    assert fake.requests == []
    assert report.mode.value == "dry_run"
    assert report.ok
    assert statuses(report) == {"google_doc": "would_deliver", "gmail_draft": "would_deliver"}
    doc, draft = report.targets
    assert doc.arguments_preview == {
        "documentId": "<GOOGLE_DOC_ID>",
        "content": pulse_text,
        "ensureLeadingNewline": True,
    }
    assert draft.arguments_preview["to"] == ["<recipient 1>", "<recipient 2>"]
    assert draft.arguments_preview["subject"] == f"Groww Review Pulse — {report.iso_week}"
    assert draft.arguments_preview["bodyType"] == "text"
    assert ledger(settings) == []
    saved = (settings.data_dir / "runs" / "dry" / "delivery.json").read_text("utf-8")
    assert DOC_ID not in saved
    assert not any(r in saved for r in RECIPIENTS)
    assert json.loads(saved)["email_sent"] is False


def test_dry_run_works_without_delivery_configuration(tmp_path: Path, built_pulse: Path) -> None:
    s = load_settings(env_file=None, data_dir=tmp_path / "data", log_level="WARNING")
    shutil.copytree(built_pulse, s.data_dir / "runs" / built_pulse.name)
    report = publish(s, run_id="dry", allow_synthetic=True)
    assert statuses(report) == {"google_doc": "not_configured", "gmail_draft": "not_configured"}


def test_confirm_flag_without_confirm_mode_is_refused(
    tmp_path: Path, token: str, built_pulse: Path
) -> None:
    s = make_settings(tmp_path, token, delivery_mode="dry_run")
    shutil.copytree(built_pulse, s.data_dir / "runs" / built_pulse.name)
    fake = FakeMcpServer(token=token)
    with pytest.raises(DeliveryError, match="DELIVERY_MODE=confirm"):
        run(s, fake, token)
    assert fake.requests == []


def test_confirm_mode_without_the_flag_is_still_a_dry_run(settings: Settings, token: str) -> None:
    fake = FakeMcpServer(token=token)
    report = run(settings, fake, token, confirm=False)
    assert report.mode.value == "dry_run"
    assert fake.requests == []


@pytest.mark.parametrize("missing", ["google_doc_id", "pulse_email_to"])
def test_live_delivery_needs_full_configuration(
    tmp_path: Path, token: str, built_pulse: Path, missing: str
) -> None:
    s = make_settings(tmp_path, token, **{missing: None})
    shutil.copytree(built_pulse, s.data_dir / "runs" / built_pulse.name)
    fake = FakeMcpServer(token=token)
    with pytest.raises(ConfigError, match=missing.upper()):
        run(s, fake, token)
    assert fake.requests == []


# ---------------------------------------------------------------- security gate
def test_live_delivery_needs_an_auth_token(tmp_path: Path, built_pulse: Path) -> None:
    s = make_settings(tmp_path, None)
    shutil.copytree(built_pulse, s.data_dir / "runs" / built_pulse.name)
    fake = FakeMcpServer(token=None)
    with pytest.raises(SecurityGateError, match="MCP_AUTH_TOKEN is not set"):
        run(s, fake, None)
    assert fake.requests == []


def test_unauthenticated_server_blocks_live_delivery(settings: Settings, token: str) -> None:
    fake = FakeMcpServer(token=None)  # like the deployed server today
    with pytest.raises(SecurityGateError, match="accepted a handshake with no credentials"):
        run(settings, fake, token)
    assert fake.calls == []
    assert fake.tool_call_requests == []
    assert ledger(settings) == []


def test_rejected_token_is_an_auth_error(settings: Settings, token: str) -> None:
    fake = FakeMcpServer(token="the-real-" + token)
    with pytest.raises(MCPAuthError):
        run(settings, fake, token)
    assert fake.calls == []


def test_unreachable_server_writes_nothing(settings: Settings, token: str) -> None:
    fake = FakeMcpServer(token=token, unreachable=True)
    with pytest.raises(MCPUnavailableError):
        run(settings, fake, token)
    assert ledger(settings) == []


def test_changed_contract_writes_nothing(settings: Settings, token: str) -> None:
    fake = FakeMcpServer(token=token)
    fake.drop_tool(DRAFT_TOOL)
    with pytest.raises(MCPUnavailableError, match="required MCP tools"):
        run(settings, fake, token)
    assert fake.calls == []


# ---------------------------------------------------------------- live delivery
def test_live_delivery_appends_and_drafts_exactly_once(
    settings: Settings, token: str, pulse_text: str
) -> None:
    fake = FakeMcpServer(token=token)
    report = run(settings, fake, token)
    assert report.ok
    assert statuses(report) == {"google_doc": "delivered", "gmail_draft": "delivered"}
    assert fake.calls_to(DOC_APPEND_TOOL) == [
        {"documentId": DOC_ID, "content": pulse_text, "ensureLeadingNewline": True}
    ]
    assert fake.calls_to(DRAFT_TOOL) == [
        {
            "to": RECIPIENTS,
            "subject": f"Groww Review Pulse — {report.iso_week}",
            "body": pulse_text,
            "bodyType": "text",
        }
    ]
    assert fake.calls_to(SEND_TOOL) == []
    assert report.as_dict()["email_sent"] is False
    # The health check and security gate that preceded the writes are in the report.
    assert report.health is not None
    assert report.health["security_gate_passed"] is True
    assert report.health["auth_enforced"] is True
    # Every call carried the bearer token; results are the server's own.
    assert {r["authorization"] for r in fake.tool_call_requests} == {f"Bearer {token}"}
    assert report.targets[0].result is not None
    assert report.targets[0].result.excerpt["status"] == "appended"
    assert report.targets[1].result is not None
    assert report.targets[1].result.excerpt["draftId"] == "draft-0001"
    # Ledger: pending, then succeeded, per target; keyed without the Doc ID or addresses.
    assert [(e.target, e.status) for e in ledger(settings)] == [
        ("google_doc", "pending"),
        ("google_doc", "succeeded"),
        ("gmail_draft", "pending"),
        ("gmail_draft", "succeeded"),
    ]
    raw = (settings.data_dir / LEDGER_FILE).read_text("utf-8")
    assert DOC_ID not in raw
    assert not any(r in raw for r in RECIPIENTS)


def test_rerun_is_skipped_with_zero_calls(settings: Settings, token: str) -> None:
    run(settings, FakeMcpServer(token=token), token, run_id="first")
    fake = FakeMcpServer(token=token)
    report = run(settings, fake, token, run_id="second")
    assert report.ok
    assert statuses(report) == {
        "google_doc": "skipped_already_delivered",
        "gmail_draft": "skipped_already_delivered",
    }
    assert fake.requests == []  # not even a handshake


def test_draft_failure_is_partial_and_only_the_draft_is_retried(
    settings: Settings, token: str
) -> None:
    fake = FakeMcpServer(token=token, behaviour={DRAFT_TOOL: "error:GMAIL_DRAFT_CREATE_FAILED"})
    report = run(settings, fake, token, run_id="first")
    assert not report.ok
    assert statuses(report) == {"google_doc": "delivered", "gmail_draft": "failed"}
    assert "GMAIL_DRAFT_CREATE_FAILED" in report.targets[1].detail

    retry = FakeMcpServer(token=token)
    report = run(settings, retry, token, run_id="second")
    assert report.ok
    assert statuses(report) == {
        "google_doc": "skipped_already_delivered",
        "gmail_draft": "delivered",
    }
    assert retry.calls_to(DOC_APPEND_TOOL) == []
    assert len(retry.calls_to(DRAFT_TOOL)) == 1


def test_google_auth_failure_is_reported_and_draft_still_attempted(
    settings: Settings, token: str
) -> None:
    fake = FakeMcpServer(token=token, behaviour={DOC_APPEND_TOOL: "error:GOOGLE_AUTH_REQUIRED"})
    report = run(settings, fake, token)
    assert statuses(report) == {"google_doc": "failed", "gmail_draft": "delivered"}
    assert "Google authorization failed" in report.targets[0].detail


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("read_error_after_write", "TRANSPORT_ERROR"),
        ("malformed", "UNEXPECTED_STATUS"),
        ("wrong_doc", "DOCUMENT_MISMATCH"),
    ],
)
def test_ambiguous_doc_outcome_blocks_retries_until_forced(
    settings: Settings, token: str, mode: str, code: str
) -> None:
    fake = FakeMcpServer(token=token, behaviour={DOC_APPEND_TOOL: mode})
    report = run(settings, fake, token, run_id="first")
    assert statuses(report) == {"google_doc": "outcome_unknown", "gmail_draft": "not_attempted"}
    assert report.targets[0].result is not None
    assert report.targets[0].result.error_code == code
    assert fake.calls_to(DRAFT_TOOL) == []  # stopped: the connection may be unhealthy
    assert ledger(settings)[-1].status == "unknown"

    again = FakeMcpServer(token=token)
    report = run(settings, again, token, run_id="second")
    assert statuses(report)["google_doc"] == "blocked_outcome_unknown"
    assert again.calls_to(DOC_APPEND_TOOL) == []  # never retried automatically
    assert statuses(report)["gmail_draft"] == "delivered"

    forced = FakeMcpServer(token=token)
    report = run(settings, forced, token, run_id="third", force=True)
    assert statuses(report)["google_doc"] == "delivered"
    assert len(forced.calls_to(DOC_APPEND_TOOL)) == 1
    assert ledger(settings)[-1].forced is True


@pytest.mark.parametrize(
    ("mode", "code"), [("connect_error", "CONNECT_FAILED"), ("auth_401", "MCP_AUTH_REJECTED")]
)
def test_doc_call_that_never_reached_the_server_can_be_retried(
    settings: Settings, token: str, mode: str, code: str
) -> None:
    fake = FakeMcpServer(token=token, behaviour={DOC_APPEND_TOOL: mode})
    report = run(settings, fake, token, run_id="first")
    assert statuses(report)["google_doc"] == "failed"
    assert report.targets[0].result is not None
    assert report.targets[0].result.error_code == code
    assert fake.calls_to(DOC_APPEND_TOOL) == []

    retry = FakeMcpServer(token=token)
    assert statuses(run(settings, retry, token, run_id="second"))["google_doc"] == "delivered"


def test_crash_mid_call_leaves_a_blocking_pending_entry(settings: Settings, token: str) -> None:
    pulse = load_publishable_pulse(_pulse_dir(settings), allow_synthetic=True)
    DeliveryLedger(settings.data_dir / LEDGER_FILE).record(
        LedgerEntry.make(
            key=doc_key(pulse.iso_week, pulse.content_hash, DOC_ID),
            target=Target.GOOGLE_DOC,
            status=LedgerStatus.PENDING,
            iso_week=pulse.iso_week,
            content_hash=pulse.content_hash,
            run_id="crashed",
        )
    )
    fake = FakeMcpServer(token=token)
    report = run(settings, fake, token)
    assert statuses(report)["google_doc"] == "blocked_outcome_unknown"
    assert "crashed" in report.targets[0].detail
    assert fake.calls_to(DOC_APPEND_TOOL) == []


def test_new_recipients_get_a_new_draft_but_the_doc_is_not_reappended(
    tmp_path: Path, settings: Settings, token: str
) -> None:
    run(settings, FakeMcpServer(token=token), token, run_id="first")
    changed = make_settings(tmp_path, token, pulse_email_to="someone.else@example.com")
    fake = FakeMcpServer(token=token)
    report = run(changed, fake, token, run_id="second")
    assert statuses(report) == {
        "google_doc": "skipped_already_delivered",
        "gmail_draft": "delivered",
    }


def test_a_different_document_is_a_new_target(
    tmp_path: Path, settings: Settings, token: str
) -> None:
    run(settings, FakeMcpServer(token=token), token, run_id="first")
    other = make_settings(tmp_path, token, google_doc_id="otherTestDocument_" + "b" * 20)
    report = run(other, FakeMcpServer(token=token), token, run_id="second")
    assert statuses(report)["google_doc"] == "delivered"


def test_secrets_stay_out_of_logs_and_files(
    settings: Settings,
    token: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from groww_pulse.logging_setup import configure_logging

    configure_logging("DEBUG", "json", settings.secret_values())
    assert logging.getLogger("mcp").getEffectiveLevel() >= logging.WARNING
    fake = FakeMcpServer(token=token, behaviour={DRAFT_TOOL: "error:INVALID_INPUT"})
    with caplog.at_level(logging.DEBUG, logger="groww_pulse"):
        report = run(settings, fake, token)
    emitted = capsys.readouterr().err  # what the masking handler actually wrote
    assert "Publish finished" in emitted
    own_records = [
        f"{r.getMessage()} {r.__dict__}" for r in caplog.records if r.name.startswith("groww_pulse")
    ]
    outputs = [
        emitted,
        *own_records,
        json.dumps(report.as_dict()),
        (settings.data_dir / "runs" / "pub-1" / "delivery.json").read_text("utf-8"),
        (settings.data_dir / LEDGER_FILE).read_text("utf-8"),
    ]
    for text in outputs:
        assert token not in text
        assert DOC_ID not in text
        assert "admin@example.com" not in text  # from the server's error message
        assert not any(r in text for r in RECIPIENTS)


# ---------------------------------------------------------------- ledger
def test_keys_are_deterministic_and_target_specific() -> None:
    week, digest = "2026-W42", "f" * 64
    assert doc_key(week, digest, DOC_ID) == doc_key(week, digest, DOC_ID)
    assert doc_key(week, digest, DOC_ID) != doc_key("2026-W43", digest, DOC_ID)
    assert doc_key(week, digest, DOC_ID) != doc_key(week, "e" * 64, DOC_ID)
    assert doc_key(week, digest, DOC_ID) != doc_key(week, digest, DOC_ID + "x")
    assert draft_key(week, digest, ["B@example.com", "a@example.com"]) == draft_key(
        week, digest, [" a@example.com", "b@example.com"]
    )
    assert draft_key(week, digest, ["a@example.com"]) != draft_key(week, digest, ["b@example.com"])
    assert DOC_ID not in doc_key(week, digest, DOC_ID)


def test_corrupt_ledger_refuses_delivery(settings: Settings, token: str) -> None:
    path = settings.data_dir / LEDGER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json\n", encoding="utf-8")
    fake = FakeMcpServer(token=token)
    with pytest.raises(DeliveryError, match="corrupt"):
        run(settings, fake, token)
    assert fake.requests == []


# ---------------------------------------------------------------- one version per ISO week
BLOCKED_WEEK = "blocked_week_already_delivered"


def _ledger_bytes(settings: Settings) -> bytes:
    path = settings.data_dir / LEDGER_FILE
    return path.read_bytes() if path.exists() else b""


def _variant(settings: Settings) -> Path:
    """A regenerated pulse for the same week (different hash), now the newest."""
    return build_variant(_pulse_dir(settings), "report-run-v2")


def test_variant_is_the_same_week_with_a_different_hash(settings: Settings) -> None:
    original = load_publishable_pulse(_pulse_dir(settings), allow_synthetic=True)
    variant = load_publishable_pulse(_variant(settings), allow_synthetic=True)
    assert variant.iso_week == original.iso_week
    assert variant.content_hash != original.content_hash


@pytest.mark.parametrize("force", [False, True])
def test_new_version_of_a_delivered_week_is_blocked_even_when_forced(
    settings: Settings, token: str, force: bool
) -> None:
    first = run(settings, FakeMcpServer(token=token), token, run_id="first")
    assert statuses(first) == {"google_doc": "delivered", "gmail_draft": "delivered"}
    _variant(settings)
    before = _ledger_bytes(settings)

    fake = FakeMcpServer(token=token)
    report = run(settings, fake, token, run_id="second", force=force)  # newest = variant
    assert statuses(report) == {"google_doc": BLOCKED_WEEK, "gmail_draft": BLOCKED_WEEK}
    assert not report.ok
    assert fake.requests == []  # no handshake, no tool call
    assert _ledger_bytes(settings) == before  # history untouched; nothing recorded
    doc, draft = report.targets
    assert "already delivered with different content" in doc.detail
    assert "second Doc section" in doc.detail
    assert "second Gmail draft" in draft.detail
    assert "cannot be overridden" in doc.detail
    assert "run first" in doc.detail
    assert "--pulse" not in doc.detail  # the delivered version is complete: nothing to finish
    saved = json.loads((settings.data_dir / "runs" / "second" / "delivery.json").read_text())
    assert [t["status"] for t in saved["targets"]] == [BLOCKED_WEEK, BLOCKED_WEEK]


def test_dry_run_reports_the_week_block(settings: Settings, token: str) -> None:
    run(settings, FakeMcpServer(token=token), token, run_id="first")
    _variant(settings)
    fake = FakeMcpServer(token=token)
    report = run(settings, fake, token, run_id="dry", confirm=False)
    assert statuses(report) == {"google_doc": BLOCKED_WEEK, "gmail_draft": BLOCKED_WEEK}
    assert fake.requests == []


def test_exact_same_content_is_still_skipped_after_a_variant_exists(
    settings: Settings, token: str
) -> None:
    original = _pulse_dir(settings)
    run(settings, FakeMcpServer(token=token), token, run_id="first")
    _variant(settings)
    fake = FakeMcpServer(token=token)
    report = run(settings, fake, token, run_id="again", pulse_path=original)
    assert report.ok
    assert statuses(report) == {
        "google_doc": "skipped_already_delivered",
        "gmail_draft": "skipped_already_delivered",
    }
    assert fake.requests == []


def test_partly_delivered_week_can_be_finished_but_not_replaced(
    settings: Settings, token: str
) -> None:
    original = _pulse_dir(settings)
    fake = FakeMcpServer(token=token, behaviour={DRAFT_TOOL: "error:GMAIL_DRAFT_CREATE_FAILED"})
    partial = run(settings, fake, token, run_id="first")
    assert statuses(partial) == {"google_doc": "delivered", "gmail_draft": "failed"}
    _variant(settings)

    # The regenerated version is refused for both targets, and told how to recover.
    blocked = FakeMcpServer(token=token)
    report = run(settings, blocked, token, run_id="second")
    assert statuses(report) == {"google_doc": BLOCKED_WEEK, "gmail_draft": BLOCKED_WEEK}
    assert blocked.requests == []
    assert f"--pulse {original} --confirm" in report.targets[1].detail

    # The delivered version finishes: the Doc is skipped, only the draft is created.
    finish = FakeMcpServer(token=token)
    report = run(settings, finish, token, run_id="third", pulse_path=original)
    assert report.ok
    assert statuses(report) == {
        "google_doc": "skipped_already_delivered",
        "gmail_draft": "delivered",
    }
    assert finish.calls_to(DOC_APPEND_TOOL) == []
    assert len(finish.calls_to(DRAFT_TOOL)) == 1

    # The week is now complete, and the variant stays blocked.
    after = FakeMcpServer(token=token)
    report = run(settings, after, token, run_id="fourth", force=True)
    assert statuses(report) == {"google_doc": BLOCKED_WEEK, "gmail_draft": BLOCKED_WEEK}
    assert "--pulse" not in report.targets[0].detail
    assert after.requests == []


def test_partly_delivered_version_with_missing_files_is_explained(
    settings: Settings, token: str
) -> None:
    original = _pulse_dir(settings)
    fake = FakeMcpServer(token=token, behaviour={DRAFT_TOOL: "error:GMAIL_DRAFT_CREATE_FAILED"})
    run(settings, fake, token, run_id="first")
    _variant(settings)
    shutil.rmtree(original)
    report = run(settings, FakeMcpServer(token=token), token, run_id="second")
    assert statuses(report) == {"google_doc": BLOCKED_WEEK, "gmail_draft": BLOCKED_WEEK}
    assert "no longer under DATA_DIR/runs" in report.targets[0].detail


def test_uncertain_outcome_of_another_version_blocks_until_forced(
    settings: Settings, token: str
) -> None:
    fake = FakeMcpServer(token=token, behaviour={DOC_APPEND_TOOL: "read_error_after_write"})
    first = run(settings, fake, token, run_id="first")
    assert statuses(first) == {"google_doc": "outcome_unknown", "gmail_draft": "not_attempted"}
    _variant(settings)

    blocked = FakeMcpServer(token=token)
    report = run(settings, blocked, token, run_id="second")
    assert statuses(report) == {"google_doc": BLOCKED_WEEK, "gmail_draft": BLOCKED_WEEK}
    assert "may have been written" in report.targets[0].detail
    assert blocked.requests == []

    # The operator checked the Doc: nothing was written, so --force (CLI only) proceeds.
    forced = FakeMcpServer(token=token)
    report = run(settings, forced, token, run_id="third", force=True)
    assert statuses(report) == {"google_doc": "delivered", "gmail_draft": "delivered"}


def test_failed_attempts_of_another_version_do_not_lock_the_week(
    settings: Settings, token: str
) -> None:
    fake = FakeMcpServer(
        token=token,
        behaviour={
            DOC_APPEND_TOOL: "error:GOOGLE_AUTH_REQUIRED",
            DRAFT_TOOL: "error:GMAIL_DRAFT_CREATE_FAILED",
        },
    )
    first = run(settings, fake, token, run_id="first")
    assert statuses(first) == {"google_doc": "failed", "gmail_draft": "failed"}
    _variant(settings)
    report = run(settings, FakeMcpServer(token=token), token, run_id="second")
    assert statuses(report) == {"google_doc": "delivered", "gmail_draft": "delivered"}


def test_legacy_ledger_with_two_versions_writes_nothing_more(
    settings: Settings, token: str
) -> None:
    """Before the week lock, version A reached the Doc and version B the draft."""
    original = _pulse_dir(settings)
    a = load_publishable_pulse(original, allow_synthetic=True)
    b = load_publishable_pulse(_variant(settings), allow_synthetic=True)
    book = DeliveryLedger(settings.data_dir / LEDGER_FILE)
    for target, pulse, key in (
        (Target.GOOGLE_DOC, a, doc_key(a.iso_week, a.content_hash, DOC_ID)),
        (Target.GMAIL_DRAFT, b, draft_key(b.iso_week, b.content_hash, RECIPIENTS)),
    ):
        book.record(
            LedgerEntry.make(
                key=key, target=target, status=LedgerStatus.SUCCEEDED, iso_week=pulse.iso_week,
                content_hash=pulse.content_hash, run_id="legacy",
            )
        )  # fmt: skip

    fake = FakeMcpServer(token=token)
    report_a = run(settings, fake, token, run_id="a", pulse_path=original)
    assert statuses(report_a) == {
        "google_doc": "skipped_already_delivered",
        "gmail_draft": BLOCKED_WEEK,
    }
    report_b = run(settings, fake, token, run_id="b", force=True)
    # --force may only re-deliver a version to a target it already reached (ordinary
    # same-content semantics); it never reaches the target the other version holds.
    assert statuses(report_b)["google_doc"] == BLOCKED_WEEK
    assert fake.calls_to(DOC_APPEND_TOOL) == []


# ---------------------------------------------------------------- week lock (pure)
def _entry(target: Target, status: LedgerStatus, content_hash: str) -> LedgerEntry:
    return LedgerEntry.make(
        key=f"{target.value}|{content_hash}",
        target=target,
        status=status,
        iso_week="2026-W42",
        content_hash=content_hash,
        run_id=f"run-{content_hash[:4]}",
    )


def test_week_lock_rules() -> None:
    a, b = "a" * 64, "b" * 64
    doc_ok = _entry(Target.GOOGLE_DOC, LedgerStatus.SUCCEEDED, a)

    lock = week_lock([doc_ok], "2026-W42", b)
    # Rule 1: another version reached this target.
    assert lock.reason(Target.GOOGLE_DOC, force=True) is not None
    # Rule 2: another version reached the week and this one reached nothing.
    assert lock.reason(Target.GMAIL_DRAFT, force=True) is not None
    # The delivered version itself is free to finish.
    own = week_lock([doc_ok], "2026-W42", a)
    assert own.reason(Target.GMAIL_DRAFT) is None
    assert own.own_succeeded == {"google_doc"}
    # Another week is unaffected.
    assert week_lock([doc_ok], "2026-W43", b).reason(Target.GOOGLE_DOC) is None


def test_week_lock_remembers_a_success_that_a_forced_retry_later_failed() -> None:
    a = "a" * 64
    entries = [
        _entry(Target.GOOGLE_DOC, LedgerStatus.SUCCEEDED, a),
        _entry(Target.GOOGLE_DOC, LedgerStatus.PENDING, a),
        _entry(Target.GOOGLE_DOC, LedgerStatus.FAILED, a),
    ]
    lock = week_lock(entries, "2026-W42", "b" * 64)
    assert [e.status for e in lock.delivered] == ["succeeded"]
    assert lock.uncertain == ()


def test_week_lock_uncertain_entries_yield_to_force_and_to_later_outcomes() -> None:
    a, b = "a" * 64, "b" * 64
    pending = _entry(Target.GOOGLE_DOC, LedgerStatus.PENDING, a)
    lock = week_lock([pending], "2026-W42", b)
    assert lock.reason(Target.GOOGLE_DOC) is not None
    assert lock.reason(Target.GOOGLE_DOC, force=True) is None
    failed = _entry(Target.GOOGLE_DOC, LedgerStatus.FAILED, a)
    assert week_lock([pending, failed], "2026-W42", b).reason(Target.GOOGLE_DOC) is None
