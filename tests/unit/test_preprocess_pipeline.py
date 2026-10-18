# SYNTHETIC TEST FIXTURE — not real review data
"""End-to-end preprocessing: funnel, output integrity, provenance, determinism, empty input."""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from groww_pulse.config import EmojiPolicy, Settings, load_settings
from groww_pulse.errors import PreprocessingError
from groww_pulse.models import Review
from groww_pulse.preprocessing.emoji_policy import contains_emoji
from groww_pulse.preprocessing.normalize import count_words
from groww_pulse.preprocessing.pii import scan_pii
from groww_pulse.preprocessing.pipeline import (
    PreprocessReport,
    load_analysis_reviews,
    review_problems,
    run_preprocessing,
    verify_provenance,
)
from groww_pulse.storage.jsonl_store import JsonlReviewStore
from tests.fixtures.preprocessing import raw_review, write_raw_store
from tests.fixtures.synthetic import AS_OF, OUT_OF_WINDOW

EN = "the app crashes every time I open my portfolio after the latest update"


def corpus() -> list[Any]:
    """One synthetic review per pipeline outcome (all texts carry the fixture prefix)."""
    return [
        raw_review("keep-1", EN),
        raw_review("keep-2", "withdrawal to my bank account took five working days this month"),
        raw_review(
            "keep-pii",
            "support asked me to email a.b@example.com and call 9876543210 about my stuck order",
        ),
        raw_review("old", EN + " again", created_at=OUT_OF_WINDOW),
        raw_review("short", "bad app", prefix=True),  # 4 words with prefix
        raw_review("emoji", "great app for investing \U0001f44d works well every single day"),
        raw_review("hindi", "यह ऐप बहुत अच्छा है लेकिन निकासी में बहुत समय लगता है"),
        raw_review("hinglish", "app bahut slow hai and customer care koi response nahi deta"),
        raw_review(
            "portuguese",
            "este aplicativo é muito bom para investir e fácil de usar todos os dias",
            prefix=False,
        ),
        raw_review("dup-exact", EN.upper(), created_at=datetime(2026, 9, 16, tzinfo=UTC)),
        raw_review(
            "dup-near",
            EN.replace("crashes", "crashes,"),
            created_at=datetime(2026, 9, 17, tzinfo=UTC),
        ),
        # 9 words before redaction, URL tokens collapse into one placeholder afterwards
        raw_review(
            "short-after", "the app is broken, read https://example.com/help/orders/failed/again"
        ),
        raw_review("other-app", EN + " too", app_id="com.example.other"),
    ]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(env_file=None, data_dir=tmp_path / "data", log_level="WARNING")


def run(settings: Settings, run_id: str = "test-run") -> PreprocessReport:
    return run_preprocessing(settings, as_of=AS_OF, run_id=run_id, allow_synthetic=True)


def steps(report: PreprocessReport) -> dict[str, dict[str, Any]]:
    return {s["step"]: s for s in report.as_dict()["steps"]}


def read_output(settings: Settings) -> list[Review]:
    path = settings.data_dir / "reviews.jsonl"
    return [Review.model_validate_json(line) for line in path.read_text("utf-8").splitlines()]


def test_full_funnel(settings: Settings) -> None:
    write_raw_store(settings.data_dir, corpus(), extra_lines=["{not json"])
    report = run(settings)
    by_step = steps(report)

    assert by_step["validate"]["input"] == 14
    assert by_step["validate"]["reasons"] == {"invalid_record": 1, "wrong_app_id": 1}
    assert by_step["window"]["reasons"] == {"outside_window": 1}
    assert by_step["length"]["reasons"] == {"too_few_words": 1}
    assert by_step["emoji"]["reasons"] == {"contains_emoji": 1}
    assert by_step["language"]["reasons"] == {
        "hinglish": 1,
        "non_latin_script": 1,
        "not_english": 1,
    }
    assert by_step["dedupe"]["reasons"] == {"exact_duplicate": 1, "near_duplicate": 1}
    assert by_step["pii_redaction"]["dropped"] == 0
    assert by_step["length_recheck"]["reasons"] == {"too_few_words_after_redaction": 1}
    assert by_step["output_gate"]["dropped"] == 0

    assert report.output_count == 3
    assert [r.review_id for r in read_output(settings)] == ["keep-1", "keep-2", "keep-pii"]
    assert report.pii_redactions == {"EMAIL": 1, "PHONE": 1, "URL": 1}
    assert report.reviews_redacted == 2


