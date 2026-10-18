"""English-only language filter (architecture §6.3, step 5; FR-6; assumption A7).

Policy, applied in this order (the first rule that fires decides):

1. ``non_latin_script``: any letter outside the Latin script (Devanagari, Tamil, Urdu, ...)
   excludes the review. Mixed-script reviews are therefore excluded too.
2. ``hinglish``: two or more tokens from a list of common romanized-Hindi words that are
   not English words (``hai``, ``nahi``, ``bahut``, ...). Romanized Hindi has no detector
   model, so the detector alone can mislabel it.
3. ``not_english``: the detector's most likely language is not English.
4. English is most likely and its confidence is at least ``LANG_MIN_CONFIDENCE`` (default
   0.80): kept (``method="detector"``).
5. **Lexical rescue**: English is most likely but below the threshold. The review is kept
   (``method="lexical_rescue"``) only if an independent signal agrees: the English
   confidence is at least :data:`RESCUE_MIN_CONFIDENCE` (English is more likely than every
   other candidate combined), at least :data:`RESCUE_MIN_TOKENS` ordinary words are present,
   and at least :data:`RESCUE_MIN_COVERAGE` of them are known English words
   (:mod:`groww_pulse.preprocessing.lexicon`; finance terms such as SIP, KYC, and IPO and
   all-caps acronyms are neutral). Romanized Hindi words are not in the lexicon, so
   code-mixed text cannot pass this way.
6. ``low_confidence``: otherwise.

Why the rescue exists: the detector's confidence is a share across 16 candidates, so short
English reviews with acronyms or cognates ("application", "funds") can fall just under 0.80
("Nice app for SIP and stocks, the charts are clean and fast to load" scores 0.789).
Lowering the threshold globally would also admit Hinglish that the detector misreads as
English (up to 0.62 on the evaluation corpus); requiring a second, independent signal does
not. The evaluation and its tradeoffs are recorded in docs/implementation-plan.md, Phase 2.

The marker count and the lexical coverage are computed on the PII-redacted text, so the
Phase 2 output gate, which only sees redacted text, re-derives exactly the same rescue
decision. Nothing is translated.

The detector (``lingua``) is restricted to English, the major Indian languages, languages
that commonly absorb romanized Hindi (Indonesian, Malay, Tagalog), and a few European
languages. Restricting the candidate set keeps results deterministic and fast; Tagalog in
particular attracts low-marker Hinglish, which keeps it from ranking English first.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass

from lingua import Language, LanguageDetector, LanguageDetectorBuilder

from groww_pulse.preprocessing.lexicon import lexical_coverage
from groww_pulse.preprocessing.pii import redact

CANDIDATE_LANGUAGES: tuple[Language, ...] = (
    Language.ENGLISH,
    Language.HINDI,
    Language.MARATHI,
    Language.BENGALI,
    Language.GUJARATI,
    Language.PUNJABI,
    Language.TAMIL,
    Language.TELUGU,
    Language.URDU,
    Language.INDONESIAN,
    Language.MALAY,
    Language.TAGALOG,
    Language.SPANISH,
    Language.FRENCH,
    Language.GERMAN,
    Language.PORTUGUESE,
)

#: Common romanized-Hindi words that are not also common English words. Ambiguous tokens
#: ("main", "me", "to", "par", "hi", "so", "hum", "jab", "tab", "pe", "ho", "fir", "hue",
#: "ab", "mast") are deliberately absent.
_HINGLISH_WORDS = """
    hai hain hoga hogi hota hoti hote tha thi nahi nahin nhi nai bahut bohot bhut
    kya kyu kyun kyon mein mujhe mujhko mera meri mere tera teri tumhara aap aapka apna
    accha acha achha achchha yaar yar bhai bhi aur lekin magar sab sabse koi kuch kuchh
    karo karna karte karta karti kiya kiye raha rahi rahe rha rhi gaya gayi gaye diya
    diye liya liye jata jati jate chahiye bekar bakwas bekaar ekdum sahi wala wali wale
    paisa paise kab kaise kaisa kitna kitne abhi yeh woh ye wo humko hame humara
    ko ka ki ke se ne kr rhe krna krte
    hua hui aaya aayi aaye mila milta milti milega milegi lagta lagte lagti lagega
    kal aaj tak baad pehle phir kyunki toh isko usko iska uska iski uski isme usme
    maine humne aapne kar karke karne karunga karenge dekha dekho dikh dikhta dikhti
    samajh pata bina dusre dusra kabhi hamesha rupaye aasan asaan dikkat pareshani jaldi
    kaafi kafi bura achhi acchi badiya badhiya bilkul turant pura puri sirf kaam kam ji
    zyada jyada theek thik galat ekdam bohot shuru aaram warna din
