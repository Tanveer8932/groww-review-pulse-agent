"""``groww-pulse`` command-line interface.

Phase 1 commands: ``import`` (CSV/JSON/JSONL exports into the raw store) and ``collect``
(which only explains that automated collection is disabled). Phase 2 adds ``preprocess``
(raw store -> filtered, deduplicated, redacted ``reviews.jsonl`` plus a funnel). Phase 3
adds ``analyze`` (themes, ranking, verified quotes, and action ideas into
``runs/{run_id}/analysis.json``). Phase 4 adds ``report`` (verified weekly pulse into
``runs/{run_id}/pulse.{md,txt,json}``; nothing is published). Phase 5 adds ``check-mcp``
(read-only MCP health and auth check) and ``publish`` (dry run by default; ``--confirm``
appends to the Google Doc and creates a Gmail draft; email is never sent). Phase 6 adds
``export-snapshot`` (a verified pulse as a read-only, committable dashboard snapshot).
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import typer

from groww_pulse.analysis.pipeline import run_analysis
from groww_pulse.collection.base import DateWindow
from groww_pulse.collection.importers import import_files, load_column_map
from groww_pulse.collection.play_public import DISABLED_MESSAGE
from groww_pulse.config import (
    MAX_WINDOW_WEEKS,
    MIN_WINDOW_WEEKS,
    DeliveryMode,
    Settings,
    load_settings,
)
from groww_pulse.delivery.publisher import mcp_client_for, publish
from groww_pulse.errors import (
    AnalysisError,
    ConfigError,
    DeliveryError,
    ImportSchemaError,
    PreprocessingError,
    ReportValidationError,
)
from groww_pulse.logging_setup import bind_run_id, configure_logging
from groww_pulse.preprocessing.pipeline import run_preprocessing
from groww_pulse.report.pipeline import run_report
from groww_pulse.snapshot import export_snapshot

app = typer.Typer(
    name="groww-pulse",
    help="Groww Review Pulse Agent (Google Play reviews only).",
    no_args_is_help=True,
    add_completion=False,
)

EXIT_OK, EXIT_PARTIAL, EXIT_USAGE = 0, 1, 2


def _settings() -> Settings:
    try:
        settings = load_settings()
    except ConfigError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(EXIT_USAGE) from None
    configure_logging(settings.log_level, settings.log_format, settings.secret_values())
    return settings


def _parse_as_of(value: str | None) -> date:
    if value is None:
        return datetime.now(UTC).date()
    try:
        return date.fromisoformat(value)
    except ValueError:
        typer.echo(f"--as-of must be YYYY-MM-DD, got {value!r}", err=True)
        raise typer.Exit(EXIT_USAGE) from None


@app.command("import")
def import_command(
    paths: Annotated[
        list[Path],
        typer.Argument(
            help="Export files (.csv, .json, .jsonl) or directories containing them.",
            exists=True,
            readable=True,
        ),
    ],
    column_map: Annotated[
        Path | None,
        typer.Option("--map", help="YAML map of canonical field -> header.", exists=True),
    ] = None,
    as_of: Annotated[
        str | None,
        typer.Option(help="Last day of the window, YYYY-MM-DD (UTC). Default: today."),
    ] = None,
    weeks: Annotated[
        int | None,
        typer.Option(
            min=MIN_WINDOW_WEEKS,
            max=MAX_WINDOW_WEEKS,
            help="Window length in weeks. Default: ANALYSIS_WINDOW_WEEKS.",
        ),
    ] = None,
) -> None:
    """Import permitted review exports into data/raw/reviews_raw.jsonl."""
    settings = _settings()
    window = DateWindow.ending(_parse_as_of(as_of), weeks or settings.analysis_window_weeks)
    try:
        overrides = load_column_map(column_map) if column_map else None
    except ImportSchemaError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_USAGE) from None

    with bind_run_id() as run_id:
        report = import_files(
            paths,
            app_id=settings.app_id,
            window=window,
            data_dir=settings.data_dir,
            run_id=run_id,
            column_map=overrides,
        )
    typer.echo(json.dumps(report.as_dict(), indent=2))
    if not report.files:
        typer.echo("No importable files found.", err=True)
        raise typer.Exit(EXIT_USAGE)
    raise typer.Exit(EXIT_OK if report.ok else EXIT_PARTIAL)


@app.command("preprocess")
def preprocess_command(
    as_of: Annotated[
        str | None,
        typer.Option(help="Last day of the window, YYYY-MM-DD (UTC). Default: today."),
    ] = None,
    weeks: Annotated[
        int | None,
        typer.Option(
            min=MIN_WINDOW_WEEKS,
            max=MAX_WINDOW_WEEKS,
            help="Window length in weeks. Default: ANALYSIS_WINDOW_WEEKS.",
        ),
    ] = None,
) -> None:
    """Filter, deduplicate, and redact the raw store into data/reviews.jsonl."""
    settings = _settings()
    as_of_date = _parse_as_of(as_of)
    with bind_run_id() as run_id:
        try:
            report = run_preprocessing(settings, as_of=as_of_date, weeks=weeks, run_id=run_id)
        except PreprocessingError as exc:
            typer.echo(f"Preprocessing failed: {exc}", err=True)
            raise typer.Exit(EXIT_PARTIAL) from None
    typer.echo(json.dumps(report.as_dict(), indent=2))
    for warning in report.warnings:
        typer.echo(f"Warning: {warning}", err=True)
    raise typer.Exit(EXIT_OK)


@app.command("analyze")
def analyze_command() -> None:
    """Find themes, rank them, select verified quotes, and propose actions (needs the LLM)."""
    settings = _settings()
    try:
        settings.require_llm()
    except ConfigError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(EXIT_USAGE) from None
    with bind_run_id() as run_id:
        try:
            result = run_analysis(settings, run_id=run_id)
        except (AnalysisError, PreprocessingError) as exc:
            typer.echo(f"Analysis failed (run {run_id}): {exc}", err=True)
            raise typer.Exit(EXIT_PARTIAL) from None
    typer.echo(json.dumps(result.summary(), indent=2, ensure_ascii=False))
    for warning in result.warnings:
        typer.echo(f"Warning: {warning}", err=True)
    raise typer.Exit(EXIT_OK)


@app.command("report")
def report_command(
    analysis: Annotated[
        Path | None,
        typer.Option(
            "--analysis",
            help="analysis.json to report on. Default: the newest one under DATA_DIR/runs.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
) -> None:
    """Verify the analysis and render the weekly pulse (no LLM calls, nothing published)."""
    settings = _settings()
    with bind_run_id() as run_id:
        try:
            result = run_report(settings, run_id=run_id, analysis_path=analysis)
        except ReportValidationError as exc:
            typer.echo(f"Report failed (run {run_id}): {exc}", err=True)
            raise typer.Exit(EXIT_PARTIAL) from None
    typer.echo(json.dumps(result.summary(), indent=2, ensure_ascii=False))
    for warning in result.warnings:
        typer.echo(f"Warning: {warning}", err=True)
    raise typer.Exit(EXIT_OK)


@app.command("export-snapshot")
def export_snapshot_command(
    pulse: Annotated[
        Path | None,
        typer.Option(
            "--pulse",
            help="pulse.json (or its run directory). Default: the newest under DATA_DIR/runs.",
            exists=True,
            readable=True,
        ),
    ] = None,
    out: Annotated[
        Path,
        typer.Option("--out", help="Snapshot directory to (re)write. Commit it for a hosted UI."),
    ] = Path("snapshot"),
) -> None:
    """Export one verified real pulse as a read-only dashboard snapshot (review IDs withheld)."""
    settings = _settings()
    try:
        result = export_snapshot(settings.data_dir, out, pulse_path=pulse)
    except DeliveryError as exc:
        typer.echo(f"Snapshot refused: {exc}", err=True)
        raise typer.Exit(EXIT_PARTIAL) from None
    typer.echo(json.dumps(result.manifest, indent=2, ensure_ascii=False))
    raise typer.Exit(EXIT_OK)


@app.command("check-mcp")
def check_mcp_command() -> None:
    """Read-only MCP health check: handshake, tools/list, contract, and auth enforcement."""
    settings = _settings()
    try:
        settings.require_mcp()
    except ConfigError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(EXIT_USAGE) from None
    with bind_run_id():
        health = asyncio.run(mcp_client_for(settings).check(probe_auth=True))
    typer.echo(json.dumps(health.as_dict(), indent=2))
    if health.healthy and not health.security_gate_passed:
        typer.echo(
            "Warning: live delivery is blocked by the security gate: " + health.auth_detail,
            err=True,
        )
    raise typer.Exit(EXIT_OK if health.healthy else EXIT_PARTIAL)


@app.command("publish")
def publish_command(
    pulse: Annotated[
        Path | None,
        typer.Option(
            "--pulse",
            help="pulse.json (or its run directory). Default: the newest under DATA_DIR/runs.",
            exists=True,
            readable=True,
        ),
    ] = None,
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help="Really append to the Doc and create the draft. Also needs "
            "DELIVERY_MODE=confirm. Without it, this is a dry run with no network calls.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Deliver again even if the ledger says delivered or outcome unknown. Check "
            "the Doc and the Drafts folder first. Never overrides duplicate-week protection: "
            "a week already delivered with different content stays blocked.",
        ),
    ] = False,
) -> None:
    """Deliver the validated pulse via MCP: Doc append + Gmail draft. Never sends email."""
    settings = _settings()
    with bind_run_id() as run_id:
        try:
            report = publish(
                settings, run_id=run_id, pulse_path=pulse, confirm=confirm, force=force
            )
        except ConfigError as exc:
            typer.echo(f"Configuration error: {exc}", err=True)
            raise typer.Exit(EXIT_USAGE) from None
        except DeliveryError as exc:
            typer.echo(f"Publish refused (run {run_id}): {exc}", err=True)
            raise typer.Exit(EXIT_PARTIAL) from None
    typer.echo(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    if report.mode is DeliveryMode.DRY_RUN:
        typer.echo("Dry run: nothing was sent to the MCP server.", err=True)
    raise typer.Exit(EXIT_OK if report.ok else EXIT_PARTIAL)


@app.command("collect")
def collect_command() -> None:
    """Automated Google Play collection is disabled; explains how to import instead."""
    typer.echo(DISABLED_MESSAGE, err=True)
    raise typer.Exit(EXIT_USAGE)


def main() -> None:  # pragma: no cover — console-script shim
    sys.exit(app())
