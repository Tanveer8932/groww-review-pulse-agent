"""A small English lexicon used as a second language signal and by the name redactor.

The statistical detector (``lingua``) spreads probability across its candidate languages,
so a short English review full of finance acronyms ("SIP", "KYC", "IPO") or cognates
("application", "excellent", "funds") can score just under the confidence threshold. This
module provides an independent check: the share of a review's ordinary words that are
known English words. Romanized Hindi words (``hai``, ``nahi``, ``paisa``) are not in the
lexicon, so code-mixed text scores low.

Three token classes:

* **English**: scikit-learn's ``ENGLISH_STOP_WORDS`` (function words) plus
  :data:`CONTENT_WORDS`, a curated vocabulary of everyday and app-review English, matched
  after light suffix stripping (``charges`` -> ``charge``, ``loading`` -> ``load``).
* **Neutral**: finance and product terms used unchanged in every Indian language
  (:data:`DOMAIN_TERMS`), all-caps acronyms, tokens containing digits, and redaction
  placeholders. They say nothing about the language and are not counted.
* **Other**: everything else, including romanized Hindi and non-English words.

The lexicon is intentionally a closed list: an unknown English word lowers the coverage,
which can only make the filter stricter, never looser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

#: Everyday and app-review English (root forms). Grouped loosely by topic for maintenance.
_CONTENT = """
    able absolutely accept access account accurate action active actual actually add
    address advance advice after again against agent alert allow almost alone already also
    alternative although always amazing amount analysis annoying another answer anymore
    anything anyway apply appreciate approve area around arrive ask asset attempt
    authentication auto automatic available average avoid aware awesome awful
    back bad balance bank banking basic beautiful beginner believe benefit best better big
    bit block bonus book boring borrow bought box brilliant broke broken broker brokerage
    bug build busy button buy buying
    calculate calculation call can cancel capital card care careful case cash cause
    certain chain change charge chart chat cheap check choice choose claim class clean
    clear clearly click client close collateral come comfortable commission company
    compare compatible complain complaint complete completely complex confuse confusing
    connect connection consider constant contact continue control convenient correct
    correctly cost could crash create credit credited cross current currently customer cut
    daily damage dark dashboard data date day deal debit debited decent decide deduct
    deducted delay delete delivery deposit design despite detail develop developer device
    difference different difficult disappoint disappointed disappointing discount display
    dividend document done double down download drop due during
    early earn easily easy effort either else email emergency end enough enter entire
    entry equity error especially even ever every everyday everything exact exactly
    example excellent except exchange execute executed execution exit expect expensive
    experience explain extra extremely
    face facility fact fail failure fair fake far fast feature fee feel few figure fill
    final finally finance financial find fine fingerprint first fix flexible flow follow
    font forever forget form found free freeze frequent frequently friend friendly full
    fully fund fundamental future futures
    gain game general genuine get give glad glitch go gold good great greek group grow
    growth guide guy
    half hand handle hang happen happy hard hate head hear help helpful hidden hide high
    history hold holding home honest hope horrible hour however huge
    idea ignore immediately important impossible improve improvement include income
    incorrect incorrectly increase index indicator information initial install instalment
    installment instant instantly insurance interest interesting interface intraday invest
    investing investment investor issue item
    job join just keep kind know knowledge
    lack large last late lately later latest launch learn least leave left less level
    life like limit link list little live load loading log login long look lose loss lost
    lot love low
    main maintain make manage mandate many margin market max maximum maybe mean minimum
    minute miss missing mistake mobile mode modify moment money month monthly morning
    mostly much multiple must mutual
    name nature near need negative net network never new news next nice night nobody
    normal note nothing notice notification now number
    offer office often okay old once one online only open opening option order original
    other otherwise overall own
    page paid part pathetic pay payment pending people per percent perfect perform
    performance period person phone pick place plan platform play please pledge pleasure
    plus point policy poor popular portfolio possible post power premium present pretty
    previous price problem process product profit progress proper properly provide purchase
    put
    quality quick quickly quit
    raise random rate rather ratio reach read ready real really reason receive received
    recent recently recommend redeem redemption reduce refund regular reject rejected
    release reliable remove repeat replace reply report request require research resolve
    respond response rest result return review right risk round rule run
    safe salary same save scam screen screener search second section secure security see
    seem sell selling send sense service session set setting setup share short show
    simple simply since single slow slowly small smooth solve solution someone something
    sometimes soon sorry speed spend stable staff start statement status stay step still
    stock stop store strange strategy stuck stupid submit success successful suddenly
    suggest suggestion super support suppose sure switch system
    take talk tap tax team technical tell term terrible test thank thanks thing think
    time today together tool top total track trade trader trading transaction transfer
    trigger triggering true trust try turn twice type
    unable understand unit unless until update upgrade use useful useless user usual
    usually
    valid value various verification verify version very view visible
    wait waiting wallet want warning waste watch watchlist way week weekly well whole
    window wise wish withdraw withdrawal within without wonderful word work worse worst
    worth would wow wrong
    year yes yesterday
    zero
    allotment application asap automatically basket match ticket allot
    am began begun came did does doing gave given goes going gone got gotten had has
    having knew known left made met said saw says seen sent sold spent stuck taken told
    took tried understood went won wrote
