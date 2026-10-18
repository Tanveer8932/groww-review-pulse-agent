"""Quote selection and verbatim verification (architecture §6.4, FR-13).

Quotes are chosen and cut by code; the LLM never writes, edits, or picks them.

**Candidates.** For each top theme: member reviews that are ``quote_eligible`` (lenient-emoji
reviews never are). Preferred (tier 0): reviews of 12-60 words whose snippet has no redaction
placeholder; everything else is tier 1. Within a tier, candidates rank by representativeness
(cosine similarity of the snippet's TF-IDF vector to the theme centroid), then helpfulness
(``thumbs_up``), then ``review_id``.

**Snippet.** The whole redacted text if it has at most :data:`QUOTE_MAX_WORDS` words;
otherwise the most representative contiguous run of whole sentences with
:data:`QUOTE_MIN_WORDS` to :data:`QUOTE_MAX_WORDS` words. Text is never paraphrased,
corrected, re-cased, or cut mid-sentence. A span that does not start or end the review sets
``leading_ellipsis`` / ``trailing_ellipsis`` so the renderer can add "…" *outside* the quote.

**Selection.** One quote per top theme in rank order; if a theme has no candidate, the next
best unused candidates of the top themes are taken round-robin (rank order) until there
are exactly 3. Each review is quoted at most once. Fewer than 3 raises
:class:`InsufficientDataError`: the run fails closed.

**Verification** (:func:`verify_quotes`) re-checks every quote against ``reviews.jsonl`` and
the raw store before it is used: the span is verbatim, the stored text hash matches, the
review belongs to the quoted theme, and the redacted text re-derives from the original raw
text (provenance).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from groww_pulse.analysis.themes import tfidf_vectorizer
from groww_pulse.errors import InsufficientDataError, QuoteVerificationError
from groww_pulse.models import Quote, RawReview, Review, Theme
from groww_pulse.preprocessing.emoji_policy import contains_emoji
from groww_pulse.preprocessing.normalize import count_words
from groww_pulse.preprocessing.pii import PLACEHOLDERS, scan_pii
from groww_pulse.preprocessing.pipeline import sha256_text, verify_provenance

QUOTES_REQUIRED = 3
QUOTE_MAX_WORDS = 35
QUOTE_MIN_WORDS = 9  # same as the review length rule: more than 8 words
PREFERRED_MIN_WORDS, PREFERRED_MAX_WORDS = 12, 60

_PLACEHOLDER_RE = re.compile(r"\[(?:" + "|".join(PLACEHOLDERS) + r")\]")
#: End of a sentence: terminal punctuation, optional closing quotes/brackets, then a space.
_SENTENCE_END_RE = re.compile(r"[.!?]+[\"'’”)\]]*(?=\s)")


@dataclass(frozen=True)
class _Candidate:
    review: Review
    theme_id: str
    start: int
    end: int
    tier: int
    similarity: float

    @property
    def sort_key(self) -> tuple[int, float, int, str]:
        return (
            self.tier,
            -round(self.similarity, 9),
            -(self.review.thumbs_up or 0),
            self.review.review_id,
        )

    def to_quote(self) -> Quote:
        text = self.review.text
        return Quote(
            review_id=self.review.review_id,
            theme_id=self.theme_id,
            text=text[self.start : self.end],
            start=self.start,
            end=self.end,
            rating=self.review.rating,
            created_at=self.review.created_at,
            leading_ellipsis=self.start > 0,
            trailing_ellipsis=self.end < len(text),
        )


# ---------------------------------------------------------------- spans
def sentence_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of each sentence; spans exclude the separating whitespace."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        spans.append((start, match.end()))
        start = match.end()
        while start < len(text) and text[start].isspace():
            start += 1
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def snippet_spans(text: str) -> list[tuple[int, int]]:
    """Every allowed verbatim span of ``text`` (see module docstring)."""
    if count_words(text) <= QUOTE_MAX_WORDS:
        return [(0, len(text))]
    sentences = sentence_spans(text)
    spans = []
    for i, (start, _) in enumerate(sentences):
        for _, end in sentences[i:]:
            words = count_words(text[start:end])
            if words > QUOTE_MAX_WORDS:
                break
            if words >= QUOTE_MIN_WORDS:
                spans.append((start, end))
    return spans


# ---------------------------------------------------------------- selection
def _candidates_for(
    theme: Theme,
    reviews_by_id: Mapping[str, Review],
    vectorizer: TfidfVectorizer,
    row_of: Mapping[str, int],
    matrix: object,
) -> list[_Candidate]:
    members = [reviews_by_id[rid] for rid in theme.review_ids if rid in reviews_by_id]
    eligible = [r for r in members if r.quote_eligible]
    if not eligible:
        return []
    rows = [row_of[r.review_id] for r in members]
    centroid = np.asarray(matrix[rows].mean(axis=0)).ravel()  # type: ignore[index]
    norm = float(np.linalg.norm(centroid))
    if norm:
        centroid = centroid / norm

    spans: list[tuple[Review, int, int]] = [
        (review, start, end) for review in eligible for start, end in snippet_spans(review.text)
    ]
    if not spans:
        return []
    vectors = vectorizer.transform([review.text[s:e] for review, s, e in spans])
    similarities = np.asarray(vectors @ centroid).ravel()

    best: dict[str, _Candidate] = {}
    for (review, start, end), similarity in zip(spans, similarities.tolist(), strict=True):
        preferred = (
            PREFERRED_MIN_WORDS <= review.word_count <= PREFERRED_MAX_WORDS
            and not _PLACEHOLDER_RE.search(review.text[start:end])
        )
        candidate = _Candidate(
            review=review,
            theme_id=theme.theme_id,
            start=start,
            end=end,
            tier=0 if preferred else 1,
            similarity=float(similarity),
        )
        current = best.get(review.review_id)
        # Per review keep the best span: lower tier, higher similarity, earliest start.
        if current is None or (candidate.tier, -round(candidate.similarity, 9), start) < (
            current.tier,
            -round(current.similarity, 9),
            current.start,
        ):
            best[review.review_id] = candidate
    return sorted(best.values(), key=lambda c: c.sort_key)


def select_quotes(
    top_themes: Sequence[Theme], reviews: Sequence[Review], n: int = QUOTES_REQUIRED
) -> list[Quote]:
    """Exactly ``n`` verbatim quotes from the top themes (see module docstring)."""
    reviews_by_id = {r.review_id: r for r in reviews}
    vectorizer = tfidf_vectorizer()
    try:
        matrix = vectorizer.fit_transform([r.text for r in reviews])
    except ValueError:
        raise InsufficientDataError("no usable text to rank quote candidates") from None
    row_of = {r.review_id: i for i, r in enumerate(reviews)}
    pools = [
        _candidates_for(theme, reviews_by_id, vectorizer, row_of, matrix) for theme in top_themes
    ]

    selected: list[_Candidate] = []
    used: set[str] = set()
    cursors = [0] * len(pools)

    def take(pool_index: int) -> bool:
        pool = pools[pool_index]
        while cursors[pool_index] < len(pool):
            candidate = pool[cursors[pool_index]]
            cursors[pool_index] += 1
            if candidate.review.review_id not in used:
                used.add(candidate.review.review_id)
                selected.append(candidate)
                return True
        return False

    # First pass: one per theme in rank order. Then redistribute round-robin.
    for i in range(len(pools)):
        if len(selected) < n:
            take(i)
    while len(selected) < n:
        progressed = False
        for i in range(len(pools)):
            if len(selected) < n and take(i):
                progressed = True
        if not progressed:
            break
    if len(selected) < n:
        raise InsufficientDataError(
            f"only {len(selected)} quote-eligible review(s) in the top themes; "
            f"exactly {n} verified quotes are required"
        )
    return [c.to_quote() for c in selected]


# ---------------------------------------------------------------- verification
def quote_problems(
    quote: Quote,
    reviews_by_id: Mapping[str, Review],
    assignments: Mapping[str, str],
    raw_by_id: Mapping[str, RawReview],
) -> list[str]:
    """Every reason ``quote`` is not a verified verbatim span (empty means verified)."""
    review = reviews_by_id.get(quote.review_id)
    if review is None:
        return ["source review not found in reviews.jsonl"]
    problems = []
    if review.text[quote.start : quote.end] != quote.text:
        problems.append("text is not the verbatim span review.text[start:end]")
    if review.text_sha256 != sha256_text(review.text):
        problems.append("stored review text does not match its text_sha256")
    if assignments.get(review.review_id) != quote.theme_id:
        problems.append("source review is not a member of the quoted theme")
    if quote.rating != review.rating or quote.created_at != review.created_at:
        problems.append("rating or date differs from the source review")
    if not review.quote_eligible or contains_emoji(quote.text):
        problems.append("source review is not quote-eligible")
    if count_words(quote.text) > QUOTE_MAX_WORDS:
        problems.append(f"quote is longer than {QUOTE_MAX_WORDS} words")
    if quote.leading_ellipsis != (quote.start > 0) or quote.trailing_ellipsis != (
        quote.end < len(review.text)
    ):
        problems.append("ellipsis flags do not match the span")
    if scan_pii(quote.text):
        problems.append("quote contains unredacted personal data")
    if verify_provenance([review], raw_by_id):
        problems.append("provenance check failed: text does not re-derive from the raw store")
    return problems


def verify_quotes(
    quotes: Sequence[Quote],
    reviews_by_id: Mapping[str, Review],
    assignments: Mapping[str, str],
    raw_by_id: Mapping[str, RawReview],
    *,
    required: int = QUOTES_REQUIRED,
) -> None:
    """Raise :class:`QuoteVerificationError` unless exactly ``required`` quotes all verify."""
    problems = []
    if len(quotes) != required:
        problems.append(f"expected exactly {required} quotes, got {len(quotes)}")
    if len({q.review_id for q in quotes}) != len(quotes):
        problems.append("the same review is quoted more than once")
    for quote in quotes:
        problems += [
            f"{quote.review_id}: {p}"
            for p in quote_problems(quote, reviews_by_id, assignments, raw_by_id)
        ]
    if problems:
        raise QuoteVerificationError("Quote verification failed: " + "; ".join(problems))
