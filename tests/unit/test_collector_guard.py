"""Automated Google Play collection must be impossible, and no App Store path may exist."""

from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path

import pytest

from groww_pulse.collection import play_public
from groww_pulse.collection.base import DateWindow
from groww_pulse.errors import CollectorDisabledError

SRC = Path(__file__).parents[2] / "src" / "groww_pulse"
WINDOW = DateWindow.ending(date(2026, 10, 14), 12)
NETWORK_MODULES = {
    "httpx",
    "requests",
    "urllib",
    "urllib3",
    "http",
    "socket",
    "aiohttp",
    "google_play_scraper",
    "selenium",
    "playwright",
    "bs4",
}


@pytest.mark.parametrize("flag", ["true", "false"])
def test_collector_always_refuses(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    monkeypatch.setenv("ENABLE_PLAY_COLLECTOR", flag)
    with pytest.raises(CollectorDisabledError, match=r"robots.txt"):
        play_public.PlayPublicCollector().fetch(WINDOW)
    with pytest.raises(CollectorDisabledError):
        play_public.collect(WINDOW)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_collector_module_has_no_network_imports() -> None:
    assert not _imported_modules(Path(play_public.__file__)) & NETWORK_MODULES


def test_collection_package_has_no_network_imports() -> None:
    for module in (SRC / "collection").glob("*.py"):
        assert not _imported_modules(module) & NETWORK_MODULES, module.name


def _code_tokens(path: Path) -> list[tuple[int, str]]:
    """Identifiers, imports and string literals, excluding docstrings (which may explain bans)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    tokens: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            tokens.append((node.lineno, node.value))
        elif isinstance(node, ast.Name):
            tokens.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute):
            tokens.append((node.lineno, node.attr))
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            tokens.extend((node.lineno, n) for n in names)
    return tokens


def test_no_scraping_endpoints_or_app_store_code_paths() -> None:
    forbidden = re.compile(
        r"batchexecute|/store/getreviews|google_play_scraper|itunes|apps\.apple\.com|app_?store",
        re.IGNORECASE,
    )
    hits = [
        f"{p.relative_to(SRC)}:{line}: {token[:60]}"
        for p in SRC.rglob("*.py")
        for line, token in _code_tokens(p)
        if forbidden.search(token)
    ]
    assert hits == []


def test_forbidden_pattern_scan_detects_code() -> None:
    """Guard the guard: a literal in code (not a docstring) must be caught."""
    sample = 'URL = "https://play.google.com/_/PlayStoreUi/data/batchexecute"\n'
    tree_tokens = [t for _, t in _code_tokens_from_source(sample)]
    assert any("batchexecute" in t for t in tree_tokens)


def _code_tokens_from_source(source: str) -> list[tuple[int, str]]:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.py"
        path.write_text(source, encoding="utf-8")
        return _code_tokens(path)
