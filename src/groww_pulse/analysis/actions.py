"""Action ideas (architecture §6.4, FR-14).

The LLM (``actions.v1``) receives only code-computed theme statistics and the verified
quotes, as data blocks. Its reply is rejected (then repaired once) unless:

* there are exactly 3 actions, one for each top theme;
* each ``action`` has at most :data:`MAX_ACTION_WORDS` words and starts with a verb-like
  word (heuristic: not an article, pronoun, or other common non-verb opener);
* ``owner_area`` is one of :data:`OWNER_AREAS`; ``rationale`` has at most
  :data:`MAX_RATIONALE_WORDS` words;
* no number appears that is not one of the statistics given in the prompt;
* there are no quotation marks (actions must not pose as review quotes), no personal data,
  and no two identical actions.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from groww_pulse.analysis.llm import LLMClient, OutputRejectedError, PromptRequest
from groww_pulse.analysis.prompts import data_block, load_prompt
from groww_pulse.models import ActionIdea, Quote, Theme
from groww_pulse.preprocessing.normalize import count_words
from groww_pulse.preprocessing.pii import PLACEHOLDERS, scan_pii

ACTIONS_REQUIRED = 3
MAX_ACTION_WORDS = 25
MAX_RATIONALE_WORDS = 40
ACTIONS_PROMPT = ("actions", "v1")
OWNER_AREAS: tuple[str, ...] = (
    "Engineering",
    "Product",
    "Design",
    "Customer Support",
    "Payments",
    "Trading Platform",
    "Onboarding and KYC",
    "Operations",
    "Compliance",
)

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_QUOTE_MARKS_RE = re.compile(r"[\"“”„«»]")
_PLACEHOLDER_RE = re.compile(r"\[(?:" + "|".join(PLACEHOLDERS) + r")\]")
_FIRST_WORD_RE = re.compile(r"[A-Za-z]+")
#: Openers that make an action a statement rather than an instruction.
_NON_VERB_OPENERS = frozenset(
    {
        "a", "an", "the", "this", "that", "these", "those", "it", "its", "we", "our", "us",
        "they", "their", "i", "you", "your", "users", "user", "customers", "customer",
        "groww", "there", "all", "some", "many", "more", "most", "better", "new", "team",
        "teams", "maybe", "perhaps", "should", "could", "would", "if", "when", "because",
    }
)  # fmt: skip


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionItem(_Strict):
    theme_id: str
    action: str
    owner_area: str
    rationale: str


class ActionsOutput(_Strict):
    actions: list[ActionItem]


def _schema(theme_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "theme_id": {"type": "string", "enum": list(theme_ids)},
                        "action": {"type": "string"},
                        "owner_area": {"type": "string", "enum": list(OWNER_AREAS)},
                        "rationale": {"type": "string"},
                    },
                    "required": ["theme_id", "action", "owner_area", "rationale"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["actions"],
        "additionalProperties": False,
    }


def pct(value: float) -> str:
    """A share as a percentage with one decimal, as shown to the LLM and in reports."""
    return f"{value * 100:.1f}"


def theme_records(top_themes: Sequence[Theme]) -> list[dict[str, Any]]:
    return [
        {
            "id": t.theme_id,
            "rank": t.rank,
            "name": t.name,
            "description": t.description,
            "count": t.count,
            "share_pct": pct(t.share),
            "avg_rating": f"{t.avg_rating:.1f}" if t.avg_rating is not None else None,
            "low_rating_pct": pct(t.low_rating_share),
        }
        for t in top_themes
    ]


def _normalize_number(token: str) -> str:
    value = float(token.replace(",", ""))
    return f"{value:g}"


def allowed_numbers(top_themes: Sequence[Theme]) -> set[str]:
    """Every number the prompt shows, normalized (``12.0`` and ``12`` are equal)."""
    allowed: set[str] = set()
    for record in theme_records(top_themes):
        for key in ("rank", "count", "share_pct", "avg_rating", "low_rating_pct"):
            if record[key] is not None:
                allowed.add(_normalize_number(str(record[key])))
    return allowed


def action_problems(output: ActionsOutput, top_ids: Sequence[str], allowed: set[str]) -> list[str]:
    problems: list[str] = []
    actions = output.actions
    if len(actions) != ACTIONS_REQUIRED:
        problems.append(f"expected exactly {ACTIONS_REQUIRED} actions, got {len(actions)}")
    ids = [a.theme_id for a in actions]
    if sorted(ids) != sorted(top_ids):
        problems.append(f"need exactly one action per theme id {', '.join(top_ids)}")
    seen: set[str] = set()
    for i, item in enumerate(actions):
        action, rationale = " ".join(item.action.split()), " ".join(item.rationale.split())
        if not action:
            problems.append(f"actions[{i}].action is empty")
            continue
        if count_words(action) > MAX_ACTION_WORDS:
            problems.append(f"actions[{i}].action is longer than {MAX_ACTION_WORDS} words")
        if count_words(rationale) > MAX_RATIONALE_WORDS:
            problems.append(f"actions[{i}].rationale is longer than {MAX_RATIONALE_WORDS} words")
        first = _FIRST_WORD_RE.match(action)
        if first is None or first.group(0).lower() in _NON_VERB_OPENERS:
            problems.append(f"actions[{i}].action must start with an imperative verb")
        if item.owner_area not in OWNER_AREAS:
            problems.append(f"actions[{i}].owner_area is not an allowed owner area")
        for field_name, text in (("action", action), ("rationale", rationale)):
            invented = sorted(
                {n for n in _NUMBER_RE.findall(text) if _normalize_number(n) not in allowed}
            )
            if invented:
                problems.append(
                    f"actions[{i}].{field_name} uses numbers not in the computed statistics: "
                    f"{', '.join(invented)}"
                )
            if _QUOTE_MARKS_RE.search(text):
                problems.append(f"actions[{i}].{field_name} contains quotation marks")
            if scan_pii(text) or _PLACEHOLDER_RE.search(text):
                problems.append(f"actions[{i}].{field_name} contains personal data")
        key = action.casefold()
        if key in seen:
            problems.append(f"actions[{i}] repeats an earlier action")
        seen.add(key)
    return problems


def actions_validator(top_ids: Sequence[str], allowed: set[str]) -> Callable[[ActionsOutput], None]:
    def validate(output: ActionsOutput) -> None:
        if problems := action_problems(output, top_ids, allowed):
            raise OutputRejectedError("; ".join(problems))

    return validate


def generate_actions(
    client: LLMClient, top_themes: Sequence[Theme], quotes: Sequence[Quote]
) -> list[ActionIdea]:
    """Exactly 3 validated action ideas, ordered by theme rank."""
    template = load_prompt(*ACTIONS_PROMPT)
    top_ids = [t.theme_id for t in top_themes]
    request = PromptRequest(
        name=template.name,
        version=template.version,
        system=template.system,
        user=template.render_user(
            themes=data_block("themes", theme_records(top_themes)),
            quotes=data_block(
                "quotes", [{"theme_id": q.theme_id, "quote": q.text} for q in quotes]
            ),
            owner_areas=", ".join(OWNER_AREAS),
        ),
        schema=_schema(top_ids),
        max_tokens=8_000,
        effort="medium",
    )
    output = client.complete_json(
        request, ActionsOutput, actions_validator(top_ids, allowed_numbers(top_themes))
    )
    order = {theme_id: i for i, theme_id in enumerate(top_ids)}
    return [
        ActionIdea(
            theme_id=item.theme_id,
            action=" ".join(item.action.split()),
            owner_area=item.owner_area,
            rationale=" ".join(item.rationale.split()),
        )
        for item in sorted(output.actions, key=lambda a: order[a.theme_id])
    ]
