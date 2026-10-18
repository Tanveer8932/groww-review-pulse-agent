# SYNTHETIC TEST FIXTURE — not real review data
"""Live LLM smoke test. Runs only with RUN_LIVE_TESTS=1 and the configured provider's key
(``ANTHROPIC_API_KEY``, or ``GROQ_API_KEY`` with ``LLM_PROVIDER=groq``) set.

Runs the full Phase 3 analysis on the labelled synthetic corpus in a temporary DATA_DIR
against the real configured model. It spends a small number of tokens, never touches the
project's ``data/`` directory, and its output is marked ``synthetic_fixture_data``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groww_pulse.analysis.pipeline import run_analysis
from groww_pulse.config import load_settings
from tests.fixtures.analysis import build_corpus


@pytest.mark.live
def test_live_analysis_on_synthetic_corpus(
    live_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "GROQ_API_KEY" if live_env.get("LLM_PROVIDER") == "groq" else "ANTHROPIC_API_KEY"
    if not live_env.get(key):
        pytest.skip(f"{key} not set")
    for name in ("LLM_PROVIDER", key, "LLM_MODEL"):
        if live_env.get(name):
            monkeypatch.setenv(name, live_env[name])
    settings = load_settings(env_file=None, data_dir=tmp_path / "data", log_level="WARNING")
    build_corpus(settings)

    result = run_analysis(settings, run_id="live-smoke", allow_synthetic=True)
    analysis = result.analysis
    assert analysis["method"]["themes"] == "llm"
    assert 3 <= len(analysis["themes"]) <= 5
    assert len(analysis["quotes"]) == 3
    assert len(analysis["actions"]) == 3
    assert result.synthetic_fixture_data is True

    again = run_analysis(settings, run_id="live-smoke-2", allow_synthetic=True)
    assert again.llm_usage["provider_calls"] == 0
    assert again.analysis_sha256 == result.analysis_sha256
