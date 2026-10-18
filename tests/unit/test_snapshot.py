# SYNTHETIC TEST FIXTURE — not real review data (except the committed-snapshot checks)
"""Read-only dashboard snapshot: exporter, the committed real snapshot, and UI fallback.

Exporter tests use a pulse built by the real Phases 2-4 from labelled fixtures, so they opt
in with ``allow_synthetic=True`` (the CLI never does). The ``committed`` tests check the
real ``snapshot/`` in the repository with every publish check and no opt-in.
"""

from __future__ import annotations

import json
import re
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
from groww_pulse.delivery.publisher import load_publishable_pulse
from groww_pulse.errors import DeliveryError
from groww_pulse.snapshot import (
    BLOCKED_WEEK,
    DELIVERED,
    MANIFEST_FILE,
    NO_LEDGER,
    NOT_DELIVERED,
    export_snapshot,
    read_manifest,
)
from tests.fixtures.report import build_pulse, build_variant

COMMITTED = Path(__file__).parents[2] / "snapshot"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


@pytest.fixture(scope="module")
def built_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data = tmp_path_factory.mktemp("snap") / "data"
    build_pulse(load_settings(env_file=None, data_dir=data, log_level="WARNING"))
    return data


@pytest.fixture
def settings(tmp_path: Path, built_data: Path) -> Settings:
    shutil.copytree(built_data, tmp_path / "data")
    return load_settings(env_file=None, data_dir=tmp_path / "data", log_level="WARNING")


def _pulse_dir(settings: Settings) -> Path:
    return next((settings.data_dir / "runs").glob("*/pulse.json")).parent


def _export(settings: Settings, out: Path, **kw: Any) -> Any:
    return export_snapshot(settings.data_dir, out, allow_synthetic=True, **kw)


def _all_text(directory: Path) -> str:
    return "\n".join(p.read_text("utf-8") for p in directory.rglob("*") if p.is_file())


# ---------------------------------------------------------------- exporter
def test_export_writes_a_verifiable_snapshot_without_review_ids(
    settings: Settings, tmp_path: Path
) -> None:
    source = load_publishable_pulse(_pulse_dir(settings), allow_synthetic=True)
    document = json.loads((_pulse_dir(settings) / "pulse.json").read_text("utf-8"))
    review_ids = {rid for t in document["pulse"]["themes"] for rid in t["review_ids"]}
    review_ids |= {q["review_id"] for q in document["pulse"]["quotes"]}
    assert review_ids

    result = _export(settings, tmp_path / "snapshot")

    copy = load_publishable_pulse(result.pulse_dir, allow_synthetic=True)  # every check
    assert (copy.iso_week, copy.content_hash, copy.full_text) == (
        source.iso_week, source.content_hash, source.full_text,
    )  # fmt: skip
    for name in ("pulse.txt", "pulse.md"):
        assert (result.pulse_dir / name).read_bytes() == (_pulse_dir(settings) / name).read_bytes()
    text = _all_text(tmp_path / "snapshot")
    assert not any(rid in text for rid in review_ids)
    snap = json.loads((result.pulse_dir / "pulse.json").read_text("utf-8"))["pulse"]
    assert [q["review_id"] for q in snap["quotes"]] == ["withheld-1", "withheld-2", "withheld-3"]
    assert all(t["review_ids"] == [] for t in snap["themes"])
    # Everything shown or validated is unchanged.
    for key in ("themes", "quotes"):
        for kept, original in zip(snap[key], document["pulse"][key], strict=True):
            assert {k: v for k, v in kept.items() if k not in ("review_id", "review_ids")} == {
                k: v for k, v in original.items() if k not in ("review_id", "review_ids")
            }
    assert snap["actions"] == document["pulse"]["actions"]
    assert result.manifest["delivery"] == {"google_doc": NO_LEDGER, "gmail_draft": NO_LEDGER}
    assert read_manifest(tmp_path / "snapshot") == result.manifest


def test_export_keeps_only_aggregate_statistics(settings: Settings, tmp_path: Path) -> None:
    result = _export(settings, tmp_path / "snapshot")
    runs = tmp_path / "snapshot" / "runs"
    funnel = json.loads(
        (runs / result.manifest["preprocess_run_id"] / "funnel.json").read_text("utf-8")
    )
    assert "settings" not in funnel
    assert "raw_path" not in funnel
    assert funnel["input"] > 0
    assert funnel["steps"]
    analysis = json.loads(
        (runs / result.manifest["analysis_run_id"] / "analysis.json").read_text("utf-8")
    )
    assert set(analysis["analysis"]) == {"method", "input"}
    assert "llm_usage" not in analysis


def test_export_summarises_delivery_without_ledger_identifiers(
    settings: Settings, tmp_path: Path
) -> None:
    pulse = load_publishable_pulse(_pulse_dir(settings), allow_synthetic=True)
    ledger = DeliveryLedger(settings.data_dir / LEDGER_FILE)
    ledger.record(
        LedgerEntry.make(
            key="secret-ledger-key", target=Target.GOOGLE_DOC, status=LedgerStatus.SUCCEEDED,
            iso_week=pulse.iso_week, content_hash=pulse.content_hash, run_id="delivery-run-x",
        )
    )  # fmt: skip
    result = _export(settings, tmp_path / "a")
    assert result.manifest["delivery"] == {"google_doc": DELIVERED, "gmail_draft": NOT_DELIVERED}

    variant = build_variant(_pulse_dir(settings), "report-run-v2")
    result = _export(settings, tmp_path / "b", pulse_path=variant)
    assert result.manifest["delivery"] == {"google_doc": BLOCKED_WEEK, "gmail_draft": BLOCKED_WEEK}
    text = _all_text(tmp_path / "b")
    assert "secret-ledger-key" not in text
    assert "delivery-run-x" not in text