def test_funnel_reconciles_and_is_persisted(settings: Settings) -> None:
    write_raw_store(settings.data_dir, corpus())
    report = run(settings)
    data = report.as_dict()
    chain = data["steps"]
    for previous, current in itertools.pairwise(chain):
        assert current["input"] == previous["output"]
    for step in chain:
        assert step["input"] - step["dropped"] == step["output"]
        assert sum(step["reasons"].values()) == step["dropped"]
    assert data["input"] - data["total_dropped"] == data["output"]

    funnel_path = settings.data_dir / "runs" / "test-run" / "funnel.json"
    assert json.loads(funnel_path.read_text("utf-8")) == data
    assert (
        data["output_sha256"]
        == hashlib.sha256((settings.data_dir / "reviews.jsonl").read_bytes()).hexdigest()
    )


def test_output_integrity(settings: Settings) -> None:
    write_raw_store(settings.data_dir, corpus())
    run(settings)
    reviews = read_output(settings)
    raw_by_id = JsonlReviewStore(settings.data_dir / "raw" / "reviews_raw.jsonl").load()
    assert reviews
    for review in reviews:
        assert count_words(review.text) > settings.min_words_exclusive
        assert review.word_count == count_words(review.text)
        assert review.lang == "en"
        assert review.lang_confidence is not None
        assert review.lang_confidence >= settings.lang_min_confidence
        assert not contains_emoji(review.text)
        assert scan_pii(review.text) == {}
        assert review.text_sha256 == hashlib.sha256(review.text.encode()).hexdigest()
        assert review.original_sha256 == raw_by_id[review.review_id].original_sha256
        assert review.provenance == raw_by_id[review.review_id].provenance
        assert review.quote_eligible
    assert len({r.review_id for r in reviews}) == len(reviews)
    assert verify_provenance(reviews, raw_by_id) == []


def test_redaction_changes_only_pii(settings: Settings) -> None:
    write_raw_store(settings.data_dir, corpus())
    run(settings)
    review = next(r for r in read_output(settings) if r.review_id == "keep-pii")
    assert review.text == (
        "SYNTHETIC FIXTURE support asked me to email [EMAIL] and call [PHONE] about my stuck order"
    )
    assert review.pii_redactions == {"EMAIL": 1, "PHONE": 1}
    # the original text never reaches reviews.jsonl
    output = (settings.data_dir / "reviews.jsonl").read_text("utf-8")
    assert "a.b@example.com" not in output
    assert "9876543210" not in output


def test_unredacted_text_only_in_raw_store(settings: Settings) -> None:
    write_raw_store(settings.data_dir, corpus())
    report = run(settings)
    funnel = (settings.data_dir / "runs" / "test-run" / "funnel.json").read_text("utf-8")
    assert "a.b@example.com" not in funnel
    assert "portfolio" not in funnel  # funnel holds counts, never review text
    assert report.output_count == 3


def test_provenance_detects_tampering(settings: Settings) -> None:
    write_raw_store(settings.data_dir, corpus())
    run(settings)
    reviews = read_output(settings)
    raw_by_id = JsonlReviewStore(settings.data_dir / "raw" / "reviews_raw.jsonl").load()
    tampered = reviews[0].model_copy(update={"text": reviews[0].text.replace("app", "ap")})
    assert verify_provenance([tampered], raw_by_id) == [tampered.review_id]
    assert verify_provenance(reviews[:1], {}) == [reviews[0].review_id]


def test_deterministic_byte_identical_output(settings: Settings) -> None:
    write_raw_store(settings.data_dir, corpus())
    out = settings.data_dir / "reviews.jsonl"
    first = run(settings, "run-a")
    first_bytes = out.read_bytes()
    second = run(settings, "run-b")
    assert out.read_bytes() == first_bytes
    assert first.output_sha256 == second.output_sha256
    assert steps(first) == steps(second)