"""

#: Finance and product terms that appear unchanged in English and in code-mixed reviews.
_DOMAIN = """
    sip sips ipo ipos kyc etf etfs nav navs amc upi otp otps gtt mtf demat dematerialized
    nifty sensex banknifty finnifty groww zerodha upstox paytm phonepe gpay nse bse sebi
    cdsl nsdl pan aadhaar ui ux fno f&o xirr cagr ltp rms elss ppf nps smallcase
    mf mfs app apps
"""

CONTENT_WORDS: frozenset[str] = frozenset(_CONTENT.split())
DOMAIN_TERMS: frozenset[str] = frozenset(_DOMAIN.split())
FUNCTION_WORDS: frozenset[str] = frozenset(ENGLISH_STOP_WORDS)
ENGLISH_WORDS: frozenset[str] = CONTENT_WORDS | FUNCTION_WORDS

#: Tokens: words with internal apostrophes/ampersands/hyphens, or bracketed placeholders.
_TOKEN_RE = re.compile(r"\[[A-Z_]+\]|[A-Za-z0-9]+(?:['’&-][A-Za-z0-9]+)*")
_ACRONYM_RE = re.compile(r"[A-Z][A-Z&]{1,5}s?")
_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("ies", "y"),
    ("ied", "y"),
    ("ing", ""),
    ("ing", "e"),
    ("ed", ""),
    ("ed", "e"),
    ("es", ""),
    ("s", ""),
    ("ly", ""),
    ("er", ""),
    ("est", ""),
    ("ment", ""),
)


def is_english_word(word: str) -> bool:
    """True if ``word`` (any case) or its stripped stem is in the lexicon."""
    word = word.lower().replace("’", "'")
    if word in ENGLISH_WORDS:
        return True
    if "'" in word:  # don't, it's, user's
        stem = word.split("'", 1)[0]
        return stem in ENGLISH_WORDS or stem in {"don", "doesn", "didn", "isn", "wasn", "can"}
    for suffix, replacement in _SUFFIXES:
        stem = word[: -len(suffix)]
        if word.endswith(suffix) and len(stem) >= 2 and stem + replacement in ENGLISH_WORDS:
            return True
    # doubled consonant before -ing/-ed: "stopped", "shopping"
    return any(
        word.endswith(suffix)
        and len(stem := word[: -len(suffix)]) >= 3
        and stem[-1] == stem[-2]
        and stem[:-1] in ENGLISH_WORDS
        for suffix in ("ing", "ed")
    )


def is_neutral_token(token: str) -> bool:
    """Language-neutral: placeholders, numbers, acronyms, and finance/product terms."""
    return (
        token.startswith("[")
        or any(ch.isdigit() for ch in token)
        or bool(_ACRONYM_RE.fullmatch(token))
        or token.lower() in DOMAIN_TERMS
    )


@dataclass(frozen=True)
class LexicalCoverage:
    english: int
    other: int
    neutral: int

    @property
    def counted(self) -> int:
        return self.english + self.other

    @property
    def share(self) -> float:
        """English share of the counted (non-neutral) tokens; 0.0 when none are counted."""
        return self.english / self.counted if self.counted else 0.0


def lexical_coverage(text: str) -> LexicalCoverage:
    english = other = neutral = 0
    for token in _TOKEN_RE.findall(text):
        if is_neutral_token(token):
            neutral += 1
        elif is_english_word(token):
            english += 1
        else:
            other += 1
    return LexicalCoverage(english=english, other=other, neutral=neutral)
