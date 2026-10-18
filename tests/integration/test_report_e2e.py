# SYNTHETIC TEST FIXTURE — not real review data
"""End-to-end Phases 1-4: a synthetic Play Console export -> import -> preprocess ->
analyze (fake LLM) -> report.

Runs offline (no live LLM or MCP) in a temporary DATA_DIR, so it is part of the normal
suite. It proves the stages fit together and that the production path (no
``allow_synthetic``) refuses labelled fixtures at every stage, so synthetic data can never
reach a production pulse.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from groww_pulse.analysis.pipeline import run_analysis
from groww_pulse.collection.base import DateWindow
from groww_pulse.collection.importers import import_files
from groww_pulse.config import Settings, load_settings
from groww_pulse.errors import PreprocessingError, ReportValidationError
from groww_pulse.preprocessing.normalize import count_words
from groww_pulse.preprocessing.pii import scan_pii
from groww_pulse.preprocessing.pipeline import run_preprocessing
from groww_pulse.report.pipeline import PULSE_JSON, run_report
from groww_pulse.report.render import quote_display
from groww_pulse.storage.jsonl_store import JsonlReviewStore
from tests.fixtures.analysis import BASE_TIME, CORPUS, PLANTED_PHONE, keyword_provider
from tests.fixtures.synthetic import AS_OF, play_row, write_play_csv


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(env_file=None, data_dir=tmp_path / "data", log_level="WARNING")


def _export(tmp_path: Path) -> Path:
    rows = []
    n = 0
    for key, items in CORPUS.items():
        for text, rating in items:
            n += 1
            rows.append(
                play_row(
                    f"e2e-{key}-{n:02d}",
                    BASE_TIME + timedelta(days=n),
                    rating,
                    f"SYNTHETIC FIXTURE {text}",
                )
            )
    return write_play_csv(tmp_path / "synthetic_play_export.csv", rows)


def _import(settings: Settings, tmp_path: Path) -> None:
    report = import_files(
        [_export(tmp_path)],
        app_id=settings.app_id,
        window=DateWindow.ending(AS_OF, 12),
        data_dir=settings.data_dir,
        run_id="e2e-import",
    )
    assert report.ok


def test_phases_one_to_four(settings: Settings, tmp_path: Path) -> None:
    _import(settings, tmp_path)
    run_preprocessing(settings, as_of=AS_OF, run_id="e2e-pre", allow_synthetic=True)
    run_analysis(settings, run_id="e2e-an", provider=keyword_provider(), allow_synthetic=True)
    result = run_report(settings, run_id="e2e-rep", allow_synthetic=True)

    pulse = result.pulse
    text = (settings.data_dir / "runs" / "e2e-rep" / "pulse.txt").read_text("utf-8")
    assert count_words(text) == pulse.word_count <= 250
    assert (len(pulse.themes), len(pulse.quotes), len(pulse.actions)) == (3, 3, 3)
    assert {a.theme_id for a in pulse.actions} == {t.theme_id for t in pulse.themes}
    assert scan_pii(text) == {}
    assert PLANTED_PHONE not in text

    # Each quote is genuine: a verbatim span of the redacted review, and (being PII-free)
    # also present verbatim in the original raw export text.
    raw = JsonlReviewStore(settings.data_dir / "raw" / "reviews_raw.jsonl").load()
    for quote in pulse.quotes:
        assert quote_display(quote) in text
        assert quote.text in raw[quote.review_id].text

    data = json.loads((settings.data_dir / "runs" / "e2e-rep" / PULSE_JSON).read_text("utf-8"))
    assert data["source"]["preprocess_run_id"] == "e2e-pre"
    assert data["source"]["analysis_run_id"] == "e2e-an"
    assert data["publishable"] is False  # synthetic: never publishable


def test_production_path_refuses_synthetic_data_at_every_stage(
    settings: Settings, tmp_path: Path
) -> None:
    _import(settings, tmp_path)
    with pytest.raises(PreprocessingError, match="synthetic"):
        run_preprocessing(settings, as_of=AS_OF, run_id="prod-pre")

    # Even if a test-only run produced the files, analysis and report still refuse them.
    run_preprocessing(settings, as_of=AS_OF, run_id="e2e-pre", allow_synthetic=True)
    with pytest.raises(PreprocessingError, match="synthetic"):
        run_analysis(settings, run_id="prod-an", provider=keyword_provider())
    run_analysis(settings, run_id="e2e-an", provider=keyword_provider(), allow_synthetic=True)
    with pytest.raises(ReportValidationError, match="synthetic"):
        run_report(settings, run_id="prod-rep")
    assert not list((settings.data_dir / "runs").glob("*/pulse.*"))
