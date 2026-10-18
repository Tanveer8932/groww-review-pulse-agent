# SYNTHETIC TEST FIXTURE — not real review data
"""Phase 6 dashboard read model on a pulse built by the real Phases 2-4 from fixtures.

The pulse is synthetic (``publishable: false``), so tests opt in with
``allow_synthetic=True``; the app never does. No test touches the network.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from groww_pulse import dashboard
from groww_pulse.config import Settings, load_settings
from groww_pulse.delivery.ledger import (
    LEDGER_FILE,
    DeliveryLedger,
    LedgerEntry,
    LedgerStatus,
    Target,
)
from groww_pulse.errors import DeliveryError
from tests.fixtures.report import build_pulse, build_variant

DOC_ID = "fakeTestDocument_" + "a" * 20


@pytest.fixture(scope="module")
def built_pulse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data = tmp_path_factory.mktemp("pulse") / "data"
    build_pulse(load_settings(env_file=None, data_dir=data, log_level="WARNING"))
    return data


def make_settings(data_dir: Path, **overrides: Any) -> Settings:
    return load_settings(env_file=None, data_dir=data_dir, log_level="WARNING", **overrides)


@pytest.fixture
def settings(tmp_path: Path, built_pulse: Path) -> Settings:
    data = tmp_path / "data"
    shutil.copytree(built_pulse, data)
    return make_settings(data)


def _pulse_dir(settings: Settings) -> Path:
    return next((settings.data_dir / "runs").glob("*/pulse.json")).parent


def _record(settings: Settings, target: Target, status: LedgerStatus, content_hash: str) -> None:
    DeliveryLedger(settings.data_dir / LEDGER_FILE).record(
        LedgerEntry.make(
            key=f"{target.value}-{content_hash}",
            target=target,
            status=status,
            iso_week=_load(settings).verified.iso_week,
            content_hash=content_hash,
            run_id="delivery-run",
        )
    )


def _load(settings: Settings) -> dashboard.DashboardPulse:
    view = dashboard.load_dashboard(settings, allow_synthetic=True).pulse
    assert view is not None
    return view


# ---------------------------------------------------------------- loading
def test_empty_data_dir_has_no_pulse(tmp_path: Path) -> None:
    board = dashboard.load_dashboard(make_settings(tmp_path / "data"))
    assert board.pulse is None
    assert board.rejected == []
    assert not board.validators_passed


def test_synthetic_pulse_is_never_shown_by_default(settings: Settings) -> None:
    board = dashboard.load_dashboard(settings)
    assert board.pulse is None
    assert len(board.rejected) == 1
    assert "synthetic" in board.rejected[0].reason


def test_verified_pulse_with_statistics(settings: Settings) -> None:
    board = dashboard.load_dashboard(settings, allow_synthetic=True)
    view = board.pulse
    assert view is not None
    assert view.synthetic
    assert len(view.pulse.themes) == len(view.pulse.quotes) == len(view.pulse.actions) == 3
    assert board.validators_passed
    assert {v["name"] for v in view.validators} >= {"word_limit", "quotes_verbatim", "no_pii"}
    assert view.pulse.word_count <= 250
    assert board.funnel is not None
    assert board.funnel.output == view.pulse.n_analyzed
    assert board.funnel.steps[0]["step"] == "validate"
    assert board.analysis is not None
    assert board.analysis.ranking_formula
    assert board.problems == []
    assert board.delivery == {"google_doc": None, "gmail_draft": None}


def test_tampered_newer_pulse_is_rejected_and_the_older_one_shown(settings: Settings) -> None:
    original = _pulse_dir(settings)
    newer = original.parent / "zz-newer-run"
    shutil.copytree(original, newer)
    document = json.loads((newer / "pulse.json").read_text("utf-8"))
    document["created_at"] = "2999-01-01T00:00:00+00:00"
    (newer / "pulse.json").write_text(json.dumps(document), encoding="utf-8")
    (newer / "pulse.txt").write_text("edited", encoding="utf-8")

    board = dashboard.load_dashboard(settings, allow_synthetic=True)
    assert board.pulse is not None
    assert board.pulse.directory == original
    assert [r.run_id for r in board.rejected] == ["zz-newer-run"]
    assert "pulse.txt is missing or differs" in board.rejected[0].reason


def test_unreadable_pulse_json_is_rejected(settings: Settings) -> None:
    shutil.rmtree(_pulse_dir(settings))
    broken = settings.data_dir / "runs" / "broken-run"
    broken.mkdir()
    (broken / "pulse.json").write_text("{not json", encoding="utf-8")
    board = dashboard.load_dashboard(settings, allow_synthetic=True)
    assert board.pulse is None
    assert [r.run_id for r in board.rejected] == ["broken-run"]
    assert "not a valid Phase 4 pulse" in board.rejected[0].reason


def test_missing_funnel_and_analysis_are_reported(settings: Settings) -> None:
    for name in ("funnel.json", "analysis.json"):
        for path in (settings.data_dir / "runs").glob(f"*/{name}"):
            path.unlink()
    board = dashboard.load_dashboard(settings, allow_synthetic=True)
    assert board.pulse is not None
    assert board.funnel is None
    assert board.analysis is None
    assert len(board.problems) == 2


# ---------------------------------------------------------------- delivery status
def test_ledger_status_for_this_pulse_and_other_versions(settings: Settings) -> None:
    content_hash = _load(settings).verified.content_hash
    _record(settings, Target.GOOGLE_DOC, LedgerStatus.PENDING, content_hash)
    _record(settings, Target.GOOGLE_DOC, LedgerStatus.SUCCEEDED, content_hash)
    _record(settings, Target.GMAIL_DRAFT, LedgerStatus.SUCCEEDED, "an-earlier-version")
    _record(settings, Target.GMAIL_DRAFT, LedgerStatus.FAILED, "a-failed-version")

    board = dashboard.load_dashboard(settings, allow_synthetic=True)
    doc = board.delivery["google_doc"]
    assert doc is not None
    assert doc.status == "succeeded"
    assert board.delivery["gmail_draft"] is None
    assert [(r.target, r.status) for r in board.other_versions_delivered] == [
        ("gmail_draft", "succeeded")
    ]


def test_corrupt_ledger_is_reported_and_blocks_delivery(settings: Settings) -> None:
    ledger = settings.data_dir / LEDGER_FILE
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("{corrupt\n", encoding="utf-8")
    board = dashboard.load_dashboard(settings, allow_synthetic=True)
    assert board.ledger_error is not None
    assert "ledger cannot be read" in " ".join(dashboard.live_delivery_blockers(settings, board))


# ---------------------------------------------------------------- delivery gate
def test_blockers_in_the_default_dry_run_configuration(settings: Settings) -> None:
    board = dashboard.load_dashboard(settings, allow_synthetic=True)
    blockers = " | ".join(dashboard.live_delivery_blockers(settings, board))
    assert "synthetic" in blockers
    assert "DELIVERY_MODE" in blockers
    assert "MCP_SERVER_URL" in blockers
    assert "MCP_AUTH_TOKEN" in blockers


def test_only_the_synthetic_blocker_remains_when_fully_configured(settings: Settings) -> None:
    configured = make_settings(
        settings.data_dir,
        delivery_mode="confirm",
        mcp_server_url="https://mcp.example.test/",
        mcp_auth_token="fake-token-for-tests",
        google_doc_id=DOC_ID,
        pulse_email_to="pm@example.com",
    )
    board = dashboard.load_dashboard(configured, allow_synthetic=True)
    assert dashboard.live_delivery_blockers(configured, board) == [
        "the pulse was built from synthetic test fixtures"
    ]


def test_no_pulse_blocks_delivery(tmp_path: Path) -> None:
    s = make_settings(tmp_path / "data")
    assert "there is no validated pulse" in dashboard.live_delivery_blockers(
        s, dashboard.load_dashboard(s)
    )


def test_preview_and_deliver_refuse_a_synthetic_pulse(settings: Settings) -> None:
    pulse_dir = _pulse_dir(settings)
    with pytest.raises(DeliveryError, match="synthetic"):
        dashboard.preview_delivery(settings, pulse_dir, run_id="preview-run")
    with pytest.raises(DeliveryError, match="synthetic"):
        dashboard.deliver(settings, pulse_dir, run_id="deliver-run")
    assert not (settings.data_dir / LEDGER_FILE).exists()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-17T06:25:58.477896+00:00", "2026-10-17 06:25 UTC"),
        ("2026-10-17T11:55:58+05:30", "2026-10-17 06:25 UTC"),
        ("2026-10-17T06:25:58", "2026-10-17 06:25"),
        ("not a time", "not a time"),
    ],
)
def test_format_timestamp(value: str, expected: str) -> None:
    assert dashboard.format_timestamp(value) == expected


# ---------------------------------------------------------------- one version per ISO week
WEEK_BLOCKER = "duplicate-week protection"


def _variant(settings: Settings) -> Path:
    return build_variant(_pulse_dir(settings), "report-run-v2")


def _configured(settings: Settings) -> Settings:
    return make_settings(
        settings.data_dir,
        delivery_mode="confirm",
        mcp_server_url="https://mcp.example.test/",
        mcp_auth_token="fake-token-for-tests",
        google_doc_id=DOC_ID,
        pulse_email_to="pm@example.com",
    )


def test_same_week_different_hash_blocks_the_newest_pulse(settings: Settings) -> None:
    original = _load(settings).verified
    _record(settings, Target.GOOGLE_DOC, LedgerStatus.SUCCEEDED, original.content_hash)
    _record(settings, Target.GMAIL_DRAFT, LedgerStatus.SUCCEEDED, original.content_hash)
    _variant(settings)

    configured = _configured(settings)
    board = dashboard.load_dashboard(configured, allow_synthetic=True)
    assert board.pulse is not None
    assert board.pulse.verified.content_hash != original.content_hash
    assert set(board.pulse.week_blocks()) == {"google_doc", "gmail_draft"}
    blockers = dashboard.live_delivery_blockers(configured, board)
    assert any(WEEK_BLOCKER in b for b in blockers)
    assert board.recovery is None  # the delivered version is complete
    assert board.recovery_problem is None


def test_partial_delivery_offers_the_delivered_version_for_completion(
    settings: Settings,
) -> None:
    original = _load(settings)
    _record(settings, Target.GOOGLE_DOC, LedgerStatus.SUCCEEDED, original.verified.content_hash)
    _record(settings, Target.GMAIL_DRAFT, LedgerStatus.FAILED, original.verified.content_hash)
    _variant(settings)

    configured = _configured(settings)
    board = dashboard.load_dashboard(configured, allow_synthetic=True)
    assert board.pulse is not None
    assert board.pulse.week_blocks()
    recovery = board.recovery
    assert recovery is not None
    assert recovery.directory == original.directory
    assert recovery.week_blocks() == {}  # the delivered version may finish
    blockers = dashboard.live_delivery_blockers(configured, board, recovery)
    assert blockers == ["the pulse was built from synthetic test fixtures"]


def test_partial_delivery_with_missing_pulse_files_is_explained(settings: Settings) -> None:
    original = _load(settings)
    _record(settings, Target.GOOGLE_DOC, LedgerStatus.SUCCEEDED, original.verified.content_hash)
    _variant(settings)
    shutil.rmtree(original.directory)
    board = dashboard.load_dashboard(settings, allow_synthetic=True)
    assert board.recovery is None
    assert board.recovery_problem is not None
    assert "no longer under DATA_DIR/runs" in board.recovery_problem


def test_existing_delivery_of_this_pulse_is_not_a_week_block(settings: Settings) -> None:
    content_hash = _load(settings).verified.content_hash
    _record(settings, Target.GOOGLE_DOC, LedgerStatus.SUCCEEDED, content_hash)
    board = dashboard.load_dashboard(settings, allow_synthetic=True)
    assert board.pulse is not None
    assert board.pulse.week_blocks() == {}
    blockers = " | ".join(dashboard.live_delivery_blockers(settings, board))
    assert WEEK_BLOCKER not in blockers
