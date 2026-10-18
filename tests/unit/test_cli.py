# SYNTHETIC TEST FIXTURE — not real review data
from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from groww_pulse.cli import app
from groww_pulse.preprocessing.pipeline import run_preprocessing
from tests.fixtures.synthetic import AS_OF, play_row, write_play_csv

runner = CliRunner()


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    return tmp_path


def _json_from(output: str) -> dict[str, object]:
    return json.loads(output[output.index("{") : output.rindex("}") + 1])


def test_import_command_success_and_reimport(workdir: Path) -> None:
    src = write_play_csv(workdir / "reviews.csv", [play_row("gp:CLI-1"), play_row("gp:CLI-2")])
    result = runner.invoke(app, ["import", str(src), "--as-of", AS_OF.isoformat()])
    assert result.exit_code == 0, result.output
    summary = _json_from(result.stdout)
    assert summary["store"]["added"] == 2  # type: ignore[index]
    assert summary["window"]["start"] == "2026-07-23T00:00:00+00:00"  # type: ignore[index]
    assert (workdir / "data" / "raw" / "reviews_raw.jsonl").exists()

    again = runner.invoke(app, ["import", str(src), "--as-of", AS_OF.isoformat()])
    assert _json_from(again.stdout)["store"]["added"] == 0  # type: ignore[index]


def test_import_directory_and_weeks(workdir: Path) -> None:
    (workdir / "imports").mkdir()
    write_play_csv(workdir / "imports" / "a.csv", [play_row("gp:D-1")])
    result = runner.invoke(
        app, ["import", str(workdir / "imports"), "--as-of", AS_OF.isoformat(), "--weeks", "8"]
    )
    assert result.exit_code == 0, result.output
    assert _json_from(result.stdout)["window"]["start"] == "2026-08-20T00:00:00+00:00"  # type: ignore[index]


def test_import_partial_failure_exit_code(workdir: Path) -> None:
    good = write_play_csv(workdir / "good.csv", [play_row("gp:OK")])
    bad = workdir / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = runner.invoke(app, ["import", str(good), str(bad), "--as-of", AS_OF.isoformat()])
    assert result.exit_code == 1
    assert _json_from(result.stdout)["ok"] is False


@pytest.mark.parametrize(
    "args",
    [
        ["--as-of", "14/10/2026"],
        ["--weeks", "13"],
        ["--weeks", "7"],
    ],
)
def test_import_usage_errors(workdir: Path, args: list[str]) -> None:
    src = write_play_csv(workdir / "r.csv", [play_row("gp:X")])
    result = runner.invoke(app, ["import", str(src), *args])
    assert result.exit_code == 2


def test_import_bad_column_map(workdir: Path) -> None:
    src = write_play_csv(workdir / "r.csv", [play_row("gp:X")])
    bad_map = workdir / "map.yaml"
    bad_map.write_text("reviewer_name: Name\n", encoding="utf-8")
    result = runner.invoke(app, ["import", str(src), "--map", str(bad_map)])
    assert result.exit_code == 2
    assert "unknown fields" in result.output


def test_import_empty_directory(workdir: Path) -> None:
    (workdir / "empty").mkdir()
    result = runner.invoke(app, ["import", str(workdir / "empty")])
    assert result.exit_code == 2