"""
HINGLISH_MARKERS: frozenset[str] = frozenset(_HINGLISH_WORDS.split())

#: Markers must be whole words: a letter run joined to digits ("5paisa", a broker name) is
#: one alphanumeric token, so it never matches the marker inside it.
_TOKEN_RE = re.compile(r"[a-z0-9]+")
HINGLISH_MIN_MARKERS = 2

#: Lexical-rescue parameters (rule 5). See the module docstring and the Phase 2 evaluation.
RESCUE_MIN_CONFIDENCE = 0.50
RESCUE_MIN_COVERAGE = 0.85
RESCUE_MIN_TOKENS = 6

METHOD_DETECTOR = "detector"
METHOD_LEXICAL_RESCUE = "lexical_rescue"


@dataclass(frozen=True)
class LanguageDecision:
    keep: bool
    lang: str | None
    confidence: float
    reason: str | None = None
    method: str | None = None  # how a kept review was accepted (METHOD_*)


@functools.cache
def _detector() -> LanguageDetector:
    return LanguageDetectorBuilder.from_languages(*CANDIDATE_LANGUAGES).build()


def has_non_latin_letters(text: str) -> bool:
    return any(ch.isalpha() and not unicodedata.name(ch, "").startswith("LATIN") for ch in text)


def hinglish_marker_count(text: str) -> int:
    return sum(1 for token in _TOKEN_RE.findall(text.lower()) if token in HINGLISH_MARKERS)


def lexical_rescue_ok(text: str, confidence: float) -> bool:
    """Rule 5: does the lexical signal corroborate a below-threshold English decision?

    ``text`` may be raw or redacted: the check always runs on the redacted form, so the
    pipeline and the output gate reach the same answer.
    """
    if confidence < RESCUE_MIN_CONFIDENCE:
        return False
    screened = redact(text).text
    if hinglish_marker_count(screened) >= HINGLISH_MIN_MARKERS:
        return False
    coverage = lexical_coverage(screened)
    return coverage.counted >= RESCUE_MIN_TOKENS and coverage.share >= RESCUE_MIN_COVERAGE


def english_confidence_ok(
    text: str, confidence: float, min_confidence: float, *, lexical_rescue: bool = True
) -> bool:
    """True if a review stored as English with ``confidence`` satisfies rules 4 or 5."""
    return confidence >= min_confidence or (lexical_rescue and lexical_rescue_ok(text, confidence))


def detect_english(
    text: str, min_confidence: float, *, lexical_rescue: bool = True
) -> LanguageDecision:
    """Apply the English-only policy described in the module docstring.

    ``lexical_rescue=False`` (``LANG_LEXICAL_RESCUE=false``) disables rule 5, so only the
    detector threshold decides.
    """
    if has_non_latin_letters(text):
        return LanguageDecision(False, None, 0.0, "non_latin_script")
    if hinglish_marker_count(redact(text).text) >= HINGLISH_MIN_MARKERS:
        return LanguageDecision(False, None, 0.0, "hinglish")

    values = _detector().compute_language_confidence_values(text)
    if not values:  # pragma: no cover — lingua returns all candidates for non-empty text
        return LanguageDecision(False, None, 0.0, "not_english")
    top = values[0]
    english = next((v.value for v in values if v.language == Language.ENGLISH), 0.0)
    confidence = round(english, 4)
    if top.language != Language.ENGLISH:
        return LanguageDecision(False, _iso(top.language), round(top.value, 4), "not_english")
    # Compare the stored (rounded) value, as the output gate does.
    if confidence >= min_confidence:
        return LanguageDecision(True, "en", confidence, method=METHOD_DETECTOR)
    if lexical_rescue and lexical_rescue_ok(text, confidence):
        return LanguageDecision(True, "en", confidence, method=METHOD_LEXICAL_RESCUE)
    return LanguageDecision(False, "en", confidence, "low_confidence")


def _iso(language: Language) -> str:
    return language.iso_code_639_1.name.lower()