@pytest.mark.parametrize("create", [False, True])
def test_empty_dataset(settings: Settings, create: bool) -> None:
    if create:
        write_raw_store(settings.data_dir, [])
    report = run(settings)
    assert report.empty
    assert report.output_count == 0
    assert all(s.input == 0 and s.dropped == 0 for s in report.steps)
    assert (settings.data_dir / "reviews.jsonl").read_bytes() == b""
    assert any("empty" in w for w in report.warnings)
    assert ("raw store not found" in " ".join(report.warnings)) is not create
    assert load_analysis_reviews(settings.data_dir / "reviews.jsonl", settings) == []


def test_empty_result_overwrites_stale_output(settings: Settings) -> None:
    write_raw_store(settings.data_dir, corpus())
    run(settings)
    assert read_output(settings)
    write_raw_store(settings.data_dir, [raw_review("short", "bad app")])
    run(settings)
    assert (settings.data_dir / "reviews.jsonl").read_bytes() == b""


def test_window_is_reapplied(settings: Settings) -> None:
    write_raw_store(
        settings.data_dir,
        [
            raw_review("first-day", EN, created_at=datetime(2026, 7, 23, 0, 0, tzinfo=UTC)),
            raw_review(
                "before", EN + " again", created_at=datetime(2026, 7, 22, 23, 59, tzinfo=UTC)
            ),
        ],
    )
    report = run(settings)
    assert steps(report)["window"]["reasons"] == {"outside_window": 1}
    assert [r.review_id for r in read_output(settings)] == ["first-day"]
    eight_weeks = run_preprocessing(
        settings, as_of=AS_OF, weeks=8, run_id="w8", allow_synthetic=True
    )
    assert eight_weeks.output_count == 0


def test_hash_mismatch_is_dropped(settings: Settings) -> None:
    good = raw_review("ok", EN)
    bad = raw_review("bad", EN + " changed").model_copy(update={"text": EN + " edited"})
    write_raw_store(settings.data_dir, [good, bad])
    report = run(settings)
    assert steps(report)["validate"]["reasons"] == {"hash_mismatch": 1}
    assert report.output_count == 1


def test_whitespace_only_text_is_dropped(settings: Settings) -> None:
    write_raw_store(settings.data_dir, [raw_review("blank", " ​ ", prefix=False)])
    report = run(settings)
    assert steps(report)["validate"]["reasons"] == {"empty_text": 1}


def test_lenient_policy_marks_quote_ineligible(tmp_path: Path) -> None:
    settings = load_settings(
        env_file=None, data_dir=tmp_path / "data", emoji_policy="lenient", log_level="WARNING"
    )
    write_raw_store(
        settings.data_dir,
        [
            raw_review("emoji", "great app for investing \U0001f44d works well every single day"),
            raw_review("plain", EN),
        ],
    )
    report = run(settings)
    by_id = {r.review_id: r for r in read_output(settings)}
    assert set(by_id) == {"emoji", "plain"}
    assert by_id["emoji"].quote_eligible is False
    assert by_id["emoji"].text.endswith("\U0001f44d works well every single day")  # unaltered
    assert by_id["plain"].quote_eligible is True
    assert report.quote_ineligible == 1


def test_synthetic_data_is_refused_without_opt_in(settings: Settings) -> None:
    write_raw_store(settings.data_dir, corpus())
    with pytest.raises(PreprocessingError, match="synthetic test fixture"):
        run_preprocessing(settings, as_of=AS_OF, run_id="prod")
    assert not (settings.data_dir / "reviews.jsonl").exists()


# ---------------------------------------------------------------- analysis gate


def _write_reviews(settings: Settings, reviews: list[Review]) -> Path:
    path = settings.data_dir / "reviews.jsonl"
    path.write_text("".join(r.model_dump_json() + "\n" for r in reviews), encoding="utf-8")
    return path


def _valid_review(settings: Settings) -> Review:
    write_raw_store(settings.data_dir, [raw_review("keep-1", EN)])
    run(settings)
    return read_output(settings)[0]


def test_gate_accepts_pipeline_output(settings: Settings) -> None:
    review = _valid_review(settings)
    loaded = load_analysis_reviews(
        settings.data_dir / "reviews.jsonl", settings, allow_synthetic=True
    )
    assert loaded == [review]