def test_export_refuses_synthetic_pulses_without_the_test_opt_in(
    settings: Settings, tmp_path: Path
) -> None:
    with pytest.raises(DeliveryError, match="synthetic"):
        export_snapshot(settings.data_dir, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()


def test_export_refuses_a_pulse_that_fails_verification(settings: Settings, tmp_path: Path) -> None:
    path = _pulse_dir(settings) / "pulse.txt"
    path.write_text(path.read_text("utf-8").replace("reviews", "posts", 1), encoding="utf-8")
    with pytest.raises(DeliveryError, match="not publishable"):
        _export(settings, tmp_path / "snapshot")


def test_re_export_replaces_the_previous_snapshot(settings: Settings, tmp_path: Path) -> None:
    out = tmp_path / "snapshot"
    first = _export(settings, out)
    variant = build_variant(_pulse_dir(settings), "report-run-v2")
    second = _export(settings, out, pulse_path=variant)
    assert not first.pulse_dir.exists()
    assert len(list((out / "runs").glob("*/pulse.json"))) == 1
    assert read_manifest(out) == second.manifest


# ---------------------------------------------------------------- dashboard fallback
def test_dashboard_falls_back_to_the_snapshot_only_without_local_data(
    settings: Settings, tmp_path: Path
) -> None:
    out = tmp_path / "snapshot"
    _export(settings, out)

    local = dashboard.load_dashboard(settings, allow_synthetic=True, snapshot_dir=out)
    assert local.snapshot is None  # DATA_DIR wins when it has a verified pulse

    empty = load_settings(env_file=None, data_dir=tmp_path / "empty", log_level="WARNING")
    board = dashboard.load_dashboard(empty, allow_synthetic=True, snapshot_dir=out)
    assert board.snapshot is not None
    assert board.pulse is not None
    assert board.funnel is not None
    assert board.analysis is not None
    assert board.problems == []
    assert board.delivery == {}  # no ledger is read from a snapshot
    blockers = dashboard.live_delivery_blockers(empty, board)
    assert any("read-only snapshot" in b for b in blockers)


def test_tampered_snapshot_is_rejected(settings: Settings, tmp_path: Path) -> None:
    out = tmp_path / "snapshot"
    result = _export(settings, out)
    path = result.pulse_dir / "pulse.json"
    data = json.loads(path.read_text("utf-8"))
    data["pulse"]["n_analyzed"] = 999
    path.write_text(json.dumps(data), encoding="utf-8")
    empty = load_settings(env_file=None, data_dir=tmp_path / "empty", log_level="WARNING")
    board = dashboard.load_dashboard(empty, allow_synthetic=True, snapshot_dir=out)
    assert board.pulse is None
    assert board.snapshot is None
    assert any("does not re-render" in r.reason for r in board.rejected)


def test_directory_without_a_manifest_is_not_a_snapshot(tmp_path: Path) -> None:
    (tmp_path / "snapshot").mkdir()
    assert read_manifest(tmp_path / "snapshot") is None
    (tmp_path / "snapshot" / MANIFEST_FILE).write_text('{"kind": "other"}', encoding="utf-8")
    assert read_manifest(tmp_path / "snapshot") is None


# ---------------------------------------------------------------- the committed snapshot
@pytest.mark.skipif(not (COMMITTED / MANIFEST_FILE).exists(), reason="no committed snapshot")
class TestCommittedSnapshot:
    def test_it_is_a_real_pulse_that_passes_every_publish_check(self) -> None:
        manifest = read_manifest(COMMITTED)
        assert manifest is not None
        pulse = load_publishable_pulse(COMMITTED / "runs" / manifest["pulse_run_id"])
        assert pulse.synthetic is False  # no opt-in: real, publishable data only
        assert (pulse.iso_week, pulse.content_hash) == (
            manifest["iso_week"], manifest["content_hash"],
        )  # fmt: skip

    def test_it_holds_no_identifiers_or_contact_details(self) -> None:
        text = _all_text(COMMITTED)
        assert not UUID.search(text)
        assert not EMAIL.search(text)
        assert "ledger" not in {p.stem for p in COMMITTED.rglob("*")}
        assert not re.search(r"data[\\/]+|[A-Z]:\\\\", text)
        files = {p.name for p in COMMITTED.rglob("*") if p.is_file()}
        assert files <= {MANIFEST_FILE, "pulse.json", "pulse.txt", "pulse.md", "funnel.json",
                         "analysis.json"}  # fmt: skip

    def test_the_hosted_dashboard_shows_it_read_only(self, tmp_path: Path) -> None:
        empty = load_settings(env_file=None, data_dir=tmp_path / "data", log_level="WARNING")
        board = dashboard.load_dashboard(empty, snapshot_dir=COMMITTED)
        assert board.pulse is not None
        assert board.snapshot is not None
        assert board.funnel is not None
        assert board.analysis is not None
        assert len(board.pulse.pulse.themes) == len(board.pulse.pulse.quotes) == 3
        assert any(
            "read-only snapshot" in b for b in dashboard.live_delivery_blockers(empty, board)
        )
