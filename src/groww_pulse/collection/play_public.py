"""Disabled guard for automated Google Play collection.

Automated collection is **rejected** (docs/data-source-decision.md): Google Play's
robots.txt disallows the review endpoints (``/_``, ``/store/getreviews``), and Google's Terms
of Service prohibit automated access that violates robots.txt. This module therefore
contains no fetching code. It always raises :class:`CollectorDisabledError`, whatever the
``ENABLE_PLAY_COLLECTOR`` setting says. Reviews enter only through the importers.
"""

from __future__ import annotations

from collections.abc import Iterator

from groww_pulse.collection.base import DateWindow
from groww_pulse.errors import CollectorDisabledError
from groww_pulse.models import RawReview

DISABLED_MESSAGE = (
    "Automated Google Play collection is disabled: robots.txt disallows the review endpoints "
    "and Google's Terms of Service prohibit automated access that violates it. Import a "
    "permitted export instead (see docs/data-source-decision.md): "
    "groww-pulse import <file-or-directory>"
)


class PlayPublicCollector:
    """Satisfies the ``ReviewSource`` protocol but can never fetch anything."""

    def fetch(self, window: DateWindow) -> Iterator[RawReview]:
        raise CollectorDisabledError(DISABLED_MESSAGE)


def collect(window: DateWindow) -> list[RawReview]:
    """Always raises; kept so callers get an explicit, documented refusal."""
    return list(PlayPublicCollector().fetch(window))