def test_gate_refuses_synthetic_by_default(settings: Settings) -> None:
    _valid_review(settings)
    with pytest.raises(PreprocessingError, match="synthetic test fixture"):
        load_analysis_reviews(settings.data_dir / "reviews.jsonl", settings)


@pytest.mark.parametrize(
    ("update", "problem"),
    [
        ({"text": "SYNTHETIC FIXTURE too short"}, "words"),
        ({"word_count": 99}, "word_count does not match"),
        ({"lang": "hi"}, "not English"),
        ({"lang_confidence": 0.49}, "not English"),  # below the lexical-rescue floor
        ({"lang_confidence": None}, "not English"),
        ({"text_sha256": None}, "text_sha256"),
        (
            {"text": "  SYNTHETIC FIXTURE the app crashes every time I open my portfolio "},
            "not normalized",
        ),
    ],
)
def test_gate_rejects_unfit_records(
    settings: Settings, update: dict[str, Any], problem: str
) -> None:
    bad = _valid_review(settings).model_copy(update=update)
    path = _write_reviews(settings, [bad])
    with pytest.raises(PreprocessingError, match=problem):
        load_analysis_reviews(path, settings, allow_synthetic=True)


def test_gate_rejects_emoji_pii_and_tampered_text(settings: Settings) -> None:
    review = _valid_review(settings)
    kwargs: dict[str, Any] = {
        "min_words_exclusive": 8,
        "emoji_policy": EmojiPolicy.STRICT,
        "lang_min_confidence": 0.8,
        "allow_synthetic": True,
    }
    emoji_text = review.text + " \U0001f44d"
    pii_text = review.text + " call 9876543210"
    with_emoji = review.model_copy(
        update={"text": emoji_text, "text_sha256": hashlib.sha256(emoji_text.encode()).hexdigest()}
    )
    problems = review_problems(with_emoji, **kwargs)
    assert any("strict" in p for p in problems)
    assert any("quote-eligible" in p for p in problems)
    with_pii = review.model_copy(
        update={
            "text": pii_text,
            "text_sha256": hashlib.sha256(pii_text.encode()).hexdigest(),
            "word_count": review.word_count + 2,
        }
    )
    assert review_problems(with_pii, **kwargs) == ["unredacted PII: PHONE"]
    tampered = review.model_copy(update={"text": review.text.replace("app", "apps")})
    assert review_problems(tampered, **kwargs) == ["text_sha256 missing or does not match text"]


def test_gate_rejects_missing_file_bad_json_and_duplicates(settings: Settings) -> None:
    with pytest.raises(PreprocessingError, match="does not exist"):
        load_analysis_reviews(settings.data_dir / "reviews.jsonl", settings)
    review = _valid_review(settings)
    path = _write_reviews(settings, [review, review])
    with pytest.raises(PreprocessingError, match="duplicate review_id"):
        load_analysis_reviews(path, settings, allow_synthetic=True)
    path.write_text("{oops\n\n", encoding="utf-8")
    with pytest.raises(PreprocessingError, match="invalid record"):
        load_analysis_reviews(path, settings, allow_synthetic=True)


def test_gate_error_lists_at_most_five_problems(settings: Settings) -> None:
    review = _valid_review(settings)
    many = [review.model_copy(update={"review_id": f"id-{i}", "word_count": 1}) for i in range(7)]
    path = _write_reviews(settings, many)
    with pytest.raises(PreprocessingError, match=r"\(\+2 more\)"):
        load_analysis_reviews(path, settings, allow_synthetic=True)


def test_output_gate_failure_fails_closed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_raw_store(settings.data_dir, [raw_review("keep-1", EN)])
    monkeypatch.setattr(
        "groww_pulse.preprocessing.pipeline.review_problems", lambda *a, **k: ["boom"]
    )
    with pytest.raises(PreprocessingError, match="Output gate rejected review keep-1: boom"):
        run(settings)
    assert not (settings.data_dir / "reviews.jsonl").exists()


def test_report_dict_shape(settings: Settings) -> None:
    write_raw_store(settings.data_dir, corpus())
    data = run(settings).as_dict()
    assert data["settings"]["emoji_policy"] == "strict"
    assert data["settings"]["min_words_exclusive"] == 8
    assert data["window"]["start"] == "2026-07-23T00:00:00+00:00"
    assert data["empty"] is False
    assert data["raw_lines"] == 13
