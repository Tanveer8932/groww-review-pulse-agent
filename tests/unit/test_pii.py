# SYNTHETIC TEST FIXTURE — not real review data
"""PII redaction: table-driven corpus, checksums, idempotency, and text preservation.

Every identifier below is fabricated for testing; checksum-valid numbers are generated.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from groww_pulse.preprocessing.pii import (
    PLACEHOLDERS,
    luhn_valid,
    redact,
    scan_pii,
    verhoeff_valid,
)
from tests.fixtures.preprocessing import luhn_number, verhoeff_number

AADHAAR = verhoeff_number("23456789012")
AADHAAR_BAD = AADHAAR[:-1] + str((int(AADHAAR[-1]) + 1) % 10)
AADHAAR_GROUPED = f"{AADHAAR[:4]} {AADHAAR[4:8]} {AADHAAR[8:]}"
AADHAAR_HYPHEN = f"{AADHAAR[:4]}-{AADHAAR[4:8]}-{AADHAAR[8:]}"
CARD = luhn_number("411111111111111")  # 16 digits
CARD_GROUPED = f"{CARD[:4]} {CARD[4:8]} {CARD[8:12]} {CARD[12:]}"
AMEX = luhn_number("37828224631000")  # 15 digits
AMEX_GROUPED = f"{AMEX[:4]} {AMEX[4:10]} {AMEX[10:]}"

# (text, expected redacted text, expected counts)
POSITIVE: list[tuple[str, str, dict[str, int]]] = [
    # email
    ("mail me at a.b@example.com today", "mail me at [EMAIL] today", {"EMAIL": 1}),
    ("contact USER+tag@mail.example.co.in.", "contact [EMAIL].", {"EMAIL": 1}),
    ("x_y-z@sub-domain.org,thanks", "[EMAIL],thanks", {"EMAIL": 1}),
    ("two: a@b.io and c@d.in", "two: [EMAIL] and [EMAIL]", {"EMAIL": 2}),
    # phone
    ("call 9876543210 now", "call [PHONE] now", {"PHONE": 1}),
    ("call +91 9876543210.", "call [PHONE].", {"PHONE": 1}),
    ("call +91-98765-43210", "call [PHONE]", {"PHONE": 1}),
    ("call 98765 43210 please", "call [PHONE] please", {"PHONE": 1}),
    ("call 987-654-3210", "call [PHONE]", {"PHONE": 1}),
    ("call 09876543210", "call [PHONE]", {"PHONE": 1}),
    ("call 919876543210", "call [PHONE]", {"PHONE": 1}),
    ("phone:6123456789", "phone:[PHONE]", {"PHONE": 1}),
    ("(7012345678)", "([PHONE])", {"PHONE": 1}),
    ("call 0091 9876543210", "call [PHONE]", {"PHONE": 1}),
    ("intl +44 20 7946 0958 ok", "intl [PHONE] ok", {"PHONE": 1}),
    ("intl +1-415-555-0100", "intl [PHONE]", {"PHONE": 1}),
    # PAN
    ("my PAN ABCDE1234F", "my PAN [PAN]", {"PAN": 1}),
    ("pan abcpe1234k, wrong", "pan [PAN], wrong", {"PAN": 1}),
    ("(PAN:ZZZPZ9999Z)", "(PAN:[PAN])", {"PAN": 1}),
    # Aadhaar
    (f"aadhaar {AADHAAR}", "aadhaar [AADHAAR]", {"AADHAAR": 1}),
    (f"aadhaar {AADHAAR_GROUPED}.", "aadhaar [AADHAAR].", {"AADHAAR": 1}),
    (f"aadhaar {AADHAAR_HYPHEN}", "aadhaar [AADHAAR]", {"AADHAAR": 1}),
    # card (Luhn)
    (f"card {CARD}", "card [CARD]", {"CARD": 1}),
    (f"card {CARD_GROUPED}!", "card [CARD]!", {"CARD": 1}),
    (f"amex {AMEX_GROUPED}", "amex [CARD]", {"CARD": 1}),
    (f"amex {AMEX}", "amex [CARD]", {"CARD": 1}),
    # account numbers
    ("a/c 123456789 debited", "a/c [ACCOUNT_NO] debited", {"ACCOUNT_NO": 1}),
    ("account 50100123456780", "account [ACCOUNT_NO]", {"ACCOUNT_NO": 1}),
    ("acct 123456789012345678", "acct [ACCOUNT_NO]", {"ACCOUNT_NO": 1}),
    (f"aadhaar? {AADHAAR_BAD}", "aadhaar? [ACCOUNT_NO]", {"ACCOUNT_NO": 1}),
    ("grouped 1234 5678 9012", "grouped [ACCOUNT_NO]", {"ACCOUNT_NO": 1}),
    # UPI
    ("pay rahul@okhdfcbank now", "pay [UPI_ID] now", {"UPI_ID": 1}),
    ("upi 9876543210@ybl.", "upi [UPI_ID].", {"UPI_ID": 1}),
    ("upi first.last@oksbi", "upi [UPI_ID]", {"UPI_ID": 1}),
    # URL
    ("see https://example.com/a?b=1.", "see [URL].", {"URL": 1}),
    ("see http://example.org", "see [URL]", {"URL": 1}),
    ("see www.example.in/path, ok", "see [URL], ok", {"URL": 1}),
    ("(https://example.com/x)", "([URL])", {"URL": 1}),
    # names and handles
    ("my name is Rahul Sharma and I", "my name is [NAME] and I", {"NAME": 1}),
    ("My name is Priya.", "My name is [NAME].", {"NAME": 1}),
    ("ping @some_user please", "ping [NAME] please", {"NAME": 1}),
    ("thanks @support.team.", "thanks [NAME].", {"NAME": 1}),
    # several types in one review
    (
        "I am Rahul, my name is Rahul, mail r@x.com or call 9876543210",
        "I am Rahul, my name is [NAME], mail [EMAIL] or call [PHONE]",
        {"EMAIL": 1, "NAME": 1, "PHONE": 1},
    ),
    # names (rules v2): lowercase, relatives, honorifics, self-introductions, agents, sign-offs
    ("my name is rahul sharma and I", "my name is [NAME] and I", {"NAME": 1}),
    ("My father's name is Ramesh Kumar.", "My father's name is [NAME].", {"NAME": 1}),
    ("my name: Asha, please help", "my name: [NAME], please help", {"NAME": 1}),
    (
        "MY NAME IS VIKRAM and support ignores me",
        "MY NAME IS [NAME] and support ignores me",
        {"NAME": 1},
    ),
    ("Mr. Sharma from support was rude", "Mr. [NAME] from support was rude", {"NAME": 1}),
    ("Smt Lakshmi Devi opened the account", "Smt [NAME] opened the account", {"NAME": 1}),
    ("Myself Rakesh Verma, using this app", "Myself [NAME], using this app", {"NAME": 1}),
    ("an executive named Priya closed it", "an executive named [NAME] closed it", {"NAME": 1}),
    ("support agent Kavya was helpful", "support agent [NAME] was helpful", {"NAME": 1}),
    ("Please fix this. Regards, Anil Kumar", "Please fix this. Regards, [NAME]", {"NAME": 1}),
    ("Fix it soon. Thanks, Meera!", "Fix it soon. Thanks, [NAME]!", {"NAME": 1}),
    # obfuscated email
    ("mail rahul.k at gmail dot com today", "mail [EMAIL] today", {"EMAIL": 1}),
    ("write to asha[at]example[dot]in", "write to [EMAIL]", {"EMAIL": 1}),
    ("id: dev (at) mailhost (dot) org", "id: [EMAIL]", {"EMAIL": 1}),
    # addresses
    (
        "my address is 12 MG Road, Indiranagar. KYC failed",
        "my address is [ADDRESS]. KYC failed",
        {"ADDRESS": 1},
    ),
    ("I live at 45 Park Street", "I live at [ADDRESS]", {"ADDRESS": 1}),
    ("flat no 302 was entered wrongly", "[ADDRESS] was entered wrongly", {"ADDRESS": 1}),
    ("H.No. 4-5-6 is not accepted in KYC", "[ADDRESS] is not accepted in KYC", {"ADDRESS": 1}),
    ("pincode 560001 not accepted", "pincode [ADDRESS] not accepted", {"ADDRESS": 1}),
    ("PIN code: 110 001 is valid", "PIN code: [ADDRESS] is valid", {"ADDRESS": 1}),
    # account identifiers given with context
    ("my client id is AB1234 and", "my client id is [ACCOUNT_NO] and", {"ACCOUNT_NO": 1}),
    ("UCC 7XK23 is blocked", "UCC [ACCOUNT_NO] is blocked", {"ACCOUNT_NO": 1}),
    ("DP ID IN30012345678901 shown", "DP ID [ACCOUNT_NO] shown", {"ACCOUNT_NO": 1}),
    ("NSDL IN301234 12345678 wrong", "NSDL [ACCOUNT_NO] wrong", {"ACCOUNT_NO": 1}),
    # landlines with an STD code
    ("landline 022-23456789 is dead", "landline [PHONE] is dead", {"PHONE": 1}),
    ("call 080 2345 6789 now", "call [PHONE] now", {"PHONE": 1}),
]

NEGATIVE: list[str] = [
    "Lost ₹10,000 in one day",
    "Invested Rs 1,00,000 and Rs. 2,50,000.50",
    "Paid 1,234,567,890 in total",
    "Order 12345678 failed",
    "On 15-09-2026 at 10:30 the app froze",
    "On 2026-09-15T10:30:00Z it crashed",
    "Version 13.42.1 is slow",
    "Nifty at 24500.75 and Sensex 80123.45",
    "Brokerage 0.03% per trade",
    "Rating 5/5 and 4.5 stars",
    "Price 12.3456789012 per unit",
    "Call the support at 1800 for help",
    "Email support is slow",
    "The @ sign is broken",
    "my name is not important here",
    "I am Rahul and I like it",
    "Groww.in website is fine",
    "3 days and 24 hours wasted",
    "OTP 482913 came late",
    "ID ABCD1234 in the error",
    "SIP of 5000 every month",
    "Up 120% then down 45%",
    # false-positive guards for the v2 rules
    "my name is not showing in the profile",
    "My name is wrong in the KYC form",
    "an agent named Support Bot replied",
    "the executive Please call back never came",
    "Thanks Groww for the quick refund",
    "Thanks, Great app for beginners",
    "Regards to the whole team, keep improving",
    "I live in Pune and invest every month",
    "my address is updated but KYC still pending",
    "look at this dot com style bubble in small caps",
    "pin 1234 is not working after the update",
    "MPIN reset failed again today",
    "toll free 1800 103 4567 never answers",
    "Ticket 45678 is still pending",
    "User interface is bad and trading account is blocked",
    "customer number one complaint is delays",
    "room for improvement in the charts",
    "No. 1 app in India for beginners",
    "door step KYC was quick",
    "Mutual funds at 9:15 dot on the opening bell",
    "My SIP name is Retirement Fund and it works",
]


@pytest.mark.parametrize(("text", "expected", "counts"), POSITIVE)
def test_positive_cases(text: str, expected: str, counts: dict[str, int]) -> None:
    result = redact(text)
    assert result.text == expected
    assert result.counts == counts


@pytest.mark.parametrize("text", NEGATIVE)
def test_negative_cases(text: str) -> None:
    result = redact(text)
    assert result.text == text
    assert result.counts == {}
    assert scan_pii(text) == {}


def test_corpus_sizes_meet_plan() -> None:
    assert len(POSITIVE) >= 40
    assert len(NEGATIVE) >= 20
    labels = {label for _, _, counts in POSITIVE for label in counts}
    assert labels == set(PLACEHOLDERS)  # every placeholder type has a positive case


def test_checksums() -> None:
    assert verhoeff_valid(AADHAAR)
    assert not verhoeff_valid(AADHAAR_BAD)
    assert verhoeff_valid("2363")  # published Verhoeff example
    assert luhn_valid("79927398713")  # published Luhn example
    assert not luhn_valid("79927398710")


def test_aadhaar_valid_vs_invalid_checksum() -> None:
    assert redact(f"id {AADHAAR}").counts == {"AADHAAR": 1}
    assert "AADHAAR" not in redact(f"id {AADHAAR_BAD}").counts


def test_sixteen_digits_failing_luhn_is_account_not_card() -> None:
    bad_card = CARD[:-1] + str((int(CARD[-1]) + 1) % 10)
    assert redact(f"num {bad_card}").counts == {"ACCOUNT_NO": 1}


def test_redaction_preserves_every_non_pii_character() -> None:
    text = "Hi, my name is Asha. Email asha@example.com; phone 9876543210! Thanks :)"
    result = redact(text)
    pattern = "|".join(re.escape(f"[{p}]") for p in PLACEHOLDERS)
    kept_segments = re.split(pattern, result.text)
    assert kept_segments == ["Hi, my name is ", ". Email ", "; phone ", "! Thanks :)"]
    # every kept segment appears, in order, in the original
    position = 0
    for segment in kept_segments:
        position = text.index(segment, position) + len(segment)


def test_scan_reports_leftovers() -> None:
    assert scan_pii("reach me at a@b.com") == {"EMAIL": 1}
    assert scan_pii("reach me at [EMAIL]") == {}


PII_SAMPLES = st.sampled_from(
    [
        "a.b@example.com",
        "9876543210",
        "+91 98765 43210",
        "ABCDE1234F",  # pragma: allowlist secret (fabricated PAN)
        AADHAAR_GROUPED,
        CARD,
        "rahul@okhdfcbank",
        "https://example.com/x",
        "@handle",
        "my name is Ravi Kumar",
        "123456789012345",
        "my name is ravi",
        "MY NAME IS A",
        "Mr Sharma",
        "Regards, Anil",
        "flat no 12B",
        "pincode 560001",
        "my address is 4 Main Road",
        "client id AB12345",
        "022-23456789",
        "asha[at]example[dot]in",
        "x at gmail dot com",
    ]
)
WORDS = st.text(
    alphabet=st.characters(categories=["L", "N", "P", "Zs", "S"]), min_size=0, max_size=40
)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.one_of(WORDS, PII_SAMPLES), max_size=8))
def test_redaction_is_idempotent(parts: list[str]) -> None:
    text = " ".join(parts)
    once = redact(text).text
    assert redact(once).text == once
    assert scan_pii(once) == {}


#: Words that can start a letters-only redaction (name and obfuscated-email cues).
_LETTER_CUES = re.compile(
    r"(?i)\b(?:name|mr|mrs|ms|shri|smt|sri|myself|named|agent|executive|representative"
    r"|regards|thanks|thank|sincerely|at)\b"
)


@settings(max_examples=200, deadline=None)
@given(st.text(alphabet=st.characters(categories=["L", "Zs"]), max_size=200))
def test_letters_and_spaces_only_text_is_never_changed_unless_cued(text: str) -> None:
    result = redact(text)
    if not _LETTER_CUES.search(text):
        assert result.text == text