def test_import_invalid_config(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANALYSIS_WINDOW_WEEKS", "20")
    src = write_play_csv(workdir / "r.csv", [play_row("gp:X")])
    result = runner.invoke(app, ["import", str(src)])
    assert result.exit_code == 2
    assert "ANALYSIS_WINDOW_WEEKS" in result.output


@pytest.mark.parametrize("flag", ["true", "false"])
def test_collect_is_disabled(workdir: Path, monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    monkeypatch.setenv("ENABLE_PLAY_COLLECTOR", flag)
    result = runner.invoke(app, ["collect"])
    assert result.exit_code == 2
    assert "disabled" in result.output
    assert not (workdir / "data").exists()


# ---------------------------------------------------------------- preprocess (Phase 2)


def test_preprocess_empty_store(workdir: Path) -> None:
    result = runner.invoke(app, ["preprocess", "--as-of", AS_OF.isoformat()])
    assert result.exit_code == 0, result.output
    summary = _json_from(result.stdout)
    assert summary["output"] == 0
    assert summary["empty"] is True
    assert "raw store not found" in result.stderr
    assert (workdir / "data" / "reviews.jsonl").read_bytes() == b""


def test_preprocess_refuses_synthetic_raw_store(workdir: Path) -> None:
    src = write_play_csv(workdir / "reviews.csv", [play_row("gp:CLI-1")])
    assert runner.invoke(app, ["import", str(src), "--as-of", AS_OF.isoformat()]).exit_code == 0
    result = runner.invoke(app, ["preprocess", "--as-of", AS_OF.isoformat()])
    assert result.exit_code == 1
    assert "synthetic test fixture" in result.stderr
    assert not (workdir / "data" / "reviews.jsonl").exists()


def test_preprocess_after_import(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI never allows synthetic data; this test opts in by wrapping the pipeline.
    monkeypatch.setattr(
        "groww_pulse.cli.run_preprocessing",
        functools.partial(run_preprocessing, allow_synthetic=True),
    )
    rows = [
        play_row("gp:CLI-1", text="SYNTHETIC FIXTURE the app crashes when I open my portfolio"),
        play_row("gp:CLI-2", text="SYNTHETIC FIXTURE bad app"),
    ]
    src = write_play_csv(workdir / "reviews.csv", rows)
    assert runner.invoke(app, ["import", str(src), "--as-of", AS_OF.isoformat()]).exit_code == 0
    result = runner.invoke(app, ["preprocess", "--as-of", AS_OF.isoformat(), "--weeks", "8"])
    assert result.exit_code == 0, result.output
    summary = _json_from(result.stdout)
    assert summary["input"] == 2
    assert summary["output"] == 1
    assert summary["settings"]["window_weeks"] == 8
    assert Path(summary["funnel_path"]).exists()


@pytest.mark.parametrize("args", [["--as-of", "2026/10/14"], ["--weeks", "13"]])
def test_preprocess_usage_errors(workdir: Path, args: list[str]) -> None:
    assert runner.invoke(app, ["preprocess", *args]).exit_code == 2


# ---------------------------------------------------------------- analyze (Phase 3)


def test_analyze_requires_llm_configuration(workdir: Path) -> None:
    result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 2
    assert "ANTHROPIC_API_KEY" in result.stderr


def test_analyze_without_reviews_fails_cleanly(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-key-for-cli-test")
    result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 1
    assert "Analysis failed" in result.stderr
    assert "preprocess" in result.stderr
    assert "sk-ant-fake" not in result.output


def test_analyze_empty_dataset(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-key-for-cli-test")
    assert runner.invoke(app, ["preprocess", "--as-of", AS_OF.isoformat()]).exit_code == 0
    result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 1
    assert "no reviews" in result.stderr
    assert not list((workdir / "data" / "runs").glob("*/analysis.json"))


def test_analyze_success(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from groww_pulse.analysis.pipeline import run_analysis
    from groww_pulse.config import load_settings
    from tests.fixtures.analysis import build_corpus, keyword_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-key-for-cli-test")
    build_corpus(load_settings(env_file=None, data_dir=workdir / "data"))
    # The CLI never allows synthetic data or a fake LLM; this test opts in by wrapping.
    monkeypatch.setattr(
        "groww_pulse.cli.run_analysis",
        functools.partial(run_analysis, provider=keyword_provider(), allow_synthetic=True),
    )
    result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 0, result.output
    summary = _json_from(result.stdout)
    assert summary["quotes"] == 3
    assert summary["actions"] == 3
    assert len(summary["top_theme_ids"]) == 3  # type: ignore[arg-type]
    assert Path(str(summary["output_path"])).exists()


# ---------------------------------------------------------------- report (Phase 4)
def test_report_without_analysis_fails_cleanly(workdir: Path) -> None:
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 1
    assert "no analysis.json found" in result.stderr
    assert not list((workdir / "data").rglob("pulse.*"))


def test_report_refuses_synthetic_analysis(workdir: Path) -> None:
    from groww_pulse.config import load_settings
    from tests.fixtures.report import build_analysis

    build_analysis(load_settings(env_file=None, data_dir=workdir / "data"))
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 1
    assert "synthetic test fixtures" in result.stderr
    assert not list((workdir / "data").rglob("pulse.*"))


def test_report_with_missing_analysis_path_is_a_usage_error(workdir: Path) -> None:
    result = runner.invoke(app, ["report", "--analysis", str(workdir / "nope.json")])
    assert result.exit_code == 2


def test_report_success(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from groww_pulse.config import load_settings
    from groww_pulse.report.pipeline import run_report
    from tests.fixtures.report import build_analysis

    path = build_analysis(load_settings(env_file=None, data_dir=workdir / "data"))
    # The CLI never allows synthetic data; this test opts in by wrapping.
    monkeypatch.setattr(
        "groww_pulse.cli.run_report", functools.partial(run_report, allow_synthetic=True)
    )
    result = runner.invoke(app, ["report", "--analysis", str(path)])
    assert result.exit_code == 0, result.output
    summary = _json_from(result.stdout)
    assert summary["iso_week"] == "2026-W42"
    assert int(str(summary["word_count"])) <= 250
    assert all(dict(summary["validators"]).values())  # type: ignore[call-overload]
    out = Path(str(summary["output_dir"]))
    assert {p.name for p in out.iterdir()} == {"pulse.md", "pulse.txt", "pulse.json"}


# ---------------------------------------------------------------- check-mcp / publish (Phase 5)
def _mcp_env(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.test/")
    monkeypatch.setenv("MCP_AUTH_TOKEN", token)


def test_check_mcp_requires_a_server_url(workdir: Path) -> None:
    result = runner.invoke(app, ["check-mcp"])
    assert result.exit_code == 2
    assert "MCP_SERVER_URL" in result.stderr


@pytest.mark.parametrize(("server_token", "gate"), [("same", True), (None, False)])
def test_check_mcp_reports_health_and_the_security_gate(
    workdir: Path, monkeypatch: pytest.MonkeyPatch, server_token: str | None, gate: bool
) -> None:
    import secrets

    from tests.fixtures.fake_mcp import FakeMcpServer

    token = secrets.token_urlsafe(16)
    _mcp_env(monkeypatch, token)
    fake = FakeMcpServer(token=token if server_token else None)
    monkeypatch.setattr("groww_pulse.cli.mcp_client_for", lambda _s: fake.client(token))
    result = runner.invoke(app, ["check-mcp"])
    assert result.exit_code == 0, result.output
    report = _json_from(result.stdout)
    assert report["healthy"] is True
    assert report["security_gate_passed"] is gate
    assert ("security gate" in result.stderr) is not gate
    assert token not in result.output
    assert fake.calls == []


def test_publish_refuses_synthetic_pulse(workdir: Path) -> None:
    from groww_pulse.config import load_settings
    from tests.fixtures.report import build_pulse

    build_pulse(load_settings(env_file=None, data_dir=workdir / "data"))
    result = runner.invoke(app, ["publish"])
    assert result.exit_code == 1
    assert "synthetic" in result.stderr


def test_publish_without_pulse_fails_cleanly(workdir: Path) -> None:
    result = runner.invoke(app, ["publish"])
    assert result.exit_code == 1
    assert "no pulse.json found" in result.stderr


def test_publish_dry_run_and_confirm(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import secrets

    from groww_pulse.config import load_settings
    from groww_pulse.delivery.publisher import publish
    from tests.fixtures.fake_mcp import FakeMcpServer
    from tests.fixtures.report import build_pulse

    token = secrets.token_urlsafe(16)
    _mcp_env(monkeypatch, token)
    monkeypatch.setenv("GOOGLE_DOC_ID", "fakeTestDocument_" + "c" * 20)
    monkeypatch.setenv("PULSE_EMAIL_TO", "pm@example.com")
    build_pulse(load_settings(env_file=None, data_dir=workdir / "data"))
    fake = FakeMcpServer(token=token)
    # The CLI never allows synthetic data or a fake server; this test opts in by wrapping.
    monkeypatch.setattr(
        "groww_pulse.cli.publish",
        functools.partial(publish, client=fake.client(token), allow_synthetic=True),
    )

    dry = runner.invoke(app, ["publish"])
    assert dry.exit_code == 0, dry.output
    assert "Dry run" in dry.stderr
    assert fake.requests == []

    refused = runner.invoke(app, ["publish", "--confirm"])  # DELIVERY_MODE is still dry_run
    assert refused.exit_code == 1
    assert "DELIVERY_MODE=confirm" in refused.stderr

    monkeypatch.setenv("DELIVERY_MODE", "confirm")
    live = runner.invoke(app, ["publish", "--confirm"])
    assert live.exit_code == 0, live.output
    report = _json_from(live.stdout)
    targets = report["targets"]
    assert isinstance(targets, list)
    assert [t["status"] for t in targets] == ["delivered", "delivered"]
    assert report["email_sent"] is False
    assert "fakeTestDocument_" not in live.output
    assert token not in live.output

    again = runner.invoke(app, ["publish", "--confirm"])
    assert again.exit_code == 0
    assert "skipped_already_delivered" in again.stdout


def test_publish_confirm_cannot_bypass_the_week_lock(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import secrets

    from groww_pulse.config import load_settings
    from groww_pulse.delivery.publisher import publish
    from tests.fixtures.fake_mcp import FakeMcpServer
    from tests.fixtures.report import build_pulse, build_variant

    token = secrets.token_urlsafe(16)
    _mcp_env(monkeypatch, token)
    monkeypatch.setenv("GOOGLE_DOC_ID", "fakeTestDocument_" + "c" * 20)
    monkeypatch.setenv("PULSE_EMAIL_TO", "pm@example.com")
    monkeypatch.setenv("DELIVERY_MODE", "confirm")
    original = build_pulse(load_settings(env_file=None, data_dir=workdir / "data"))
    fake = FakeMcpServer(token=token)
    monkeypatch.setattr(
        "groww_pulse.cli.publish",
        functools.partial(publish, client=fake.client(token), allow_synthetic=True),
    )
    assert runner.invoke(app, ["publish", "--confirm"]).exit_code == 0
    build_variant(original, "report-run-v2")
    calls_before = len(fake.calls)

    for args in (["publish", "--confirm"], ["publish", "--confirm", "--force"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 1, result.output
        report = _json_from(result.stdout)
        targets = report["targets"]
        assert isinstance(targets, list)
        assert {t["status"] for t in targets} == {"blocked_week_already_delivered"}
    assert len(fake.calls) == calls_before


def test_export_snapshot_refuses_synthetic_and_missing_pulses(workdir: Path) -> None:
    from groww_pulse.config import load_settings
    from tests.fixtures.report import build_pulse

    missing = runner.invoke(app, ["export-snapshot", "--out", str(workdir / "snap")])
    assert missing.exit_code == 1
    assert "no pulse.json found" in missing.stderr

    build_pulse(load_settings(env_file=None, data_dir=workdir / "data"))
    refused = runner.invoke(app, ["export-snapshot", "--out", str(workdir / "snap")])
    assert refused.exit_code == 1
    assert "synthetic" in refused.stderr
    assert not (workdir / "snap").exists()
