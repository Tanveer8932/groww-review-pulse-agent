"""Versioned prompt templates (``<name>.<version>.txt``) and the untrusted-data encoder.

Each template has a ``### SYSTEM`` section and a ``### USER`` section. The user section is a
:class:`string.Template`; every placeholder must be supplied (``substitute``, not
``safe_substitute``). Changing a template's wording means adding a new version file, so
cached replies are never reused for a different prompt.

Prompt-injection containment: review text (and anything derived from LLM output) reaches a
prompt only through :func:`data_block`, which writes one JSON object per line inside an XML
tag. JSON escaping keeps quotes and newlines inside their string, and ``<``, ``>``, ``&``
are written as ``\\u003c``-style escapes, so text can never close the data tag or open a
new one. Every system prompt states that data blocks are untrusted and never instructions.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from string import Template
from typing import Any

_SECTION_RE = re.compile(r"^### (SYSTEM|USER)\s*$", re.MULTILINE)
_TAG_RE = re.compile(r"^[a-z_]+$")
_ESCAPES = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026"}


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: str
    system: str
    user: Template

    def render_user(self, **values: str) -> str:
        return self.user.substitute(**values)


@cache
def load_prompt(name: str, version: str) -> PromptTemplate:
    """Load ``<name>.<version>.txt`` from this package."""
    raw = resources.files(__package__).joinpath(f"{name}.{version}.txt").read_text("utf-8")
    parts = _SECTION_RE.split(raw)
    # ["preamble", "SYSTEM", system_text, "USER", user_text]
    if len(parts) != 5 or parts[1] != "SYSTEM" or parts[3] != "USER":
        raise ValueError(f"prompt {name}.{version} must have ### SYSTEM then ### USER")
    return PromptTemplate(
        name=name, version=version, system=parts[2].strip(), user=Template(parts[4].strip())
    )


def _escape(line: str) -> str:
    for char, escape in _ESCAPES.items():
        line = line.replace(char, escape)
    return line


def data_block(tag: str, records: Iterable[Mapping[str, Any]]) -> str:
    """Untrusted records as JSON lines inside ``<tag>...</tag>`` (see module docstring)."""
    if not _TAG_RE.fullmatch(tag):
        raise ValueError(f"invalid data block tag: {tag!r}")
    lines = [
        _escape(json.dumps(dict(r), ensure_ascii=False, separators=(", ", ": "))) for r in records
    ]
    return f"<{tag}>\n" + "\n".join(lines) + f"\n</{tag}>"
