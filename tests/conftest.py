"""Shared pytest fixtures.

Tests never read the developer's real ``.env`` and never touch the network unless marked
``live`` and run with ``RUN_LIVE_TESTS=1``.
"""

from __future__ import annotations

import os

import pytest

from groww_pulse.config import Settings

_SETTINGS_ENV_VARS = [name.upper() for name in Settings.model_fields]

#: Environment as it was before any fixture ran; only live tests may use it.
ORIGINAL_ENV: dict[str, str] = dict(os.environ)


@pytest.fixture
def live_env() -> dict[str, str]:
    """The real process environment, for opt-in live tests."""
    return dict(ORIGINAL_ENV)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every setting from the process environment so tests start from defaults."""
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("RUN_LIVE_TESTS") == "1":
        return
    skip_live = pytest.mark.skip(reason="live test; set RUN_LIVE_TESTS=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
