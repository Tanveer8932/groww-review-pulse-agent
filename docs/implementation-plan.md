# Implementation Plan — Groww Review Pulse Agent

| Field | Value |
|---|---|
| Document status | Draft v1 |
| Last updated | 2026-10-19 (overview and [As-built status](#as-built-status-2026-10-19) updated; per-phase status sections keep their own dates) |
| Related docs | [problemStatement.md](problemStatement.md), [architecture.md](architecture.md) |

Scope reminder: **Google Play only. Apple App Store is excluded** from every phase below.

---

## Overview

| Phase | Name | Main output | Depends on |
|---|---|---|---|
| 0 | Foundation and verification spikes **(engineering complete; external approvals pending; see status)** | Repo skeleton, config, CI, verified MCP contract, data-source decision | — |
| 1 | Review collection and import **(implemented; first real import done 2026-10-14: 73/73 accepted from a manually collected file; source NOT approved, see data-source-decision.md §7)** | Canonical `reviews_raw.jsonl` from CSV/JSON/JSONL exports (collector is a disabled guard) | 0 |
| 2 | Preprocessing and privacy **(implemented and revised 2026-10-14: lexical language rescue, PII rules v2, whole-token Hinglish markers, automatic and manual quote holds; real-data validation done on 73 reviews: 67 kept, 60 quote-eligible)** | Filtered, deduplicated, redacted `reviews.jsonl` + funnel | 1 |
| 3 | AI analysis **(implemented; run once on real data 2026-10-15 with Groq `openai/gpt-oss-120b`, LLM themes, no fallback)** | ≤ 5 themes, top 3, verified quotes, 3 actions | 2 |
| 4 | Report generation and validation **(implemented; real 2026-W42 pulse passes all validators: 211 words, 3/3/3, no PII)** | ≤ 250-word pulse that passes all validators | 3 |
| 5 | MCP delivery **(implemented; real delivery 2026-10-17 through the bearer-auth Railway deployment: Doc appended, Gmail draft created, no email sent)** | Doc append + Gmail draft, idempotent, send blocked | 0 (contract), 4 |
| 6 | Streamlit UI and orchestration **(dashboard and publish panel implemented 2026-10-17; duplicate-week lock and read-only snapshot 2026-10-18; orchestrator, `run` command, and upload/run-stage pages not started)** | UI + CLI driving the full pipeline | 1–5 |
| D | Deployment **(dashboard on Streamlit Community Cloud as a read-only snapshot; Railway hosting of this agent not built)** | Running app on Railway (and/or Streamlit Cloud) | 6 |

Phase 5's MCP client can be built in parallel with Phases 2–4 once the Phase 0 contract is verified.

## As-built status (2026-10-19)

The per-phase "Status" sections below are dated records and are not rewritten. Where a later run changed an outcome, a dated **Update** note is added next to it. The current state:

| Area | State | Evidence |
|---|---|---|
| Processing | Import, preprocessing, Groq analysis, and report generation run only through the `groww-pulse` CLI, one stage at a time. No dashboard has browser controls for them | CLI commands in the [README](../README.md#cli-commands) |
| Real run | 73 reviews imported 2026-10-14 (`--as-of 2026-10-14`), 67 kept, 60 quote-eligible; analysis 2026-10-15 with Groq `openai/gpt-oss-120b` (LLM themes, no fallback); 2026-W42 pulse passing all validators | Local run records (`data/`, gitignored); committed `snapshot/` |
| Dataset provenance | Collected manually from the public Google Play listing and parsed offline. **Not approved**: no Play Console authorization, licence, or legal review. Actual review dates 2026-07-24 to 2026-09-19 | [data-source-decision.md](data-source-decision.md) |
| MCP server | Railway deployment `https://mcp-server-4-production-b6ec.up.railway.app/` with bearer auth. On 2026-10-17 it rejected handshakes without credentials and with a wrong token; `tools/list` had no send tool; the security gate passed | [mcp-server-security.md §6](mcp-server-security.md#6-railway-deployment-with-bearer-auth-verified-2026-10-17) |
| Delivery | 2026-10-17: the Week 42 pulse `6cac28a6…` was appended to the Google Doc (`appended`) and created as a Gmail draft (`draft_created`); `email_sent: false`. A repeat `publish --confirm` skipped both targets. The corrected Week 42 pulse `84d6f68b…` was **not** delivered again: duplicate-week protection marks both targets `blocked_week_already_delivered` | Ledger `data/runs/ledger.jsonl` (local); `snapshot/snapshot.json` |
| Hosted dashboard | Streamlit Community Cloud at <https://groww-review-pulse-agent-2nza7oxgal2ywzeqg36n9m.streamlit.app/>. Shows the committed, verified read-only snapshot of the corrected Week 42 pulse. Preview, delivery, and the MCP health check are disabled there | [architecture.md §6.8 and §9](architecture.md#68-read-only-dashboard-snapshot-snapshotpy) |
| Tests | Local 2026-10-19: 989 passed, 3 skipped (opt-in live tests), 97% coverage. GitHub Actions CI passes at commit `2e289cd` on Python 3.11 and 3.12 ([run 39209919490](https://github.com/Tanveer8932/groww-review-pulse-agent/actions/runs/39209919490)) | [README test results](../README.md#test-results) |
| Not built | Orchestrator and `groww-pulse run`; upload and run-stage pages; scheduling; hosting this agent on Railway (Dockerfile, persistent ledger volume, cron); a smoke script; a recorded manual check of the Doc and Drafts | — |

### Cross-cutting rules (every phase)
- Type hints on all public functions. `mypy --strict` on `src/`. `ruff` lint and format.
- Structured logging with `run_id`. No `print`.
- Typed exceptions from `errors.py`. No bare `except`.
- No real secrets, document IDs, or email addresses in code, tests, fixtures, or docs.
- Test fixtures are synthetic and labelled `# SYNTHETIC TEST FIXTURE — not real review data`. They are never used for a published pulse.
- Every PR must keep CI green (lint, type check, tests).

---

## Phase 0 — Foundation and Verification Spikes

**Objective:** Establish the project skeleton and resolve the open assumptions (A1–A6) before building on them.

### Deliverables
1. `pyproject.toml` (Python ≥ 3.11) with dependency groups for runtime and dev, plus a lock file and `requirements.txt` export.
2. Package skeleton matching [architecture.md §4](architecture.md), including `config.py` (pydantic-settings), `logging_setup.py` (with a secret-masking filter), `errors.py`, and `models.py` stubs.
3. `.env.example` listing every variable from architecture §8 with placeholder values only. `.gitignore` covers `.env`, `data/`, `tokens.json`, `.streamlit/secrets.toml`.
4. GitHub repo with a CI workflow (ruff, mypy, pytest) and pre-commit hooks (ruff, detect-secrets).
5. **MCP contract spike** (`scripts/check_mcp.py`, later folded into `delivery/mcp_client.py`):
   - Find the Streamable HTTP endpoint path on the deployed server.
   - Run `initialize` + `tools/list`, and save the tool names and input schemas to `docs/mcp-contract.md`.
   - Confirm whether client authentication is required.
   - Using a **test** Google Doc and the operator's own address, call `google_docs_append_content` and `gmail_create_draft` once manually, and record the real responses, redacted.
6. **Data-source decision record** (`docs/data-source-decision.md`): which permitted source(s) supply 8–12 weeks of reviews, who approved them, and under what terms. It also states whether the optional public collector may be enabled.

### Testing requirements
- `tests/unit/test_config.py`: missing required vars raise `ConfigError`; window values outside 8–12 are rejected; defaults apply.
- `tests/unit/test_logging.py`: secrets and token-like strings are masked in log output.
- CI runs on every push and PR.

### Acceptance criteria

**A. Engineering criteria: met, with local evidence (re-verified 2026-10-14)**

| Criterion | Evidence |
|---|---|
| [x] `pip install -e .[dev]` and `pytest` succeed on a clean Python 3.11 environment | A fresh venv with pip gave 79 passed, 1 skipped |
| [x] `docs/mcp-contract.md` lists the **observed** tool names and schemas from `tools/list`, not copied from the README, and confirms the endpoint path and auth model | `docs/mcp-tools-observed.json` (live output). Endpoint `POST /`. `auth_enforced: false` |
| [x] Records show `gmail_send_email` exists on the server, so the allowlist is needed | `contract.forbidden_tools_present = ["gmail_send_email"]`. `test_contract.py` proves it is blocked |
| [x] The repo contains no secrets or document IDs | detect-secrets baseline has `results: {}`. The CI-equivalent `git ls-files` scan passes |
| [x] Lint, format, strict typing, tests, and pre-commit pass locally | ruff, `ruff format --check`, `mypy --strict` (15 files), pytest 79/1 skipped, 98% coverage, 9/9 hooks |

**B. Criteria that depend on external action: NOT met**

| Criterion | Blocker | Owner |
|---|---|---|
| [ ] CI is green on `main` | No GitHub repo yet. Workflow written; every step passes locally; nothing committed | Operator |
| [ ] The data-source decision is approved | Recorded as **Proposed**; needs an approver, the chosen route (A or B), and licence confirmation | Operator |
| [ ] MCP test append and test draft recorded (deliverable 5, last bullet) | Needs a test Doc ID and the operator's own address. Account identity and token validity unknown until then | Operator |

**C. Security gate (added 2026-10-14): NOT met, does not block Phase 1**

| Criterion | Blocker | Owner |
|---|---|---|
| [ ] The MCP server authenticates clients, or a hardened fork is deployed, before any production delivery | Server is unauthenticated, and its OAuth routes lack a `state` check ([mcp-server-security.md](mcp-server-security.md)). **Blocks Phase 5 live delivery, not Phases 1–4** | MCP server owner / operator |

### Status — 2026-10-14 (reviewed): **engineering complete; 3 external approvals and 1 security gate pending**

| # | Deliverable | Status | Evidence |
|---|---|---|---|
| 1 | `pyproject.toml`, lock file, `requirements.txt` export | Done | `pyproject.toml` (runtime deps plus `[dev]` extra), `uv.lock`, `requirements.txt` (runtime only, exported from the lock) |
| 2 | Package skeleton, `config.py`, `logging_setup.py`, `errors.py`, `models.py` | Done | `src/groww_pulse/`. Later-phase subpackages exist as empty packages |
| 3 | `.env.example`, `.gitignore` | Done | Every architecture §8 variable, plus `MCP_TIMEOUT_SECONDS` and `LOG_FORMAT` |
| 4 | CI workflow and pre-commit hooks | Done locally | `.github/workflows/ci.yml` (ruff, format check, mypy, detect-secrets, pytest on 3.11 and 3.12); `.pre-commit-config.yaml`. All hooks pass. **GitHub repo not created yet** |
| 5 | MCP contract spike | Read-only part done; test writes pending | `scripts/check_mcp.py` (now also reports `auth_enforced`), `docs/mcp-contract.md`, `docs/mcp-tools-observed.json`. The endpoint `POST /` was re-confirmed, and **the server has no client auth**. Risk assessment and fix proposal in `docs/mcp-server-security.md` |
| 6 | Data-source decision record | Proposed (facts re-verified) | `docs/data-source-decision.md`. Automated collector rejected (robots.txt plus Google ToS). Recommended: Groww Play Console export (Route A), or a licensed vendor export (Route B). Corrected: the Play Console `Review Link` carries the ID in the URL fragment |
| — | Streamlit entrypoint | Done | `app/streamlit_app.py` shows config readiness, the tool policy, and phase status. Launched headless; `/_stcore/health` returned `ok` |

**Test results (local, Python 3.11.16):**
- 79 passed, 1 skipped (live MCP test, opt-in), with 98% line+branch coverage on `groww_pulse`.
- The opt-in live MCP contract test (`RUN_LIVE_TESTS=1`) passed against the deployed server. It is read-only.
- A fresh venv with `pip install -e .[dev]` then `pytest` gave 79 passed. On Windows, keep the venv on a short path: one deep scikit-learn file exceeds MAX_PATH under long directories.
- `ruff check`, `ruff format --check`, `mypy --strict` (src, scripts, app), detect-secrets, and all pre-commit hooks are clean.

**Additions beyond the plan:**
- `delivery/contract.py`: the allowlist and the `tools/list` contract evaluator, used by the spike now and by the Phase 5 client later.
- `tests/unit/test_contract.py`: proves `gmail_send_email` raises `ForbiddenToolError`.
- `tests/unit/test_streamlit_app.py`: AppTest checks that there is no send control and that no secret is displayed.

**Phase 1 readiness (assessed 2026-10-14): READY TO START, import-only**
- No Phase 1 deliverable depends on live collection. Importers, store, quarantine, window, and CLI are all testable with labelled synthetic fixtures.
- Format feasibility was checked: UTF-16 CSV with BOM, the documented Play Console headers, the `ReviewPlace:id=` fragment, millisecond timestamps, JSON arrays, and JSONL. Evidence is in data-source-decision.md §5.
- The models (`Review`, `Provenance`), the errors (`ImportSchemaError`, `CollectorDisabledError`), and config (`ANALYSIS_WINDOW_WEEKS`, `DATA_DIR`) Phase 1 needs already exist.
- **One Phase 1 criterion cannot close without the operator**: "a real permitted export imports…". It needs the approved data source and a real file in `data/imports/`. Synthetic fixtures must never stand in for it.

**Carry-over notes for later phases:**
- Phase 1: Play Console CSVs are UTF-16; map columns by header name, not position. The review ID comes from the `Review Link` fragment. `collection/play_public.py` is a disabled guard only (deliverable 3 below).
- Phase 5: the server's write tools are non-idempotent and `listChanged=true`. Success requires `structuredContent.ok == true`.
- Deployment: `requirements.txt` excludes the project itself. Streamlit Cloud will need `-e .` or a `sys.path` shim in the entrypoint.

---

## Phase 1 — Review Collection and Import

**Objective:** Get real, permitted Groww reviews into a canonical raw store.

### Deliverables
1. `collection/base.py`: `ReviewSource` protocol and `DateWindow`.
2. `collection/importers.py`: CSV, JSON, and JSONL importers with:
   - format auto-detection and a configurable column map (CLI flag or YAML);
   - date parsing to UTC (ISO 8601, epoch milliseconds, and common export formats);
   - encoding detection (UTF-8, UTF-8-BOM, UTF-16) and column mapping **by header name**, never position;
   - a built-in Play Console reviews CSV mapping, with `review_id` taken from the `Review Link` fragment `ReviewPlace:id=`;
   - row-level validation; invalid rows go to `data/quarantine/{run_id}.jsonl` with reasons;
   - the reviewer name, avatar, profile, and developer-reply fields dropped at ingest.
3. `collection/play_public.py`: **disabled guard only.** It raises `CollectorDisabledError` and contains no fetching code. Automated collection is rejected by [data-source-decision.md](data-source-decision.md) (robots.txt plus Google ToS). If that record is amended to approve a permitted automated source, this deliverable is re-specified.
4. `storage/jsonl_store.py`: atomic write and upsert by `review_id`. Writes `data/raw/reviews_raw.jsonl`.
5. CLI command: `groww-pulse import <file>... [--map cols.yaml]`. `groww-pulse collect` exists only to report that collection is disabled, with a pointer to the decision record.

### Testing requirements
- Importers: valid CSV, JSON, and JSONL fixtures; missing columns; bad dates; non-integer ratings; UTF-8, UTF-8-BOM, and **UTF-16** variants; Play Console header set with the review ID from the `Review Link` fragment; empty files; large files (10k rows streamed, with memory bounded). All fixtures are synthetic and labelled.
- Upsert idempotency: importing the same file twice yields an identical store (byte-equal after sort). Overlapping monthly files do not duplicate reviews.
- Window: boundary dates are included or excluded correctly. Timezone conversion is correct.
- Collector guard: `collect` and `play_public` raise `CollectorDisabledError` whatever `ENABLE_PLAY_COLLECTOR` is set to. A static test checks that `play_public.py` makes no network calls (no `httpx`/`requests` import). **No live network in CI.**
- PII-bearing reviewer fields are never written. The test asserts the absence of those keys.

### Acceptance criteria
- [~] *(needs operator data)* A real permitted export of Groww reviews imports into `reviews_raw.jsonl` with provenance on each record. **Technically met, approval pending:** the operator-supplied `groww_reviews_verified_combined.csv` imported 73/73 rows with provenance (see "Real-data import" below). The data-source decision record is still **Proposed**, and the file's origin and licence are not yet recorded there.
- [x] Re-importing the same file adds 0 records. *(tests `test_reimport_is_byte_identical_and_adds_nothing` and the CLI test; CLI smoke run gave store SHA-256 unchanged; all synthetic)*
- [x] Malformed rows are quarantined with reasons, and the import does not crash. *(`test_malformed_rows_are_quarantined_with_reasons`, `test_more_row_level_reasons_and_blank_lines`)*
- [x] Automated collection cannot run; the guard raises `CollectorDisabledError`. *(`test_collector_guard.py`, with `ENABLE_PLAY_COLLECTOR` set to both `true` and `false`)*
- [x] No Apple App Store code paths exist. *(AST scan of `src/` for App Store and scraping identifiers or literals: none)*

### Status — 2026-10-14: **implemented and tested with synthetic fixtures; real-data acceptance pending**

| # | Deliverable | Status | Where |
|---|---|---|---|
| 1 | `ReviewSource` protocol and `DateWindow` | Done | `collection/base.py`. The window is `weeks × 7` whole UTC days ending on and including `as_of`; 8–12 weeks enforced |
| 2 | CSV / JSON / JSONL importers | Done | `collection/importers.py`. Covers: encoding detection (UTF-8, UTF-8-BOM, UTF-16 with BOM, BOM-less UTF-16 LE/BE; anything else rejected, never guessed); mapping by normalized header name (`play_console` and `generic` profiles, plus a YAML `--map` override); `review_id` from the `Review Link` fragment `ReviewPlace:id=`, falling back to `sha256(source\|created_at\|text)`; ratings as whole numbers 1–5; dates from ISO 8601, epoch seconds/ms (Play Console millis preferred), and named formats, with naive → UTC and ambiguous slash dates rejected; allowlist ingestion; file-level atomicity |
| — | Canonical raw model | Done | `models.RawReview`: allowlisted fields only (`extra="forbid"`), original text verbatim plus `original_sha256`, UTC-aware datetimes, provenance |
| 3 | `collection/play_public.py` guard | Done | Always raises `CollectorDisabledError`; no network imports (AST-checked) |
| 4 | `storage/jsonl_store.py` | Done | Atomic write (temp file, fsync, `os.replace`); deterministic order and serialization; upsert by `review_id` with duplicate, newer-edit-replaces, and conflict-keeps-existing rules; quarantine appender |
| 5 | CLI | Done | `groww-pulse import <files-or-dirs>… [--map cols.yaml] [--as-of YYYY-MM-DD] [--weeks 8-12]` prints a JSON report. Exit codes: 0 ok, 1 some files rejected, 2 usage/config. `groww-pulse collect` always exits 2 with the reason |

**What is dropped at ingestion:**
- every column not mapped to a canonical field, including reviewer name, avatar, profile URL, reviewer email, device, and developer reply text and dates;
- the `Review Link` URL itself, which carries the developer account ID; only the extracted review ID is kept.

Tests assert that none of these appear in the store or the quarantine file.

**Row outcomes:**
- **accepted** into the store;
- **skipped, no text** (rating-only reviews; counted, not quarantined);
- **skipped, out of window** (counted);
- **quarantined**, recorded in `data/quarantine/{run_id}.jsonl` with location, reasons, and allowlisted field values only.

**Test results (2026-10-14, Python 3.11.16, local):**
- `pytest`: **206 passed, 1 skipped** (opt-in live MCP test). Coverage on `groww_pulse` is **99%** line+branch; `importers.py` 99%, `jsonl_store.py` 98%, `base.py`, `play_public.py`, and `cli.py` 100%.
- New Phase 1 test modules:
  - `test_collection_parsing.py`: window, rating, dates, encodings, header mapping, column map;
  - `test_importers.py`: end-to-end files covering all 5 encodings, reordered headers, PII field dropping, quarantine reasons, file-level errors, idempotency, overlapping monthly exports, edits, derived IDs, verbatim text, and 10k rows streamed with peak memory under 120 MB;
  - `test_jsonl_store.py`, `test_collector_guard.py`, `test_cli.py`.
- `ruff check`, `ruff format --check`, `mypy --strict` (20 files), all 9 pre-commit hooks, and the CI-equivalent detect-secrets scan are clean.
- CLI smoke run through the installed `groww-pulse` script, on a synthetic UTF-16 file in a scratch directory:
  - the expected counts appeared (1 accepted, 1 quarantined, 1 no-text, 1 out of window);
  - the re-import added 0 rows and left the store hash unchanged;
  - `collect` refused.
- Bugs found and fixed while testing:
  - BOM-less UTF-16 detection misfired on tiny files: `[]` was decoded as UTF-16;
  - the log handler kept a reference to a replaced, closed `stderr`; it now resolves `sys.stderr` at emit time.

**Real-data import (2026-10-14).** The operator placed `data/imports/groww_reviews_verified_combined.csv` (73 rows; SHA-256 `f1d5c57d…a88f3b`) and ran:

```
groww-pulse import data/imports/groww_reviews_verified_combined.csv --as-of 2026-10-14
```

| Check | Result |
|---|---|
| Schema | Headers `reviewId, content, score, at`; UTF-8 with BOM. Matched the `generic` profile with no `--map` and no code change |
| Rows | 73 read, **73 accepted**, 0 quarantined, 0 skipped (no text), 0 skipped (out of window). Store total 73 |
| Review IDs | 73 unique UUIDs from the file; **0 derived** |
| Ratings | All integers 1–5: 1★ 40, 2★ 5, 3★ 8, 4★ 7, 5★ 13 |
| Dates | All `YYYY-MM-DD`, 2026-07-24 to 2026-09-19, inside the 12-week window 2026-07-23 to 2026-10-14. Stored as UTC midnight |
| Fidelity | Every stored `text` equals the CSV `content` byte for byte; `original_sha256` verified for all 73 |
| Provenance | `source_file`, `ingested_at`, and `run_id` on every record; `source = google_play_import`; `app_id = com.nextbillion.groww` |
| Idempotency | Re-import: 0 added, 73 duplicates, store byte-identical |

Provenance and limits are recorded in [data-source-decision.md §7](data-source-decision.md):
- **Source:** the public Groww Google Play listing (`com.nextbillion.groww`), collected manually by the operator in a browser; saved Network responses were parsed offline into the CSV. **Not** a Play Console export; **no** confirmed reuse licence; **not** legally approved. Validation use only.
- **Coverage:** 2026-07-24 → 2026-09-19 on 36 distinct days. **2026-09-20 → 2026-10-14 (25 days) has no reviews**, so the last three window weeks are empty.
- **Sample:** 73 reviews is a small, non-random subset (sort order and scroll depth not recorded), skewed to 1★ (55%). Results describe this sample, not Groww's user base.
- **Missing fields:** no app version, no time of day, no thumbs-up count or title.
- **`source` field:** stored as `google_play_import`, the only import type the code has. The planned `manual_export` type is not implemented, so the collection method lives in the decision record, not in the records.

**Earlier source investigation (superseded by the import above). Real Groww review ingestion: NOT DONE at the time**. Source investigation on 2026-10-14 found no accessible compliant dataset ([data-source-decision.md §5b](data-source-decision.md)):
- the free Groww datasets on Hugging Face and GitHub are unlicensed, scraped or of undocumented provenance, and outside or only partly inside the window;
- scraping APIs are rejected;
- licensed vendors (42matters, Appbot, AppFollow, AppTweak) need an operator purchase or trial and a written collection-method and licence check.

Details:
- No real Groww reviews have been downloaded or imported. The project has no `data/` directory, and every test and smoke run used synthetic fixtures labelled `SYNTHETIC FIXTURE`, in temporary or scratch directories only.
- Blocked on the operator:
  - approve the data source (Route A or B in [data-source-decision.md](data-source-decision.md));
  - place the export in `data/imports/`;
  - run `groww-pulse import data/imports --as-of <latest date in the export>`.
- The Play Console header names come from Google's documentation, not from a real Groww file. The first real import must be checked. Expected results: profile `play_console`, a low quarantine rate, and non-derived IDs. If headers differ, use `--map`.

**Remaining limitations:**
1. `--as-of` defaults to today (UTC). Because of the 3–7 day Play Console posting lag (R15), pass the latest date present in the export; the window is not derived automatically.
2. The raw store is not pruned. Reviews from earlier imports stay even if outside a later window; Phase 2 re-applies the window. `RAW_RETENTION_DAYS` pruning is not implemented yet.
3. JSON arrays are loaded whole; CSV and JSONL stream. Accepted rows of each file and the whole store are held in memory during an import. This is fine for 12 weeks of reviews; the 10k-row test peaked under 120 MB.
4. Review text is stored **verbatim**, including any PII typed inside it. By design (FR-9, FR-10), redaction happens in Phase 2; the raw store is gitignored and restricted.
5. Rating-only reviews are skipped. `title` is stored separately and not merged into `text`.
6. Naive timestamps are assumed UTC. Slash dates (`01/02/2026`) are rejected as ambiguous. Use ISO 8601 or epoch.
7. Generic exports without a package column are assumed to be `APP_ID`; the operator is responsible for importing only Groww exports.
8. Same ID, same timestamp, different content → existing record kept, `conflicts` counted, warning logged.
9. Quarantine is written per run, so re-importing a file with bad rows quarantines them again under the new run ID.
10. No file locking: run one import at a time.

---

## Phase 2 — Preprocessing and Privacy

**Objective:** Produce the canonical, redacted `data/reviews.jsonl` with an auditable funnel.

### Deliverables
1. `preprocessing/normalize.py`: NFC, whitespace, and the shared `count_words()` function used by both preprocessing and report validation.
2. `preprocessing/emoji_policy.py`: `strict` (default) and `lenient` per architecture §6.3. Text is never modified.
3. `preprocessing/language.py`: English detection with a confidence threshold.
4. `preprocessing/dedupe.py`: ID, exact-hash, and near-duplicate (rapidfuzz) deduplication.
5. `preprocessing/pii.py`: redactors for email, Indian phone numbers, PAN, Aadhaar (Verhoeff), UPI ID, account and card numbers (Luhn), URLs, and handle or name patterns. Returns the redacted text plus per-type counts.
6. `preprocessing/pipeline.py`: ordered chain (window → length → emoji → language → dedupe → PII → length re-check). Writes `reviews.jsonl` and `data/runs/{run_id}/funnel.json`.
7. CLI: `groww-pulse preprocess`.

### Testing requirements
- Length: exactly 8 words is dropped, 9 is kept. Punctuation and hyphenation edge cases are covered.
- Emoji: emoji-only, a single emoji inside text, ZWJ family sequences, skin-tone modifiers, flags, and text symbols like `:)` (not emoji, so kept) are each tested under both policies.
- Language: English, Hindi (Devanagari), Hinglish, mixed, and short-borderline cases.
- Dedupe: identical IDs, case and whitespace variants, near-duplicates (≥ 95), and different ratings (not merged).
- PII: a table-driven corpus of ≥ 40 positive and ≥ 20 negative cases. Examples: a 12-digit Aadhaar with a valid versus invalid checksum; prices like "₹10,000" must not be redacted as account numbers.
- Property test (hypothesis): redaction is idempotent, `redact(redact(x)) == redact(x)`.
- Funnel: the counts per step sum correctly (`in - dropped = out`).

### Acceptance criteria
All five are met **on synthetic fixtures only**. Each must be re-confirmed on the first real export (see "Real-data validation status" below).
- [x] Every record in `reviews.jsonl` has > 8 words, is English above the threshold, complies with the emoji policy, is unique, and is redacted. *(`test_output_integrity`; enforced at write time by the output gate and at read time by `load_analysis_reviews`)*
- [x] The PII scan over `reviews.jsonl` finds 0 matches. *(`test_output_integrity`, `test_redaction_changes_only_pii`; the output gate fails closed on any leftover match)*
- [x] The funnel is persisted and the numbers reconcile. *(`test_funnel_reconciles_and_is_persisted`)*
- [x] Original text exists only in the raw store, and `original_sha256` links the two. *(`test_unredacted_text_only_in_raw_store`, `test_provenance_detects_tampering`)*
- [x] The pipeline is deterministic: two runs on the same input give byte-identical output. *(`test_deterministic_byte_identical_output`)*

### Status — 2026-10-14: **implemented and tested with synthetic fixtures; real-data validation pending**

| # | Deliverable | Status | Where |
|---|---|---|---|
| 1 | Normalization and shared `count_words()` | Done | `preprocessing/normalize.py`. NFC; removes invisible characters (ZWSP, BOM, bidi marks) and control characters; collapses whitespace. Keeps the zero-width joiner so emoji ZWJ sequences stay detectable. A word is a `\b[\w'’-]+\b` token |
| 2 | Emoji policy | Done | `preprocessing/emoji_policy.py`. `strict` (default) drops any review with an emoji; `lenient` drops emoji-only reviews and marks the rest `quote_eligible=false`. Text is never modified |
| 3 | English detection | Done (revised 2026-10-14) | `preprocessing/language.py`. Non-Latin script, then a Hinglish marker check, then `lingua` over 16 candidate languages with the `LANG_MIN_CONFIDENCE` threshold, plus a **lexical rescue** for borderline English (`preprocessing/lexicon.py`; policy and evaluation below) |
| 4 | Deduplication | Done | `preprocessing/dedupe.py`. Same ID, exact text (case- and whitespace-insensitive), then near-duplicate (rapidfuzz token-set ratio ≥ 95, same rating, word-count ratio ≥ 0.8). Keeps the earliest |
| 5 | PII redaction | Done (rules v2, 2026-10-14) | `preprocessing/pii.py`. `[URL]`, `[EMAIL]` (including obfuscated forms), `[UPI_ID]`, `[NAME]` (cue-based, see below), `[ADDRESS]`, `[PAN]`, `[PHONE]` (mobile, international, STD landline), `[AADHAAR]` (Verhoeff), `[CARD]` (Luhn), `[ACCOUNT_NO]` (including client/demat/UCC IDs given with a keyword). Only matched spans change. Returns per-type counts, never values. `PII_RULES_VERSION` is recorded in the funnel |
| 6 | Pipeline, output, and funnel | Done | `preprocessing/pipeline.py`. Writes `data/reviews.jsonl` atomically and `data/runs/{run_id}/funnel.json` |
| 7 | CLI | Done | `groww-pulse preprocess [--as-of YYYY-MM-DD] [--weeks 8-12]` prints the funnel as JSON. Exit codes: 0 ok (including an empty result, with a warning on stderr), 1 preprocessing refused or failed, 2 usage or config error |
| — | Analysis gate (beyond the plan) | Done | `load_analysis_reviews()` is the only supported way for Phase 3 to read `reviews.jsonl`. It re-runs `review_problems()` on every record and raises `PreprocessingError` on any failure |
| — | Provenance check (beyond the plan) | Done | `verify_provenance()` proves each review's text can be re-derived from the raw store, for Phase 3/4 quote verification |
| — | `Review.text_sha256` (beyond the plan) | Done | Hash of the redacted text, so an edited `reviews.jsonl` is detected before quotes are verified against it |

**Filtering pipeline** (fixed order; each step records `input`, `dropped`, `output`, and a count per reason):

| # | Step | Drops when | Reason codes |
|---|---|---|---|
| 1 | `validate` | The line is not a valid `RawReview`; `sha256(text) ≠ original_sha256`; `app_id ≠ APP_ID`; the text is empty after normalization | `invalid_record`, `hash_mismatch`, `wrong_app_id`, `empty_text` |
| — | synthetic guard | Any record carries the `SYNTHETIC FIXTURE` marker: the **whole run is refused** and nothing is written | — (raises `PreprocessingError`) |
| 2 | `window` | `created_at` outside the 8–12 week window ending on `--as-of` (same `DateWindow` as Phase 1) | `outside_window` |
| 3 | `length` | `count_words(text) ≤ MIN_WORDS_EXCLUSIVE` (8) | `too_few_words` |
| 4 | `emoji` | strict: any emoji; lenient: emoji-only | `contains_emoji`, `emoji_only` |
| 5 | `language` | See the language policy below | `non_latin_script`, `hinglish`, `not_english`, `low_confidence` |
| 6 | `dedupe` | Repeated ID, exact text, or near-duplicate | `duplicate_id`, `exact_duplicate`, `near_duplicate` |
| 7 | `pii_redaction` | Never drops. Totals per placeholder type and the number of redacted reviews go into the funnel | — |
| 8 | `length_recheck` | Redaction left ≤ 8 words (for example, a long URL collapsed to `[URL]`) | `too_few_words_after_redaction` |
| 9 | `output_gate` | Never drops. Any record failing `review_problems()` aborts the run before anything is written | — (raises `PreprocessingError`) |

**Language policy** (FR-6, A7). The first rule that fires decides:
1. Any letter outside the Latin script (Devanagari, Tamil, Urdu, …) → `non_latin_script`. Mixed-script reviews are excluded too.
2. Two or more tokens from a list of common romanized-Hindi words that are not English words (`hai`, `nahi`, `bahut`, `mujhe`, `kab`, `abhi`, `zyada`, …; about 190 words) → `hinglish`. Ambiguous tokens (`main`, `me`, `to`, `par`, `hum`, `pe`, `ho`, `fir`, …) are deliberately not on the list. One loanword such as `bhai` is not enough. A test asserts that no marker word is in the English lexicon.
3. The detector's most likely language is not English → `not_english`.
4. English is most likely and its confidence is at least `LANG_MIN_CONFIDENCE` (0.80) → kept (detector).
5. **Lexical rescue** (`LANG_LEXICAL_RESCUE=true` by default): English is most likely but below 0.80. The review is kept only if an independent signal agrees:
   - the English confidence is at least **0.50**, so English is more likely than all other candidates combined;
   - there are no 2+ Hinglish markers (already guaranteed by rule 2);
   - at least **6** ordinary words are present;
   - at least **85%** of them are known English words.

   Finance and product terms (SIP, IPO, KYC, NAV, demat, Groww, …), all-caps acronyms, numbers, and redaction placeholders are neutral and not counted. The lexicon is scikit-learn's `ENGLISH_STOP_WORDS` plus a curated app-review and finance vocabulary, with light suffix stripping.
6. Otherwise → `low_confidence`.

The marker count and the lexical coverage are computed on the **redacted** text. The output gate and `load_analysis_reviews()` see only redacted text, so they re-derive exactly the same rescue decision; a stored confidence below 0.80 passes the gate only if the rescue holds. Nothing is translated. `lang` and `lang_confidence` (English confidence, rounded to 4 places) are stored on each kept review, and the funnel records `lang_lexical_rescues` plus the rescue parameters.

**Language evaluation (2026-10-14).** *Why were valid English reviews rejected?* `lingua`'s confidence is a share spread across all 16 candidates, not an absolute score. Short English reviews that contain uppercase finance acronyms (SIP, KYC, IPO, AMC, UI) or cognates ("application", "excellent", "funds", "stocks") leak probability to German, French, and Tagalog. For example, "Excellent app, smooth UI, easy KYC, fast IPO application and zero AMC" scored 0.683. Masking the acronyms before detection raised scores by only about 0.01, so masking was not adopted.

The evaluation used a labelled corpus of hand-written test sentences (`tests/fixtures/language_corpus.py`): 35 finance English, 15 general English, 30 Hinglish, 6 low-marker Hinglish, 10 other languages (Indonesian, Malay, Tagalog, Spanish, Portuguese, French, German), and 4 very short English. These are **not real reviews**.

| Policy | English kept (of 50) | Hinglish kept (of 36) | Other kept (of 10) |
|---|---|---|---|
| Threshold 0.80, no rescue (old) | 47 | 0 | 0 |
| Threshold 0.70, no rescue | 49 | 0 | 0 |
| Threshold 0.50, no rescue | 50 | **1** | 0 |
| Detector only (marker rule off), threshold 0.60 | 50 | **2** | — |
| Detector only (marker rule off), threshold 0.50 | 50 | **7** | — |
| **Threshold 0.80 + lexical rescue (new default)** | **50** | **0** | **0** |

Tradeoffs:
- Lowering the threshold recovers English but admits Hinglish the marker list misses. At 0.50 the low-marker sentence "Mast trading experience, ho gaya profit, shukriya Groww team members" (0.531) is kept. The rescue rejects it because only 44% of its words are English.
- On this corpus, English scored 0.683–0.996, Hinglish (when English ranked first) 0.09–0.62, and lexical coverage was 0.80–1.00 for English and 0.00–0.50 for Hinglish. The two signals separate the groups more cleanly than either does alone.
- The rescue cannot help very short reviews ("good app"), because they have fewer than 6 ordinary words. These are already excluded by the 8-word rule.
- The numbers come from a small hand-written corpus. They justify the design, not the exact constants. Re-measure on the first real export (see "Real-data validation status").

**What a record in `data/reviews.jsonl` carries:**
- `text`: normalized and redacted. The only text used downstream.
- `original_sha256`: copied from the raw store after re-checking it against the raw text.
- `text_sha256`, `word_count`, `lang`, `lang_confidence`, `quote_eligible`, and `pii_redactions` (counts per type).
- `provenance`: the import's `source_file`, `ingested_at`, and `run_id`.

The preprocessing run ID is kept out of `reviews.jsonl`, so the output stays byte-identical across runs; it appears only in the funnel path.

**Test results (2026-10-14, Python 3.11.16, local):**
- `pytest`: **383 passed, 1 skipped** (the opt-in live MCP test). Phase 2 added 177 tests. Coverage on `groww_pulse` is **99%** line+branch: `normalize.py`, `emoji_policy.py`, `language.py`, `dedupe.py`, and `cli.py` at 100%; `pii.py` and `pipeline.py` at 99%.
- New Phase 2 test modules:
  - `test_preprocessing_filters.py` (60): word-count boundaries (8 dropped, 9 kept), punctuation, hyphens, and apostrophes; normalization; emoji-only, inline, ZWJ family, skin tone, flag, and text-presentation heart under both policies; `:)`, ★, →, ₹, ✓ are not emoji; English, Devanagari, Tamil, mixed-script, Hinglish, Portuguese, Spanish, French, short-borderline, and a configurable threshold.
  - `test_pii.py` (73): **43 positive and 22 negative** table cases, including a valid vs. invalid Aadhaar checksum, a 16-digit number that fails Luhn, and prices like `₹10,000` and `1,00,000` left alone. Also checksum vectors, a check that every non-PII character survives, and two Hypothesis property tests (idempotency, and letter-only text never changing).
  - `test_dedupe.py` (10): identical IDs, case and whitespace variants, near-duplicates, reordered words, different ratings not merged, a subset review not merged into a longer one, deterministic tie-breaks, and chains.
  - `test_preprocess_pipeline.py` (29): the full funnel with every reason code, reconciliation, output integrity, provenance and tampering, byte-identical re-runs, empty and missing raw store, a stale output overwritten by an empty result, window boundaries, hash mismatch, lenient mode, synthetic refusal, and every analysis-gate rejection.
  - `test_cli.py` (+5): empty store, synthetic refusal, a successful run after import, and usage errors.
- `ruff check`, `ruff format --check`, `mypy --strict` (26 files), and all 9 pre-commit hooks (including detect-secrets) are clean.
- Performance smoke test on 5,000 synthetic raw reviews in a scratch directory: **12 s** end to end (NFR-8 allows 5 min).
- Bugs found and fixed while testing:
  - `lingua` returns new `Language` objects, so identity checks (`is`) never matched English; it now compares with `==`.
  - A fabricated 14-digit account number happened to pass Luhn and was labelled `[CARD]`; the fixture was changed. Card and account numbers are both redacted either way.

**PII rules v2 (2026-10-14).** Added after the original implementation:

| Type | New rule | Guarded against (negative tests) |
|---|---|---|
| `[NAME]` | "my/his/her … name is X", including the relatives and qualifiers `father`, `wife`, `full`, `user`, …, and "name:". A capitalised word is always a name here; a lowercase word only if it is not English. Up to 3 words, stopping at a function word or finance term | "my name is not showing", "My name is wrong in the KYC form", "My SIP name is Retirement Fund" |
| `[NAME]` | A capitalised name after `Mr`/`Mrs`/`Ms`/`Shri`/`Smt`/`Sri` | — (`Dr` is excluded because of the stock "Dr Reddy's") |
| `[NAME]` | A capitalised **non-English** word after `myself`, `named`, `agent`, `executive`, `representative`, or in a closing signature ("Regards, Anil Kumar" at the end of the review) | "an agent named Support Bot", "Thanks Groww", "Thanks, Great app" |
| `[EMAIL]` | `x[at]y[dot]z`, `x (at) y (dot) z`, and `x at gmail dot com` (spaced form for well-known mail providers only) | "look at this dot com style bubble" |
| `[ADDRESS]` | The rest of the clause after "my address is", "I live/stay/reside at", "residing at", or "address:" (only when it contains a digit); flat, house, `H.No`, plot, door, shop, or room numbers; 6-digit PIN codes after "pin", "pincode", "postal code", or "zip code" | "I live in Pune", "my address is updated but…", "pin 1234 not working", "room for improvement", "door step KYC" |
| `[ACCOUNT_NO]` | An ID containing a digit after "client/customer/user/demat/trading/BO/DP/folio id/code/no/number" or "UCC"; NSDL DP IDs (`IN` + 14 digits) | "User interface is bad and trading account is blocked", "Ticket 45678 is still pending" |
| `[PHONE]` | Landlines with an STD code (`022-23456789`, `080 2345 6789`; 10–11 digits) | "toll free 1800 103 4567" (a business number) |

Engineering details:
- Name rules use the same English lexicon as the language rescue, so ordinary English words are never mistaken for names.
- Cue rules resume searching right after each replaced span. This keeps redaction idempotent when two cues overlap; Hypothesis found the overlap case ("MY NAME IS B My NAME IS B").
- On the 127 hand-written evaluation and analysis-corpus sentences, the v2 rules changed only the one deliberately planted phone number.
- `PII_RULES_VERSION` (`"2"`) is written to the funnel. A `reviews.jsonl` made with older rules fails provenance re-derivation in Phase 3/4, so **re-run `groww-pulse preprocess` after upgrading**.

**Preservation guarantees (re-verified after the v2 changes).**
- `original_sha256`, `text_sha256`, `verify_provenance()`, and the Phase 3 quote verification are unchanged.
- Quotes are still verbatim spans of the redacted text.
- New tests in `test_language_policy.py` run the real pipeline on reviews containing a name, a phone number, and an address. They select and verify quotes against the raw store, and assert that:
  - no original PII value appears in any quote;
  - redacted reviews stay quote-eligible but rank below clean ones.

**Test results after the Task 1 revision (2026-10-14, Python 3.11.16, local):**
- `pytest`: **681 passed, 2 skipped** (the opt-in live MCP and live LLM tests), up from 492. Coverage on `groww_pulse` is **98%** line+branch: `language.py` and `lexicon.py` at 100%, `pii.py` and `pipeline.py` at 99%.
- `test_language_policy.py` (142, new):
  - every corpus sentence's outcome;
  - the threshold tradeoff table above, as an executable test;
  - the rescue's floor, token, coverage, and marker guards;
  - rescue decided identically on raw and redacted text;
  - gate acceptance and rejection, with the rescue switched on and off;
  - lexicon inflections and neutral tokens; marker list disjoint from the lexicon;
  - pipeline funnel counts with `LANG_LEXICAL_RESCUE` on and off;
  - quote eligibility and PII-free quotes.
- `test_pii.py` (73 → 120): **69 positive and 43 negative** table cases (from 43/22), every placeholder type covered, and the extended Hypothesis idempotency and letters-only properties.
- Two existing tests changed intentionally:
  - a gate case now stores confidence 0.49, below the rescue floor (it was 0.50, which the rescue now accepts for clean English);
  - the "threshold is configurable" test now switches the rescue off explicitly.
- **Phase 3 regression:** all 109 Phase 3 tests pass unchanged. `run_analysis()`, `load_analysis_reviews()`, `verify_provenance()`, `review_problems()`, and `detect_english()` keep their signatures; new parameters are keyword-only with defaults.
- `ruff check`, `ruff format --check`, and `mypy --strict` are clean.
- Performance smoke test on 5,000 synthetic raw reviews (scratch directory): **22.6 s** (NFR-8 allows 5 min). Language detection now also redacts its input, so this is slower than the earlier 12 s, on a different corpus.
- Bugs found and fixed:
  - overlapping name cues needed two passes, which broke idempotency (found by Hypothesis);
  - all-caps names were cut to their first letter;
  - "My SIP name is …" was treated as a personal name;
  - a regex written through a shell heredoc picked up literal backspace characters again (in a test file). Regex edits now go through files, and the tree was scanned for control characters.

**Real-data validation (2026-10-14, final).** `groww-pulse preprocess --as-of 2026-10-14` on the 73 imported reviews (12-week window), run `20261014T173632Z-6b3c7f`:

| Step | In | Dropped | Out | Reasons |
|---|---|---|---|---|
| validate | 73 | 0 | 73 | — (all 73 `original_sha256` match) |
| window | 73 | 0 | 73 | — |
| length | 73 | 0 | 73 | — (shortest kept review: 25 words) |
| emoji | 73 | 6 | 67 | `contains_emoji` 6 (strict policy) |
| language | 67 | 0 | 67 | — |
| dedupe | 67 | 0 | 67 | — |
| pii_redaction | 67 | 0 | 67 | 0 redactions |
| length_recheck | 67 | 0 | 67 | — |
| output_gate | 67 | 0 | 67 | — |

- **67 reviews** in `reviews.jsonl` (1★ 37, 2★ 4, 3★ 7, 4★ 7, 5★ 12), dated 2026-07-24 → 2026-09-19. **60 quote-eligible**; 7 on quote hold (below).
- `load_analysis_reviews()` accepts all 67, including the manual-hold registry check. `verify_provenance()` reports **0 failures**. Every raw text still equals the CSV `content` byte for byte, and every raw record carries `source_file`, `ingested_at`, and `run_id`. The raw store was not modified by this pass (SHA-256 unchanged). A re-run of preprocessing is byte-identical.
- **Language:** all 67 kept by the detector; 0 lexical rescues; 0 language drops.
- **Emoji:** 6 of 73 (8%) dropped, including 2 that use ⭐ as a rating symbol.
- **PII:** the rule scanner finds nothing in the 73 raw texts, and a manual read of all 73 found no names, phone numbers, emails, or account numbers. The redaction step was therefore not exercised on real data.
- **Duplicates:** 0 by ID, exact text, or near-duplicate (the file arrived deduplicated).

**Hinglish false positive fixed (2026-10-14).** The first run dropped `c8031c34…` as `hinglish`: it is English, but mentions the broker "5paisa" three times, and the marker tokenizer `[a-z]+` split "5paisa" into the marker `paisa`. Markers now have to be whole tokens: `_TOKEN_RE` is `[a-z0-9]+`, so a letter run joined to digits is one token and never matches. A marker next to punctuation, spaces, or a separate number still counts ("paisa 2 din", "paisa-wasool", "kya bakwas…!!!"). The 36 Hinglish corpus sentences are all still excluded, and the threshold-tradeoff test is unchanged. Regression tests: `test_markers_inside_alphanumeric_tokens_do_not_count` and `test_hinglish_next_to_digits_and_punctuation_is_still_excluded` in `test_preprocessing_filters.py`. `c8031c34…` is now kept (confidence 1.0) but held from quoting by the truncation rule (490 characters, no closing punctuation).

**Quote holds.** Reviews whose original text may not be what the reviewer wrote stay in `reviews.jsonl` for theming, with `quote_eligible=false` and the reasons in `quote_hold_reasons`. Nothing is dropped and no text changes.

*Automatic holds* (`preprocessing/integrity.py`, computed on the raw text):
- `possible_truncation`: within 10 characters of Google Play's 500-character limit and not ending in closing punctuation, or ending in an ellipsis.
- `unbalanced_quotes`: an odd number of `"`, or unequal `“`/`”`.

*Manual holds* (`data/quote_holds.json`, operator-maintained, gitignored with the data): each entry has `review_id`, a snake_case `reason`, and an optional `note` and `added` date; it adds `manual:<reason>`. The file holds IDs and reason codes only, never review text. A malformed file stops preprocessing before anything is written. To lift a hold after verifying the original text, delete its entry and re-run `groww-pulse preprocess`.

Enforcement:
- The output gate rejects any held review marked eligible.
- `verify_provenance()`, which Phase 3 calls for every quote, re-derives the automatic holds from the raw store.
- `load_analysis_reviews()`, which Phases 3 and 4 use to read `reviews.jsonl`, re-reads the registry and refuses a file in which a listed review lacks its manual hold (for example, the registry was edited without re-running preprocessing).
- The funnel records `quote_holds` per reason and `settings.manual_holds` (path, number of IDs, SHA-256 of the registry).

Holds on the real data (7):

| Review | Reason | Evidence |
|---|---|---|
| `fcb0057c…` | `possible_truncation` | Ends "…lower than my origi" (498 chars) |
| `2228dac7…` | `possible_truncation` | Ends "…considered t" (500 chars) |
| `cbb5923b…` | `possible_truncation` | 491 chars, ends mid-sentence |
| `c8031c34…` | `possible_truncation` | 490 chars, no closing punctuation; may be complete (conservative hold) |
| `8b4e9c54…` | `unbalanced_quotes` | Opens with `banned."`; last sentence looks spliced from another text |
| `dd491cad…` | `manual:starts_mid_sentence` | Begins "your operations in stocks right .If…" |
| `4705e73c…` | `manual:possible_spliced_text` | "…when account is switched Dear Groww Tea, This is requested…" |

**Tests (2026-10-14):** `pytest` **897 passed, 3 skipped** (opt-in live tests); coverage 98% overall, `integrity.py` and `language.py` 100%, `pipeline.py` 99%. `test_integrity.py` (41): truncation boundaries and endings, quote balance, ordering, pipeline holds, gate and provenance tampering, and the manual registry (applied, combined with automatic holds, unknown IDs warned, text unchanged, missing hold refused at analysis load, lifted after re-run, 10 malformed-file cases failing closed). `test_preprocessing_filters.py` +8 language regression cases. `ruff check`, `ruff format --check`, `mypy --strict` (43 files), and all 9 pre-commit hooks are clean.

**Remaining before the data can be used beyond validation:** approval of the source (data-source-decision.md §6–§7), manual verification of the 7 held texts if they are wanted as quotes, and a decision on the 25-day coverage gap.

**Known limitations:**
1. **Language constants are tuned on a small hand-written corpus.** The rescue (0.50 / 6 words / 85%) and the 0.80 threshold separate the 96 evaluation sentences perfectly, but none of them is a real review. Real reviews may contain spelling errors, slang, or rare vocabulary that lowers lexical coverage. That only makes the filter stricter, so English may still be lost, but non-English is not admitted. Measure on the first real export.
2. **Hinglish detection is heuristic.** It uses a word list plus the detector plus the lexicon. Hinglish with at most one marker word passes only if the detector ranks English first and clears 0.80. An English review with 2 marker-like tokens (for example `ko`, `se`, `din`) is excluded.
3. **Emoji means the `emoji` library's set.** © ® ™ ‼ ❤ count as emoji and drop a review under `strict`; ✓ ★ → ₹ and `:)` do not.
4. **PII is rule-based, not NER.**
   - Names are found only next to a cue (see the v2 table). Free-text names ("Rahul from Pune said…", "thanks to Priya"), names written in lowercase outside "my name is", and names that are also English words after cues such as `named` ("named Hope") are not redacted.
   - Addresses without a digit or a cue are not redacted, and an address cue's clause ends at the first `.`, so "H.No." inside an address cue can end the redaction early. The house-number rule usually catches the rest.
   - City-level locations, bare company domains (`groww.in`), and toll-free numbers are deliberately left unredacted.
   - Order, ticket, and transaction references are redacted only when they have 9–19 digits, as `[ACCOUNT_NO]`/`[CARD]` (over-redaction that errs toward privacy).
   - Over-redaction is possible after honorifics ("Mr. Market") and in the clause after an address cue.
5. **Near-duplicate guard.** Beyond architecture §6.3, near-duplicates also need word counts within a 0.8 ratio. Token-set ratio scores 100 whenever one review's words are a subset of another's. Very short distinct reviews built from a small shared vocabulary can still be merged; the 5,000-row synthetic smoke test (an 18-word vocabulary) merged heavily, so check the `near_duplicate` share on real data.
6. **Word definition.** FR-5 says "whitespace-delimited tokens"; the implementation uses the architecture's stricter `\b[\w'’-]+\b`, so bare punctuation is not a word and `10,000` counts as two words.
7. **Window default.** `--as-of` defaults to today (UTC), as in Phase 1. Pass the latest date in the export (R15).
8. **Raw store not pruned.** `RAW_RETENTION_DAYS` pruning is still not implemented.
9. **Synthetic guard is marker-based.** It recognises the `SYNTHETIC FIXTURE` marker that every project fixture carries; it cannot detect unlabelled fabricated data.
10. **Normalization changes whitespace only.** Line breaks are collapsed to single spaces in `text`; the original layout remains only in the raw store.
11. **Re-running overwrites.** `reviews.jsonl` is replaced on every run, including by an empty result, so stale data never reaches analysis. Each run's funnel is kept under `data/runs/`.

---

## Phase 3 — AI Analysis

**Objective:** Produce trustworthy themes, rankings, quotes, and actions.

### Deliverables
1. `analysis/llm/`: `LLMClient` interface, an Anthropic implementation (model from `LLM_MODEL`), JSON-schema-validated responses with one repair retry, a disk cache, token-budget enforcement, and retry with backoff on rate limits.
2. `analysis/prompts/`: versioned templates (`theme_discovery.v1`, `theme_assignment.v1`, `actions.v1`). Review text sits inside delimited data blocks, with prompt-injection guidance.
3. `analysis/themes.py`: stratified-sample discovery (≤ 5 themes), batched assignment with an `other` bucket, and the TF-IDF + KMeans fallback.
4. `analysis/ranking.py`: `score = count × (1 + low_rating_share)` with deterministic tie-breaks and top-3 selection.
5. `analysis/quotes.py`: candidate selection, sentence-span extraction (≤ 35 words), verbatim verification, and the fail-closed rule.
6. `analysis/actions.py`: exactly 3 validated action ideas linked to the top themes.
7. CLI: `groww-pulse analyze` writes `data/runs/{run_id}/analysis.json`.

### Testing requirements
- The LLM is mocked with a fake client returning canned JSON, including invalid outputs:
  - more than 5 themes is rejected;
  - duplicate names are rejected;
  - unknown review IDs are rejected;
  - missing assignments go to `other`;
  - malformed JSON triggers the repair retry and then `LLMOutputError`.
- Cache: identical input means 0 provider calls on the second run.
- Budget: exceeding the budget raises `BudgetExceededError` before the call.
- Ranking: hand-computed fixtures cover tie-breaking, and `other` is never ranked.
- Quotes:
  - `text[start:end] == quote` for every selected quote;
  - tampering with one character fails verification;
  - quote-ineligible (lenient-emoji) reviews are never chosen;
  - fewer than 3 eligible candidates raises an error;
  - the redistribution rule still yields exactly 3.
- Actions: exactly 3, each linked to a top theme and ≤ 25 words. Invented numbers are rejected.
- Fallback clustering: produces 2–5 themes on the fixture corpus.
- Optional live-LLM smoke test marked `@pytest.mark.live`. It is skipped in CI unless `RUN_LIVE_TESTS=1`.

### Acceptance criteria
The criteria that name real data are **not met**. The others are met with a mocked LLM on synthetic fixtures only and must be re-confirmed on the first real run (see "Real-data validation status" below).
- [x] *(needs real data)* On real preprocessed data: 1–5 themes, each with ≥ 1 member, every review assigned to a theme or `other`. *(Mechanism proven on synthetic data: `test_end_to_end_analysis`.)* **Update 2026-10-15:** met on the 67 real reviews with Groq (LLM themes, no fallback).
- [x] The top 3 are selected with a displayed, reproducible score. *(`test_theme_stats_are_exact`, `test_ranking_order_tie_breaks_and_exclusions`, `test_ranking_tie_on_score_breaks_by_count`; the formula is written into `analysis.json`)*
- [x] *(needs real data)* Exactly 3 quotes, each a verified verbatim substring of a stored, redacted, **real** review. *(Mechanism proven on synthetic data, including raw-store provenance: `test_end_to_end_analysis`, the quote verification tests.)* **Update 2026-10-15:** met on the real run; the quotes are re-verified in the 2026-W42 pulse.
- [x] Exactly 3 concrete actions tied to the top themes. *(`test_valid_actions_pass`, `test_invalid_actions_are_rejected`, `test_generate_actions_repairs_invented_numbers_and_orders_by_rank`)*
- [x] A re-run with the same inputs uses the cache and yields identical analysis. *(`test_rerun_uses_cache_and_is_identical`: 0 provider calls, identical `analysis_sha256`)*
- [x] No unredacted text is sent to the LLM. *(`test_no_unredacted_text_reaches_the_llm`: a planted phone number is in the raw store but in no captured prompt, and every data-block value scans clean)*

### Status — 2026-10-14: **implemented and tested with a mocked LLM on synthetic fixtures; live-LLM smoke test and real-data validation pending**

| # | Deliverable | Status | Where |
|---|---|---|---|
| 1 | LLM layer | Done | `analysis/llm/`: `base.py` (`PromptRequest`, `LLMProvider` protocol); `anthropic_provider.py`; `client.py` (`LLMClient.complete_json`, `ResponseCache`, `TokenBudget`); `__init__.py` (`provider_from_settings`, `build_client`). See "How the LLM is called" below |
| 2 | Versioned prompts | Done | `analysis/prompts/theme_discovery.v1.txt`, `theme_assignment.v1.txt`, `actions.v1.txt`, and `prompts/__init__.py` (`load_prompt`, `data_block`) |
| 3 | Themes | Done | `analysis/themes.py`: stratified sample (≤ 300), discovery (≤ 5 themes, with 2–8 evidence IDs each), batched assignment (50 per call) with `other`, and the TF-IDF + KMeans fallback |
| 4 | Ranking | Done | `analysis/ranking.py`: per-theme stats and the ranking (below) |
| 5 | Quotes | Done | `analysis/quotes.py`: candidates, sentence-span snippets (≤ 35 words), selection with redistribution, verification, fail closed |
| 6 | Actions | Done | `analysis/actions.py`: exactly 3, one per top theme, validated |
| 7 | CLI and output | Done | `groww-pulse analyze` → `data/runs/{run_id}/analysis.json`. Exit codes: 0 ok, 1 analysis refused or failed (message includes the run ID), 2 usage or config error (for example `ANTHROPIC_API_KEY` not set) |
| — | Stage orchestration (beyond the plan) | Done | `analysis/pipeline.py`: `run_analysis()` runs the steps in order and writes nothing unless every check passes |
| — | Config (beyond the plan) | Done | `LLM_TIMEOUT_SECONDS` (default 120) and `ANALYSIS_MIN_REVIEWS` (default 20) in `config.py` and `.env.example` |
| — | Models and errors (beyond the plan) | Done | `Theme` gained `low_rating_count`, `rating_counts`, `rank`; `Quote` gained `leading_ellipsis`/`trailing_ellipsis`; `ActionIdea` gained `owner_area`. New errors: `InsufficientDataError`, `QuoteVerificationError` (both `AnalysisError`) |

**Stage order** (`run_analysis`):
1. Load `reviews.jsonl` through `load_analysis_reviews()`, the Phase 2 gate. It refuses synthetic fixtures unless a test passes `allow_synthetic=True`; the CLI never does.
2. Check there is enough data: at least 1 review, at least `ANALYSIS_MIN_REVIEWS` reviews, and at least 3 quote-eligible reviews. This happens **before any LLM call**.
3. Find themes with the LLM. If the provider is unavailable, fall back to clustering.
4. Compute statistics and rank the themes in code. Drop themes with no members. Require at least 3 ranked themes.
5. Select quotes in code, then verify them against `reviews.jsonl` and the raw store.
6. Generate action ideas with the LLM and validate them.
7. Write `analysis.json` atomically.

Any failure raises a typed error, and **no `analysis.json` is written**.

**How the LLM is called** (`LLMClient.complete_json`):
- **Structured outputs.** Every request sends the prompt's JSON Schema as `output_config.format`. Assignment and action schemas also use `enum`s, so the model can only return known review IDs, theme IDs, and owner areas. Every reply is still parsed with Pydantic (`extra="forbid"`) and passed to a semantic validator.
- **One repair retry.** An invalid reply is sent back once with the validator's message. Messages carry IDs and counts only, never input values, and any LLM-invented ID they echo is truncated. A second failure raises `LLMOutputError`. Only validated replies are cached.
- **Cache.** One file per reply in `data/cache/llm/{sha256}.json`. The key is `sha256(provider, model, prompt id, system, user, schema, effort)`. A cached entry that no longer validates is ignored.
- **Budget.** Before every call, the client checks `used + ceil(prompt chars / 3) + max_tokens ≤ LLM_MAX_TOKENS_PER_RUN`. If the call would not fit, it raises `BudgetExceededError` and makes no call. Actual usage is recorded after each call.
- **Anthropic provider.**
  - Calls `client.beta.messages.create` with the configured `LLM_MODEL` (default `claude-sonnet-5-5`).
  - Effort is `medium` for discovery and actions and `low` for assignment.
  - The SDK retries 408/409/429/5xx and connection errors with backoff (`max_retries=4`). What still fails becomes `LLMProviderError`, with the status and request ID only.
  - Server-side refusal fallback is on (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`): a declined request is retried on a fallback model in the same call. If the whole chain refuses, the client raises `LLMOutputError`.
  - The LLM gets **no tools**.
- **Groq provider** (`llm/groq_provider.py`, optional, `LLM_PROVIDER=groq` + `GROQ_API_KEY`; added after Phase 3 to avoid paid API usage).
  - Calls `chat.completions.create` with the configured `LLM_MODEL` (default `openai/gpt-oss-120b`, on the free tier). `response_format` is `json_schema` with `strict: true` for the gpt-oss and qwen3.8 models (constrained decoding); other models get best-effort mode. Every reply still goes through the same Pydantic and semantic validators and the same repair retry.
  - `reasoning_effort=low` and `include_reasoning=false`, because reasoning tokens count against the output cap and the per-minute limit. The request's `effort` is ignored.
  - **Free-tier sizing.** Groq counts prompt + `max_completion_tokens` against the per-minute limit (8,000 on the free plan). `build_client` gives the client `request_token_limit = GROQ_TOKENS_PER_MINUTE` and `max_output_tokens = GROQ_MAX_OUTPUT_TOKENS` (default 2,500). `LLMClient.fits()` keeps each first request, plus 1,000 tokens of repair head-room, under the limit. Discovery shrinks its stratified sample (never below 15); with the 67 real reviews it uses 28. Assignment halves its batches; with 67 reviews that gives 25/25/17, and every review is still assigned. A request that cannot fit raises `LLMProviderError` before any call. Without a limit (Anthropic), requests are byte-identical to before, so existing cache keys still hit.
  - **Pacing and retries.** A sliding 60-second window on requested tokens waits rather than failing. The SDK retries 408/409/429/5xx, timeouts and connection errors (`max_retries=4`, honouring `retry-after`). Mapping: 401/403/404 → "check GROQ_API_KEY and LLM_MODEL"; persistent 429 and 413 → `LLMProviderError`; `json_validate_failed`, `finish_reason=length`, and empty replies → `LLMOutputError`.
  - The model is never switched: there are no fallbacks and no `service_tier`, so nothing moves to another model or paid capacity.
  - Tests: `tests/unit/test_groq_provider.py` (no network) and `test_analysis_pipeline.py::test_analysis_under_groq_free_tier_limits`. The live smoke test runs on Groq with `LLM_PROVIDER=groq GROQ_API_KEY=… RUN_LIVE_TESTS=1`.
- **Deviation from architecture §6.4 (temperature 0):** `temperature` is not sent, because current Claude models reject non-default sampling parameters (HTTP 400). Reproducibility (NFR-4) comes from the cache: the same input gives the same prompts, so cache hits produce an identical analysis.

**Prompt-injection containment** (requirement 12):
- Review text reaches a prompt only through `data_block()`. It writes one JSON object per line inside `<reviews>` (or `<themes>`, `<quotes>`) tags. JSON escaping keeps quotes and newlines inside the string, and `<`, `>`, `&` are written as `<`-style escapes, so a review cannot close the tag or start a new section.
- No data is ever placed in a system prompt. Each system prompt says the data blocks are untrusted and never instructions.
- Every output is checked against the schema and the validators: known IDs only, at most 5 themes, no digits or PII in theme labels, no invented numbers, no quotation marks in actions, and no catch-all themes.
- The LLM never writes quotes and never calls tools.
- The corpus includes an "ignore all previous instructions … theme HACKED" review. A test proves it stays inside the data block and is classified as data.

**Theme ranking logic** (`ranking.py`). Every number is computed in code from the assignments:
- `count` is the number of members. `share = count / n_analyzed`, where `n_analyzed` includes `other`.
- `avg_rating` is the mean star rating. `rating_counts` gives the member count for each star value from 1 to 5.
- `low_rating_count` is the number of 1–2★ members, and `low_rating_share = low_rating_count / count`. This is the negative-rating distribution.
- `score = count × (1 + low_rating_share)`. This equals `count + low_rating_count` exactly, so the score is computed as an integer and float rounding can never create or break a tie.
- Sort by score (highest first), then count (highest first), then `theme_id` (ascending). Top 3 = ranks 1–3.
- `other` is never ranked. Themes with no members are dropped, with a warning.
- If fewer than 3 themes remain, the run fails with `InsufficientDataError`.
- `analysis.json` stores the formula text, each theme's `score`, `rank`, `share_pct`, and `low_rating_pct` (one decimal), and the `other` count and share.

**Quote selection and validation logic** (`quotes.py`). Quotes come from code only; the LLM never writes, edits, or picks them.
1. **Candidates.** Each top theme's `quote_eligible` members (lenient-emoji reviews never qualify).
   - Tier 0: reviews of 12–60 words whose snippet has no redaction placeholder. Everything else is tier 1.
   - Within a tier, candidates rank by TF-IDF cosine similarity of the snippet to the theme centroid, then `thumbs_up`, then `review_id`.
2. **Snippet.**
   - If the review has ≤ 35 words, the snippet is the whole text.
   - Otherwise it is the most representative contiguous run of whole sentences with 9–35 words.
   - The text is never paraphrased, re-cased, or cut mid-sentence. `leading_ellipsis` / `trailing_ellipsis` tell Phase 4 to add "…" *outside* the verbatim span.
   - A review whose only sentence is longer than 35 words has no snippet.
3. **Selection.**
   - First pass: one quote per top theme, in rank order.
   - If a theme has no candidate, the next unused candidates of the top themes are taken round-robin until there are exactly 3. No review is quoted twice.
   - Fewer than 3 candidates raises `InsufficientDataError`.
4. **Verification** (`verify_quotes`, before any quote is accepted). For each quote:
   - The review exists in `reviews.jsonl`.
   - `review.text[start:end] == quote.text`.
   - `text_sha256` matches the stored text.
   - The review is assigned to the quoted theme.
   - The rating and date match the review.
   - The review is quote-eligible, and the quote has no emoji.
   - The quote has ≤ 35 words.
   - The ellipsis flags match the span.
   - A PII scan of the quote is clean.
   - **Provenance:** `verify_provenance()` re-derives the redacted text from the raw store's original text and `original_sha256`.

   Exactly 3 quotes are required, with no duplicates. Any failure raises `QuoteVerificationError`, and nothing is written.

**Action validation** (`actions.py`). The LLM sees only the code-computed statistics of the top 3 themes and the 3 verified quotes. The reply is rejected (then repaired once) unless:
- there are exactly 3 actions, one per top theme;
- each action has ≤ 25 words, starts with a verb-like word (a heuristic deny-list of articles, pronouns, and similar openers), and is not a repeat;
- `owner_area` comes from a fixed list: Engineering, Product, Design, Customer Support, Payments, Trading Platform, Onboarding and KYC, Operations, Compliance;
- the rationale has ≤ 40 words;
- every number in the action and rationale is one of the statistics shown in the prompt (normalized, so `12` equals `12.0`);
- there are no quotation marks and no PII or redaction placeholders.

**What `analysis.json` contains:**
- `schema_version`, `run_id`, `created_at`, `warnings`, `llm_usage` (calls, cache hits, repairs, tokens, served models, prompt versions).
- `synthetic_fixture_data`: true if any review carries the fixture marker. Phase 4/5 must refuse such an analysis.
- `analysis_sha256`: a hash of the deterministic `analysis` object only.
- `analysis` contains:
  - `input`: the `reviews.jsonl` hash, counts, and date range;
  - `method`: `llm` or `fallback_clustering` and the reason, provider, model, prompt versions, ranking formula, and owner areas;
  - `themes`: all ranked themes with statistics and member IDs;
  - `other`: its count and share, plus how many reviews defaulted to `other` because the LLM left them out;
  - `top_theme_ids` and `assignments` (review ID → theme ID or `other`);
  - `quotes` and `actions`.

**Test results (2026-10-14, Python 3.11.16, local):**
- `pytest`: **492 passed, 2 skipped**. The skips are the opt-in live MCP test and the new opt-in live LLM test. Phase 3 added 109 tests.
- Coverage on `groww_pulse` is **98%** line+branch. By module:
  - `llm/client.py`, `llm/anthropic_provider.py`, `llm/base.py`, `ranking.py`: 100%;
  - `actions.py` 99%, `pipeline.py` 98%, `themes.py` 97%, `quotes.py` 95%, `prompts/__init__.py` 95%, `llm/__init__.py` 89%.
- New test modules. All use a fake LLM provider and synthetic fixtures from `tests/fixtures/analysis.py`, labelled `SYNTHETIC TEST FIXTURE`. The 27-review corpus goes through the real Phase 2 pipeline into a temporary `DATA_DIR`.
  - `test_llm_client.py` (19):
    - cache hit makes 0 provider calls; a changed prompt misses the cache;
    - malformed JSON is repaired once, then succeeds; malformed twice raises `LLMOutputError`; invalid output is never cached;
    - a semantic validator drives the repair;
    - error messages never echo values;
    - the budget is checked before the call and accumulates actual usage;
    - corrupt and stale cache entries are ignored;
    - provider wiring from settings;
    - Anthropic request shape: `output_config`, fallback beta, no temperature, no tools;
    - a refusal raises an error; 401/404/429/500 and connection errors map to `LLMProviderError`.
  - `test_prompts.py` (7): templates load and carry the injection rules; a missing placeholder fails; an attack string with `</reviews>`, headings, and `&` cannot escape the data block and round-trips exactly.
  - `test_themes_ranking.py` (25):
    - more than 5 themes, 0 themes, duplicate names, unknown evidence IDs, too little evidence, catch-all names, digits, PII, and over-long names or descriptions are each rejected after one repair;
    - missing assignments go to `other`; unknown, repeated, or wrong-theme IDs are rejected;
    - batching (120 reviews → 3 calls) and schema enums;
    - the stratified sample is bounded, deterministic, and covers every rating and week;
    - fallback clustering gives 3–5 themes deterministically; too little text is refused; the LLM-down path uses the fallback;
    - hand-computed ranking with tie-breaks and `other`/empty themes excluded.
  - `test_quotes_actions.py` (37):
    - sentence spans; whole vs. sentence-span snippets; one per theme, verbatim;
    - ellipsis flags on a long review; lenient (ineligible) reviews never chosen;
    - redistribution still gives exactly 3; fewer than 3 fails closed;
    - placeholder and word-range preferences;
    - verification passes; a one-character tamper fails; a tampered stored review fails the hash and provenance checks;
    - missing source, wrong theme, wrong rating, wrong ellipsis, missing raw store, wrong count, duplicates, ineligible, and PII each fail;
    - 14 invalid-action cases are rejected, including invented numbers; repair and ordering work.
  - `test_analysis_pipeline.py` (17):
    - the full end-to-end run, with counts, percentages, scores, and quotes reconciled and re-verified;
    - a cached re-run is identical with 0 calls;
    - no unredacted text reaches the LLM; the injection review is contained;
    - fallback clustering when the LLM is down for themes; total outage, invalid AI output, budget exceeded, fewer than 3 themes, and a missing raw store each fail with nothing written;
    - synthetic data is refused without opt-in;
    - a missing file, an empty dataset, too few reviews, or too few quote-eligible reviews is refused before any LLM call;
    - an unconfigured provider raises `ConfigError`.
  - `test_cli.py` (+4): `analyze` without a key exits 2; with no reviews or an empty dataset it exits 1 and writes nothing; success prints the summary.
  - `tests/integration/test_llm_live.py`: an opt-in `@pytest.mark.live` smoke test against the real Claude API on the synthetic corpus in a temporary directory. It is skipped unless `RUN_LIVE_TESTS=1` and `ANTHROPIC_API_KEY` are set. **It has not been run**: no API key is configured in this environment.
- `ruff check`, `ruff format --check`, `mypy --strict` (35 source files; the new test modules also pass `mypy --strict`), and all 9 pre-commit hooks, including detect-secrets, are clean.
- Bugs found and fixed while testing:
  - a regex written through a shell heredoc picked up a literal backspace character instead of `\b`;
  - a fake API key passed as a keyword argument in a test tripped detect-secrets; it now comes from `monkeypatch.setenv`.

**Real-data validation status: NOT DONE.** There is no `data/` directory, no real Groww review has been imported, preprocessed, or analysed, and no `ANTHROPIC_API_KEY` is configured. The blockers are the same approved data source as in Phases 1–2, plus an API key. No analysis was generated from synthetic data outside temporary test directories; the CLI refuses synthetic input, and an empty or insufficient dataset produces an error, not an analysis. On the first real export, the operator should:
1. Set `ANTHROPIC_API_KEY` (and optionally `LLM_MODEL`). Optionally run `RUN_LIVE_TESTS=1 pytest tests/integration/test_llm_live.py` once, to confirm the live API path on fixtures.
2. Run `groww-pulse import …`, then `groww-pulse preprocess --as-of <date>`, then `groww-pulse analyze`.
3. Review `data/runs/{run_id}/analysis.json`:
   - Are the themes distinct and specific?
   - Is the `other` share reasonable (if it is above ~30%, the themes are too narrow)?
   - Are the assignments right? Spot-check about 50 reviews against their theme.
   - Do the 3 quotes read well and represent their themes?
   - Are the 3 actions concrete?
   - Is `llm_usage` within budget?
4. Run `groww-pulse analyze` again and confirm `provider_calls: 0` and the same `analysis_sha256`. Record the results here.

**Known limitations:**
1. **No live-LLM verification yet.** Every test uses a fake provider. The Anthropic request shape is unit-tested against a stub, but the real API (structured-output schema acceptance, the fallback beta, token usage, theme quality) is unverified until the live test runs.
2. **Temperature is not set** (see the deviation above). Without the cache, two runs can produce different themes. Deleting `data/cache/llm/` or changing the model or prompts gives a new, non-identical analysis.
3. **Fallback clustering is only partial.** If the provider is completely down, action generation also fails, so the run fails; the fallback only covers theme discovery and assignment failures. Fallback themes are named by their top terms ("Fallback: orders / market / slow"), which is weak, and every review is assigned (no `other`). The k range is 3–5, not the architecture's 2–5, because the pulse needs 3 themes.
4. **Three themes are required.** Data with only 1–2 non-empty themes fails rather than producing a pulse with fewer themes.
5. **The verb check is a heuristic.** "Starts with an imperative verb" is enforced with a deny-list of non-verb openers, so a noun opener not on the list would pass. Concreteness is encouraged by the prompt and the `owner_area` enum, not proven.
6. **The number check is literal.** Any digit in an action must equal a statistic in the prompt. Harmless numbers ("24/7", "two-step" written as "2-step") are rejected and repaired. Spelled-out numbers ("five days") are not detected.
7. **Sentence splitting is a heuristic.** It splits on `. ! ?` followed by a space, so abbreviations such as "e.g. " can split a sentence. Quotes are still verbatim, but a span may start mid-thought.
8. **Representativeness is lexical.** TF-IDF similarity favours reviews that share vocabulary with the theme, not necessarily the most insightful review.
9. **Budget estimate.** The pre-call check assumes about 3 characters per token plus the full `max_tokens`. At the default 400,000-token budget, very large datasets (more than about 3,000 reviews, depending on length) may stop with `BudgetExceededError`; raise `LLM_MAX_TOKENS_PER_RUN` deliberately if needed.
10. **The cache is unbounded.** `data/cache/llm/` stores validated replies only (review IDs, theme labels, actions; no review text). It is gitignored but never pruned.
11. **Provenance needs the raw store.** Quote verification requires `data/raw/reviews_raw.jsonl`. If raw-store retention pruning is added later, it must keep the source records of reviews that may be quoted.
12. **Multi-turn repair drops thinking blocks.** The repair request resends only the previous reply's text. This is valid for the API, but the model does not see its earlier reasoning.

---

## Phase 4 — Report Generation and Validation

**Objective:** Render the weekly pulse and gate delivery on strict validation.

### Deliverables
1. `report/render.py`: plain-text and Markdown renderings from the template in architecture §6.5, with the ISO week label and audit marker line.
2. `report/validators.py`:
   - ≤ 250 words, using the shared `count_words`;
   - exactly 3 themes, 3 quotes, and 3 actions;
   - every quote re-verified against `reviews.jsonl`;
   - PII scan;
   - every number in the body is a computed number.
3. Word-budget handling: shrink actions or descriptions through a constrained re-prompt, never shrink quotes, and otherwise fail with `ReportValidationError`.
4. `content_hash` computation and `data/runs/{run_id}/pulse.{md,txt,json}`.
5. CLI: `groww-pulse report`.

### Testing requirements
- Snapshot tests of the rendered output from fixture analysis.
- Word-count boundaries: 250 passes, 251 fails, and the shrink path is exercised.
- Validator negatives: 2 or 4 quotes, an edited quote, a planted email or phone number, and a fabricated number.
- The hash is stable across runs and changes when the content changes.

### Acceptance criteria
Mechanism criteria are met on labelled synthetic fixtures. **Every criterion that says "real" is not met**, because no real review data exists yet.
- [x] The pulse renders in at most 250 words, with exactly 3 themes, 3 verbatim quotes, and 3 actions. *(Synthetic only: `test_report_end_to_end`, `test_phases_one_to_four`, the word-limit boundary and shrink tests.)*
- [x] *(needs real data)* All validators pass on a **real** run. *(Mechanism: any failure blocks writing and names each failed check and reason; proven by 40+ refusal tests.)* **Update 2026-10-17:** the corrected 2026-W42 pulse (`84d6f68b…`, 211 words) passes all 6 validators.
- [x] The rendered pulse contains no PII. *(Synthetic only: `no_pii` validator, `test_pii_anywhere_in_the_report_fails_safely`, `test_pii_in_rendered_report_fails_safely`; the planted phone number never appears.)*

### Status — 2026-10-14: **implemented and tested on labelled synthetic fixtures; NOT production-validated (no real data)**

| # | Deliverable | Status | Where |
|---|---|---|---|
| 1 | Plain-text and Markdown rendering with ISO week and audit marker | Done | `report/render.py`. Deterministic template from architecture §6.5. The audit marker `[groww-pulse:{iso_week}:{hash8}]` sits above the hashed body in the saved files |
| 2 | Validators | Done | `report/validators.py`: `word_limit`, `three_three_three`, `quotes_verbatim`, `numbers_computed`, `no_pii`, `no_synthetic` |
| 3 | Word-budget handling | Done, **with a deliberate deviation** | `render_within_limit()` drops action owner areas, then theme average ratings, and otherwise fails. **No LLM re-prompt** (see decision below) |
| 4 | `content_hash` and `pulse.{md,txt,json}` | Done | `report/pipeline.py` writes `data/runs/{run_id}/pulse.md`, `pulse.txt`, then `pulse.json` last, all atomically and only after every check passes |
| 5 | CLI | Done | `groww-pulse report [--analysis PATH]` (default: newest `analysis.json` by `created_at`). Exit codes: 0 ok, 1 refused (message lists every failed check and includes the run ID), 2 usage error. Never allows synthetic data |
| — | Input re-verification (beyond the plan) | Done | `build_content()`, described below |

**Decision: Phase 4 makes no LLM calls.** The plan allowed a constrained LLM re-prompt to shorten actions. It was not implemented, so the report stage is deterministic, needs no API key, and cannot invent or reword anything. The worst-case pulse can exceed 250 words: three 35-word quotes, 8-word theme names, and 25-word actions. In that case the run fails closed with the exact word count, and the operator re-runs `analyze`. Measured: with three maximum-length quotes, shrinking fixes overruns of up to 12 words.

**What the report stage verifies before rendering** (all re-derived from stored data):
1. **Integrity.** `analysis.json` must be readable JSON with schema version 1, and its `analysis_sha256` must match the recomputed hash. An edited file is refused.
2. **Origin.** `synthetic_fixture_data: true` is refused unless a test passes `allow_synthetic=True`. `reviews.jsonl` must be byte-identical to the analysed file and pass the Phase 2 gate again; a stale analysis is refused. The rendered body is also scanned for the fixture marker.
3. **Sufficiency.** `n_analyzed` must equal the reviews in `reviews.jsonl` and be at least `ANALYSIS_MIN_REVIEWS`.
4. **Statistics.**
   - Assignments must cover exactly the stored reviews, and only known themes.
   - Every theme's members, count, share, `share_pct`, average rating, low-rating count and share, `rating_counts`, score, and rank is recomputed with `ranking.py` and must match.
   - `top_theme_ids` must be the first 3 of the recomputed ranking.
5. **Quotes.**
   - `verify_quotes()` from Phase 3: exactly 3, verbatim `text[start:end]`, `text_sha256`, theme membership, rating and date, quote eligibility, ≤ 35 words, ellipsis flags, PII scan, and **raw-store provenance**.
   - Each quote must also come from a top theme.
6. **Actions.** The Phase 3 `action_problems()` runs again: exactly 3, one per top theme, at most 25 words, starting with a verb, an allowed owner area, only computed numbers, and no PII. They must also be in rank order.
7. **Window.** The funnel whose `output_sha256` equals `reviews_sha256` supplies the window (ISO week of the window end) and `n_collected` (the funnel's `window` step output, i.e. valid reviews inside the window). The analysed reviews' dates must fall inside it.

**What the rendered pulse is checked for** (all six validators must pass):
- **Word limit:** at most 250 words in both `pulse.txt` and `pulse.md`, counted with the shared `count_words`, audit marker included.
- **3/3/3:** exactly 3 themes, 3 quotes, and 3 actions, in the verified content and as rendered lines in each section.
- **Verbatim quotes:** each quote appears exactly once in each rendering, as `…"verbatim span"…`: the quotation marks enclose only the verbatim span, and ellipses go outside them.
- **Computed numbers only:** every number outside the quotes is a computed value (ISO week, window dates, counts, ranks, percentages, average ratings, quote ratings, quote dates).
- **No PII:** theme names, quotes, actions, and both full files are scanned. A hit **fails the run**, naming where it was and its type, never its value. Nothing is redacted to make the scan pass.
- **No synthetic data:** neither the fixture marker nor the synthetic banner appears unless synthetic data was explicitly allowed (tests only).

**Outputs** (`data/runs/{run_id}/`):
- `pulse.txt`: the marker line, then `body_text`.
- `pulse.md`: the marker line, then `body_markdown`.
- `pulse.json`:
  - `schema_version`, `run_id`, `created_at`;
  - `synthetic_fixture_data`, and `publishable`, which is false for synthetic data;
  - `audit_marker`, `detail_level`, `warnings`;
  - `source`: analysis path, run ID, and hash; `reviews_sha256`; funnel path; preprocessing run ID; theme method;
  - every validator result, and the full `Pulse` model (themes, quotes with review IDs, actions, bodies, `word_count`, `content_hash`).

Review IDs appear only in `pulse.json`, for audit, and never in the pulse body.

**Example (synthetic fixture, from `test_report_end_to_end`; not real data):** 191 words, detail level 0, ISO week 2026-W42. A synthetic run puts `SYNTHETIC TEST DATA — NOT FOR PUBLICATION` above the title.

**Test results (2026-10-14, Python 3.11.16, local):**
- `pytest`: **769 passed, 2 skipped** (the opt-in live MCP and live LLM tests). Coverage on `groww_pulse` is **98%** line+branch: `report/render.py` 100%, `report/validators.py` 98%, `report/pipeline.py` 98%.
- Phase 4 added 88 tests. All use labelled synthetic fixtures (`tests/fixtures/report.py`) in temporary directories.
- `test_report_render.py` (35):
  - an exact snapshot of the plain text and Markdown;
  - a stable `content_hash` that changes when the content changes;
  - ellipsis placement in all 4 combinations; quote text never escaped or altered;
  - detail levels; the fallback note and synthetic banner; ISO-week edge cases (W01, W53);
  - word limit: 250 passes and 251 fails; the shrink path reaches levels 0, 1, and 2 at exact word counts, and fails closed when even the compact form is too long;
  - 2 or 4 quotes, or 2 actions, fail; an edited or duplicated quote fails; a fabricated count fails; numbers inside quotes are not treated as statistics; an action number outside the stats fails;
  - PII in an action, a theme name, or a quote fails without echoing the value; synthetic content is refused.
- `test_report_pipeline.py` (47): the full end-to-end report, determinism (byte-identical files), and newest-analysis selection. Refusals, each with nothing written:
  - synthetic data in production mode; a missing, corrupted, or wrong-version analysis;
  - an edit without re-hash (integrity), and a broken structure;
  - 8 fabricated statistics or ranking variants (re-signed);
  - 7 unverifiable quote variants: edited, 2 or 4 quotes, non-top theme, unknown review, wrong rating, wrong ellipsis;
  - 5 invalid action variants: count, invented number, PII, order, theme;
  - `reviews.jsonl` changed or missing; raw store missing or tampered (provenance); funnel missing, unreadable, without a window step, or not covering the reviews;
  - too little data; duplicate theme IDs; a theme with no members; fewer than 3 themes; an `n_analyzed` mismatch; reviews failing the Phase 2 gate;
  - PII in the rendered report (fails safely, value never echoed).

  It also tests the fallback-clustering label.
- `tests/integration/test_report_e2e.py` (2, offline):
  - a synthetic UTF-16 Play Console CSV goes through the real importer, preprocessing, analysis (fake LLM), and report; quotes are checked verbatim against the original raw text;
  - the production path (no `allow_synthetic`) refuses the fixtures at preprocess, analyze, and report, and no pulse file is created.
- `test_cli.py` (+4): `report` with no analysis, with a synthetic analysis, with a missing `--analysis` path, and a successful run (via an explicit test-only wrapper).
- `ruff check`, `ruff format --check`, `mypy` (configured scope) and `mypy --strict` on `src`, `scripts`, `app`, and the new test modules, all 9 pre-commit hooks (run on tracked and untracked files), and the CI-equivalent detect-secrets scan are clean.
- Bugs found and fixed while testing:
  - the Markdown footer `_…Agent._` counted one extra word, because `_` is a word character; it now uses `*…*`, and both renderings count identically;
  - a shrink-test fixture used 36-word quotes, over the 35-word cap; fixed to exact 35-word quotes;
  - **tooling conflict (pre-existing):** pre-commit pins ruff `v0.6.9` but the dev environment (and CI's `ruff format --check`) uses ruff 0.17.0. The two formatted one signature in `test_quotes_actions.py` differently, so a commit through pre-commit would have failed CI. That signature now uses a type alias that both versions format identically. **Recommendation:** align the pre-commit `rev` with the locked ruff version.

**Real-data validation status: NOT DONE.** No real Groww review has been imported, preprocessed, analysed, or reported. No `ANTHROPIC_API_KEY` is configured, and the live-LLM smoke test has never run. Every pulse produced so far came from labelled synthetic fixtures in temporary directories, carries the synthetic banner, and is marked `publishable: false`. The CLI refuses such an analysis. On the first real run, the operator should:
1. Run `groww-pulse import …`, `preprocess --as-of <date>`, and `analyze`, then `groww-pulse report`.
2. Read `pulse.txt` in full. Check that:
   - every quote reads naturally and matches its source review in `reviews.jsonl`;
   - the counts match `analysis.json`;
   - the window is correct (posting lag, R15);
   - nothing identifies a reviewer.
3. Record the word count, detail level, and any validator failure here.

**Known limitations:**
1. **No LLM shortening.** An over-long pulse fails, and `analyze` must be re-run. A new analysis may pick different quotes or actions, and nothing steers it toward shorter ones.
2. **The PII scan is the same rule-based scanner as Phase 2.** It cannot detect free-text names without a cue (see Phase 2 limitation 4). The report stage only re-checks text that Phase 2 already redacted.
3. **Markdown is not escaped,** so quotes stay verbatim in the source. A quote containing `*` or `_` may render with emphasis in a Markdown viewer. The text itself is unchanged, and Phase 5 sends plain text.
4. **`n_collected` means valid reviews inside the window** (the funnel's `window` step), not every row in the export, because rows outside the window are skipped at import.
5. **The newest analysis is chosen by its `created_at`,** not by ISO week. Pass `--analysis` to report on a specific run.
6. **Re-running creates a new run directory each time.** Identical inputs give byte-identical `pulse.txt` and `pulse.md` and the same `content_hash`, which Phase 5's idempotency key relies on. Old runs are not pruned.
7. **Theme names and descriptions are LLM wording** validated in Phase 3 (no digits, no PII, length limits). The report stage checks the numbers but cannot judge whether a name fairly describes its theme.

**Phase 5 readiness:** the engineering inputs Phase 5 needs exist: a validated `pulse.txt` and `pulse.md`, a stable `content_hash`, the ISO week, and the audit marker. Starting Phase 5 is still gated:
- **Live delivery** is blocked by the Phase 0 security gate (the MCP server is unauthenticated) and by a test Doc ID and recipient.
- **Production delivery** also needs one real, validated pulse, which needs the approved data source and an API key.

Building and unit-testing the Phase 5 client against a fake MCP server is not blocked.

**Update 2026-10-17:** both gates cleared. A real, validated pulse exists, and the bearer-auth Railway deployment passed the security gate (see [As-built status](#as-built-status-2026-10-19)).

---

## Phase 5 — MCP Delivery

**Objective:** Append to the Google Doc and create a Gmail draft safely, idempotently, and honestly.

### Deliverables
1. `delivery/mcp_client.py`:
   - Streamable HTTP client built on the official `mcp` SDK;
   - `check_mcp()` (handshake + `tools/list` + schema check);
   - allowlist enforcement (`ForbiddenToolError`);
   - timeouts, transport-only retries, and strict `CallToolResult` parsing.
2. `delivery/ledger.py`: append-only JSONL ledger with separate keys for the Doc append and the draft.
3. `delivery/publisher.py`:
   - dry-run preview of exact tool names and arguments;
   - confirm mode with the ledger check;
   - `--force` override;
   - a `DeliveryReport` built only from real responses.
4. CLI: `groww-pulse check-mcp` and `groww-pulse publish --dry-run | --confirm [--force]`.

### Testing requirements
- Unit tests use an **in-process fake MCP server**, built with the `mcp` SDK's server utilities, that records calls. Cover:
  - a successful append and a successful draft;
  - `isError=true` raises `MCPToolError`, and the ledger records the failure;
  - a malformed result raises an error;
  - a missing tool in `tools/list` raises `MCPUnavailableError`;
  - a root URL that returns 200 while the MCP handshake fails is reported **unhealthy**.
- **Send-blocking test:** any attempt to call `gmail_send_email` raises `ForbiddenToolError`, and the fake server receives 0 calls. A static test greps `src/` to confirm the only send-tool reference is in the deny path.
- Idempotency: the second confirm for the same week and hash makes 0 calls. A doc-success/draft-fail run retries only the draft.
- Dry run makes 0 network calls.
- Secrets and `GOOGLE_DOC_ID` are masked in logs.
- Live integration test (`@pytest.mark.live`) against the deployed server, using a test Doc and the operator's own address. It runs manually and is never in CI.

### Acceptance criteria
*As of 2026-10-14:* **no Google Doc had been appended to and no Gmail draft created by this code.** Every write in the tests went to an in-process fake MCP server. **Update 2026-10-17:** one real delivery succeeded through the bearer-auth Railway deployment (see the criteria below).
- [x] `check-mcp` passes against the deployed server via `tools/list`. Root-URL reachability alone never passes. *(Live, read-only, 2026-10-14: healthy, protocol `2025-11-25`, contract compatible. The opt-in test `test_phase5_client_health_and_security_gate` passed. A fake answering 200 without MCP is reported unhealthy.)*
- [ ] *(partly met)* A confirmed publish appends the pulse to the configured Google Doc, confirmed by both the MCP response and a manual look at the Doc. **Update 2026-10-17:** the MCP response confirms it (`ok: true`, `status: appended`, 1,364 characters). No record of the manual look is in the repository.
- [ ] *(partly met)* A Gmail draft exists with the pulse, confirmed by both the MCP response and a manual look at Drafts. **Update 2026-10-17:** the MCP response confirms it (`ok: true`, `status: draft_created`); `email_sent: false`. No record of the manual look is in the repository.
- [x] No email is sent in any test or run, and the send tool is unreachable from code. *(The allowlist raises `ForbiddenToolError` before any I/O, and the fake receives 0 calls. A static test shows the send tool's name exists as code only in `contract.py`. No code path sends.)*
- [x] Re-publishing the same week is skipped and reported as such. *(Fake server: 0 HTTP requests on the second run.)* **Update 2026-10-17:** confirmed live: the repeat `publish --confirm` reported `skipped_already_delivered` on both targets. Since 2026-10-18, a pulse with different content for an already delivered week is `blocked_week_already_delivered`, and `--force` does not override it.
- [x] Failures show the server's error code and masked message, and nothing is reported as delivered unless the server said so. *(Fake server: `isError` → `failed`; malformed or ambiguous → `outcome_unknown`; `delivered` only on `ok: true` plus the expected status.)*

### Status — 2026-10-14: **implemented and tested against a fake MCP server; live delivery BLOCKED by the security gate; no real Doc append or draft yet**

| # | Deliverable | Status | Where |
|---|---|---|---|
| 1 | MCP client | Done | `delivery/mcp_client.py`: official `mcp` SDK 1.30, Streamable HTTP to the root URL, bounded handshake, `tools/list` contract check on every session, allowlist before I/O, strict result parsing, auth probe, typed errors |
| 2 | Ledger | Done | `delivery/ledger.py`: append-only, fsynced `data/runs/ledger.jsonl`; separate hashed keys for the Doc and the draft; statuses `pending`/`succeeded`/`failed`/`unknown`; fails closed when corrupt |
| 3 | Publisher | Done | `delivery/publisher.py`: publishable-pulse loader, dry-run preview, triple opt-in, security gate, ledger decisions, `--force`, `delivery.json` |
| 4 | CLI | Done | `groww-pulse check-mcp` (exit 0 healthy, 1 unhealthy, 2 config; warns when the gate fails) and `groww-pulse publish [--pulse PATH] [--confirm] [--force]` (exit 0 ok or dry run, 1 refused or not fully delivered, 2 config) |
| — | Errors | Done | `MCPAuthError` (subclass of `MCPUnavailableError`), `SecurityGateError` |

**What `publish` does:**
1. **Loads only a validated, publishable Phase 4 pulse** (newest `pulse.json` by `created_at`, or `--pulse`). It refuses a pulse if:
   - it is synthetic, `publishable: false`, has a failed or missing Phase 4 validator, or has the wrong schema version;
   - its `content_hash`, audit marker, or the `pulse.txt`/`pulse.md` files don't match `pulse.json`;
   - its body does not re-render byte-for-byte from its stored themes, quotes, and actions;
   - any Phase 4 validator fails when re-run (word limit, 3/3/3, verbatim quotes, computed numbers, PII, synthetic).
2. **Plans exactly two calls:**
   - `google_docs_append_content(documentId, content=pulse.txt, ensureLeadingNewline=true)`;
   - `gmail_create_draft(to=[…], subject="Groww Review Pulse — {iso_week}", body=pulse.txt, bodyType="text")`. The draft contains the full pulse, not a Doc link, so no Doc ID goes into an email.
3. **Dry run (default):** no network I/O. It shows the exact arguments, with the Doc ID and recipients masked, and the ledger decision per target.
4. **Live, only with all of:**
   - `--confirm`;
   - `DELIVERY_MODE=confirm`;
   - `MCP_SERVER_URL`, `GOOGLE_DOC_ID`, and `PULSE_EMAIL_TO`;
   - `MCP_AUTH_TOKEN`.

   These are all checked before any network I/O. If every target is already delivered, it stops there with 0 calls.
5. **Health and security gate:** handshake, `tools/list`, and contract, then the auth probe (no credentials, wrong token). If the server accepts either, the run raises `SecurityGateError` and nothing is written. There is no override.
6. **Writes**, Doc first, then draft. A `pending` ledger entry is written before each call, then the outcome:
   - `delivered` (server confirmed);
   - `failed`, retried next run (the server returned `isError`, or the request never reached it);
   - `outcome_unknown`, blocked until `--force` (connection lost after sending, malformed result, or a different document). The run stops before the draft.
7. **Idempotency keys:** `sha256(tool | iso_week | content_hash | documentId)` for the Doc, and `sha256(tool | iso_week | content_hash | sorted lower-case recipients)` for the draft. Already succeeded → skipped. `pending` (crash mid-call) or `unknown` → blocked until the operator checks the Doc and Drafts and re-runs with `--force`. A changed recipient list creates a new draft without re-appending the Doc.

**Failure handling, as tested:**

| Situation | Result |
|---|---|
| Server unreachable | Handshake retried 2×, then `MCPUnavailableError`; ledger untouched |
| Root URL answers 200 but not MCP | Handshake times out → unhealthy |
| 503s during handshake | Retried; succeeds if the server recovers |
| Our token rejected (401) | `MCPAuthError`; no writes |
| Server accepts unauthenticated clients | `SecurityGateError`; no writes |
| Tool missing or schema changed (`listChanged`) | `MCPUnavailableError` before any write |
| `isError` (e.g. `DOC_NOT_FOUND`, `GMAIL_DRAFT_CREATE_FAILED`) | `failed` with the server's code and masked message; retried next run |
| Google auth broken on the server (`GOOGLE_AUTH_REQUIRED`, …) | `failed`, flagged as the server owner's problem |
| Connection refused / 401 on the call itself | `failed` (never reached the server); retried next run |
| Connection dropped after the write was sent | `outcome_unknown`; draft not attempted; blocked until `--force` |
| Malformed or unexpected success / different document | `outcome_unknown`; blocked |
| Doc OK, draft failed (partial) | Report not ok; the next run skips the Doc and retries only the draft |
| Process crash mid-call | `pending` left in ledger → blocked |
| Corrupt ledger | Delivery refused |

**Secrets:**
- `MCP_AUTH_TOKEN` and `GOOGLE_DOC_ID` are registered with the log masker.
- Recipients are never logged.
- `delivery.json` and the dry-run preview show `<GOOGLE_DOC_ID>` and `<recipient n>`.
- Server error messages are masked.
- Ledger keys are hashes.
- The `mcp` SDK logger is now capped at WARNING, like `httpx`, because the SDK logs full tool arguments at DEBUG. The masking formatter also redacts them; this is defence in depth.

**Fake MCP server** (`tests/fixtures/fake_mcp.py`):
- the SDK's low-level `Server`, serving the **exact tool definitions recorded from the deployed server** (`docs/mcp-tools-observed.json`), so argument validation matches the real contract;
- reached through the real `streamable_http_client` over an in-process `httpx` transport, with no network;
- the transport simulates bearer auth, a refused connection, 503s, a non-MCP 200, and a connection dropped after the server performed the write;
- every request and tool call is recorded.

**Test results (2026-10-14, Python 3.11.16, local):**
- `pytest`: **848 passed, 3 skipped** (opt-in live tests), up from 769. Coverage on `groww_pulse` is **98%**: `mcp_client.py` 98%, `ledger.py` 98%, `publisher.py` 94%.
  - The uncovered publisher lines are mostly defensive branches: a session failing after results, non-`DeliveryError` session failures, and an over-limit `word_count`.
  - The line that stores the health report is reported uncovered even though the live-delivery test asserts its effect. This is a coverage-tracing artefact after `asyncio.run`.
- Phase 5 added 78 tests:
  - `test_mcp_client.py` (34):
    - gate pass and fail, a missing token, rejected credentials;
    - a missing tool, a changed schema, a non-MCP 200, unreachable (3 attempts), 503 retry;
    - forbidden tools never reach the server, and the static send-tool check;
    - strict parsing of 6 Doc outcomes, masked errors, draft success;
    - schema rejection of an unexpected argument, 4 malformed results, `isError` without an envelope, 7 call-failure classifications, typed auth error.
  - `test_publisher.py` (38):
    - synthetic and missing pulses; 7 tampered `pulse.json` variants; PII in the body (rejected by the re-render check); PII in structured content, re-signed (rejected by the `no_pii` validator); an edited `pulse.txt`;
    - dry run with 0 requests and masked output; dry run without configuration; the confirm flag vs `DELIVERY_MODE`; missing Doc ID or recipients; a missing token;
    - an unauthenticated server; a rejected token; unreachable; a changed contract;
    - full success with exact arguments and a pending→succeeded ledger;
    - a re-run with 0 requests; a partial draft failure and retry; a Google auth failure;
    - 3 ambiguous outcomes (blocked, then forced); 2 never-sent failures (retried); a crash `pending` entry; new recipients; a new Doc;
    - secrets absent from emitted logs, records, report, `delivery.json`, and the ledger;
    - key properties; a corrupt ledger.
  - `test_cli.py` (+6): `check-mcp` config error, gate pass and fail; `publish` refusing a synthetic pulse, no pulse, and dry run → refused → live → skipped.
- **Phase 4 regression fix:** the `numbers_computed` validator echoed uncomputed numbers in its error message, so a planted phone number would have been printed. Numbers longer than 6 digits are now shown as `<N-digit number>` (+1 test).
- **Phases 1–4:** all earlier tests pass unchanged.
- `ruff check`, `ruff format --check`, `mypy` (configured scope), `mypy --strict` on `src`/`scripts`/`app` and the new test modules, all 9 pre-commit hooks (tracked and untracked files), and the detect-secrets scan are clean. Three pre-existing `mypy --strict` errors in `tests/unit/test_cli.py` (lines 27, 147, 148) are outside the configured scope and unchanged.
- **Live, read-only check (2026-10-14).** Phase 5 client against `https://mcp-server-4-production-0fca.up.railway.app/`, with no tool called:
  - `healthy: true`, protocol `2025-11-25`, contract compatible, `forbidden_tools_present: ["gmail_send_email"]`;
  - `auth_enforced: false` ("the server accepted a handshake with no credentials"), so **`security_gate_passed: false`**.
- Bugs found and fixed while testing:
  - my first session code held the SDK's anyio contexts open manually, so a 401 during the auth probe surfaced as a bare `CancelledError`; it now uses structured `async with`;
  - a non-MCP 200 at the root made the handshake hang until the 60 s read timeout; the handshake is now time-bounded;
  - our own typed errors raised inside a session (`ForbiddenToolError`, contract changed) came back wrapped in the SDK's `ExceptionGroup`; they are now unwrapped;
  - the Phase 4 number echo described above.

**What remained before live Google Docs and Gmail testing** (as of 2026-10-14; in order; all need the operator or the MCP server owner). **Update 2026-10-17:** a bearer-auth deployment passed the health check and security gate inside the publish, a real pulse was produced with Groq (not Anthropic), and steps 7 and 9 were run with the results above. The repository does not record whether the configured Doc and recipient were test targets (step 4), whether step 5 was run, the manual check in step 8, or step 10.
1. **Fix the MCP server's authentication (blocker).** The deployed server must reject clients without a valid bearer token: Step 1 of [mcp-server-security.md](mcp-server-security.md), or the operator's own hardened fork. Step 0.1 (`GOOGLE_TOKENS_JSON`) and Step 3 (lock down the OAuth routes) are strongly recommended first, so the authorized Google account cannot be swapped. Until then, `publish --confirm` raises `SecurityGateError` by design.
2. **Issue a client key** and set `MCP_AUTH_TOKEN` (environment or secret store only).
3. Run `groww-pulse check-mcp`. Expect `healthy: true`, `auth_enforced: true`, and `security_gate_passed: true`. Ideally `gmail_send_email` no longer appears in `forbidden_tools_present` (server Step 2).
4. **Use a test Doc and your own address:** set `GOOGLE_DOC_ID` to a TEST Doc the server's Google account can edit, and `PULSE_EMAIL_TO` to the operator's own address.
5. Optionally run the Phase 0 contract writes first (`python scripts/check_mcp.py --test-append --test-draft --i-understand-this-writes`). Record the redacted results in mcp-contract.md §6, and confirm **which Google account** received them.
6. **Produce a real, publishable pulse.** This needs the approved data source, a real export, and `ANTHROPIC_API_KEY` (Phases 1–4). A synthetic pulse is refused.
7. Run `groww-pulse publish` (dry run) and review the preview, then set `DELIVERY_MODE=confirm` and run `groww-pulse publish --confirm`.
8. **Verify by hand:**
   - the pulse appears once at the end of the test Doc, starting with the `[groww-pulse:…]` marker;
   - one draft is in the **server account's** Drafts folder, and nothing is in Sent.

   Record the `delivery.json` statuses here.
9. Run `publish --confirm` again and confirm both targets report `skipped_already_delivered`, with no new append and no new draft.
10. Only then point `GOOGLE_DOC_ID` and `PULSE_EMAIL_TO` at the production Doc and recipients. Those are new idempotency keys, so the pulse is delivered once more.

**Known limitations:**
1. **Real writes verified once.** Response parsing followed the server source (mcp-contract.md §4) and the fake. **Update 2026-10-17:** the real responses (`appended`, `draft_created`) parsed as `succeeded`.
2. **The gate trusts what it observes.** A server that rejects only header-less clients and wrong tokens passes. Which Google account the server uses still can't be verified from the client (S3 in the security assessment).
3. **Unknown outcomes need a human.** There is no read tool, so after `outcome_unknown` the operator must check the Doc and Drafts before `--force`. `--force` can create a duplicate if the write did happen.
4. **The ledger is local and unlocked.** Run one publish at a time. On ephemeral disks (Streamlit Cloud) idempotency is per-session. Deleting `data/runs/ledger.jsonl` loses idempotency history.
5. **The draft contains the full pulse, not a Doc link.** This keeps the Doc ID out of email; a link option could be added later.
6. **The Doc append is plain text.** Markdown formatting isn't applied in the Doc; `pulse.md` stays a preview artefact.
7. **The newest pulse is chosen by `created_at`.** Pass `--pulse` to deliver a specific run.
8. **Each live run makes 3 extra handshakes** (health check plus two auth probes) before writing. This is intentional, since `tools.listChanged` can change the contract between runs.

---

## Phase 6 — Streamlit UI and Orchestration

**Objective:** Give operators a safe, transparent interface to the full pipeline.

### Deliverables
1. `orchestrator.py`: staged execution with `run_id`, resume via input hashes, and per-stage status.
2. `cli.py`: the `groww-pulse run` end-to-end command, plus the per-stage commands from earlier phases.
3. `app/streamlit_app.py` with pages for Data, Preprocess, Themes, Pulse Preview, and Publish (architecture §6.7):
   - upload widget for CSV/JSON/JSONL and a window slider (8–12);
   - funnel visualization and samples of excluded reviews with reasons;
   - theme table with the score formula, and quotes linked to source review IDs;
   - validator checklist, with the word count shown live;
   - publish panel: MCP health status, dry-run preview, confirmation checkbox, separate "Append to Doc" and "Create Gmail draft" buttons, and real responses shown verbatim. **No send button.**
   - a warning when the ledger is ephemeral (Streamlit Cloud).
4. `.streamlit/config.toml` and secrets loading through `st.secrets` with an environment fallback.

### Testing requirements
- Orchestrator: full pipeline on the fixture corpus with mocked LLM and MCP. Resume skips completed stages. A failure in stage N leaves the outputs of stages 1..N−1 intact.
- Streamlit `AppTest` tests:
  - pages render without secrets in dry-run;
  - the publish buttons stay disabled until validators pass **and** confirmation is checked;
  - no send control exists.
- CLI tests with Typer's `CliRunner` check exit codes and messages.

### Acceptance criteria
- [ ] An operator can upload a real export, run all stages, preview the pulse, and publish from the UI.
- [ ] Every number, quote, and status in the UI traces to stored data or a real MCP response.
- [ ] The UI never displays secrets. Dry-run is the default.
- [ ] `groww-pulse run --dry-run` reproduces the UI result headlessly.

### Status — 2026-10-17: **dashboard and publish panel implemented and tested; orchestrator and pipeline-running pages not started; no live delivery from the UI yet**

| # | Deliverable | Status | Where |
|---|---|---|---|
| 1 | Orchestrator | Not started | — |
| 2 | `groww-pulse run` | Not started | Per-stage commands exist (`import`, `preprocess`, `analyze`, `report`, `check-mcp`, `publish`) |
| 3 | Streamlit dashboard | Done (read-only pages + publish panel) | `app/streamlit_app.py` (UI only) and `src/groww_pulse/dashboard.py` (read model) |
| 3 | Upload widget, window slider, run-stage buttons | Not started | The pipeline is still run from the CLI; the empty state lists the commands |
| 3 | Samples of kept and excluded reviews | Deliberately omitted | Only counts and reasons are shown, so no review text beyond the 3 verified quotes reaches the UI |
| 4 | `.streamlit/config.toml` | Done | No usage statistics, error details limited to the exception type, viewer toolbar, headless |
| 4 | `st.secrets` | Environment only | Streamlit Community Cloud exposes root-level secrets as environment variables, which `load_settings` already reads |

**What the dashboard shows** (all from stored artefacts; nothing is generated):
- **Pulse:** the newest `runs/*/pulse.json` that passes `load_publishable_pulse` (schema, `publishable`, hashes, `pulse.txt`/`pulse.md`, byte-for-byte re-render, every Phase 4 validator re-run). Newer files that fail are listed with the reason and never displayed as content. Synthetic pulses are refused; only tests opt in.
- **Statistics:** the funnel and analysis named by the pulse's `source` block (`preprocess_run_id`, `analysis_run_id`). Missing files are reported, not guessed.
- **Delivery status:** the latest ledger entry per target for this content hash, plus targets already delivered for the same ISO week with a **different** content hash. Such a pulse is shown as blocked (duplicate-week protection, enforced by the publisher, no UI override); if the delivered version reached only some targets and its pulse still verifies, a separate section previews and delivers *that* version. Tool responses are not copied into the view; results of an action in the session are shown masked.

**Delivery from the UI** (`dashboard.preview_delivery`, `dashboard.deliver`, `dashboard.check_mcp` are thin wrappers over the Phase 5 publisher and client):
1. "Preview delivery (dry run)": `publish(confirm=False)`. No network calls; writes `runs/{run_id}/delivery.json` like the CLI.
2. The confirmation checkbox and "Append to Google Doc and create Gmail draft" stay disabled until: a validated, non-synthetic pulse; a readable ledger; `DELIVERY_MODE=confirm`; `MCP_SERVER_URL`, `GOOGLE_DOC_ID`, `PULSE_EMAIL_TO`; `MCP_AUTH_TOKEN`; and a dry run of **this content hash** in the session. The reasons are listed on the page. The publisher re-checks everything and still runs the security gate.
3. The button calls `publish(confirm=True, force=False)` for exactly the displayed pulse (never "newest"), then resets the checkbox. There is no `--force` and no send control.
4. "Check MCP server (read-only)": handshake, `tools/list`, contract, auth probe. No tool call.

**Deviation from the plan:** one button delivers both targets (Doc first, then draft), instead of separate "Append to Doc" and "Create draft" buttons. The Phase 5 publisher plans both calls together with their ordering and ledger rules; splitting them would mean changing tested delivery code.

**Bug found and fixed while testing:** a session-state key shared a name with a button key (`preview`, `health`), which Streamlit rejects at the first click. Results now use separate keys.

**Test results (2026-10-17, Python 3.11, local):**
- `pytest`: **953 passed, 3 skipped** (opt-in live tests), up from 931. `dashboard.py` coverage 94%; the uncovered lines are the live wrappers (`deliver`, `check_mcp`) that the app tests replace with fakes.
- `tests/unit/test_dashboard.py` (16): empty data dir; synthetic refused by default; a verified pulse with funnel, analysis, and 3/3/3; a tampered newer pulse rejected while the older valid one is shown; unreadable `pulse.json`; missing funnel and analysis; ledger status for this pulse vs. other delivered versions; a corrupt ledger blocks delivery; blockers in dry-run mode, fully configured, and with no pulse; preview and deliver refuse a synthetic pulse with no ledger write; timestamp formatting.
- `tests/unit/test_streamlit_app.py` (10, `AppTest`): empty state without secrets; invalid configuration; synthetic pulse refused without the test opt-in; all tabs, validators, quotes with review IDs, themes, actions, funnel, and ranking formula render; quotes shown verbatim with the ellipsis outside the quotation marks; delivery disabled in dry-run mode; no send or force control; the preview → checkbox → button sequence (fakes for the publisher); a refused preview shows the reason; secret values never displayed.
- `ruff check`, `ruff format --check`, `mypy` (configured scope), `mypy --strict` on the two new test modules, and the pre-commit hooks (including detect-secrets) on the changed files are clean.
- Local launch bound to `127.0.0.1`: `/_stcore/health` returned `ok` and the page returned 200. An `AppTest` render against the real local data showed the W42 pulse (`84d6f68b`), 6/6 validators, 211 words, the funnel (73 → 67), and the warning that the earlier W42 version (`6cac28a6`) was already delivered to the Doc and as a draft. No button was pressed.

**Remaining:** the orchestrator and `groww-pulse run`; upload and run-stage pages; a first supervised delivery from the UI; a deployment-specific way to detect an ephemeral disk (the Delivery tab currently always shows the ledger caveat).

---

## Deployment

**Objective:** Run the app reliably in a hosted environment.

### Deliverables
1. `Dockerfile` (python:3.11-slim, non-root user, pinned dependencies) and `Procfile` / `railway.json` for Railway. A persistent volume is mounted at `DATA_DIR`.
2. `requirements.txt` for Streamlit Community Cloud, with an `.streamlit/secrets.toml.example` that holds placeholders only.
3. `docs/deployment.md`: environment variables per platform, volume setup, the weekly run (Railway cron service running `groww-pulse run --confirm`, or a manual UI run), rollback steps, and secret rotation.
4. Post-deploy smoke checks:
   - the app loads;
   - `check-mcp` passes through `tools/list`;
   - a dry-run on a sample export succeeds.
5. Health endpoint or page showing the app version, MCP health, and last successful run (no secrets).

### Testing requirements
- CI builds the Docker image and runs `pytest` inside it.
- Smoke script (`scripts/smoke.py`) runs against the deployed URL after each deploy. It is dry-run only.
- One manual confirmed publish against the test Doc and draft after the first deployment, with the result recorded in the deployment log.

### Status — 2026-10-19
The dashboard is deployed on Streamlit Community Cloud at <https://groww-review-pulse-agent-2nza7oxgal2ywzeqg36n9m.streamlit.app/> from `feature/streamlit-dashboard-week-lock`, showing the committed read-only snapshot. Delivery from it is disabled by design, because the cloud has no ledger. Deliverables 1, 3, 4 (smoke script), and 5, the Docker CI build, and hosting this agent on Railway are **not built**. The only Railway service is the MCP server. Deliverable 2 exists as `requirements.txt`, exported from `uv.lock`; there is no `.streamlit/secrets.toml.example`, and the secrets are documented in the README instead. The weekly run is manual.

### Acceptance criteria
- [ ] The deployed app is reachable, and the smoke checks pass, including the MCP handshake (not just the root URL).
- [ ] Secrets live only in the platform secret store, and nothing sensitive is in the image or repo.
- [ ] On Railway, the ledger persists across restarts: re-publishing after a redeploy is skipped.
- [ ] A weekly run produces a Doc append and a Gmail draft, and **no email is sent**.

---

## Dependencies

| Area | Package (indicative) | Purpose |
|---|---|---|
| Core | `pydantic`, `pydantic-settings`, `python-dotenv` | Models, config |
| Data | `pandas` | Import and tabular processing |
| Text | `emoji`, `lingua-language-detector`, `rapidfuzz`, `scikit-learn` | Emoji policy, language, dedupe, TF-IDF/KMeans fallback and quote ranking |
| LLM | `anthropic` (default), `groq` (optional, free tier), provider abstraction | Theme, assignment, action generation |
| MCP | `mcp` (official Python SDK), `httpx` | Streamable HTTP client |
| Collector | None. Automated collection was rejected in Phase 0 (data-source decision) | — |
| UI | `streamlit` | Frontend |
| CLI | `typer` | Headless runs |
| Dev | `pytest`, `pytest-cov`, `hypothesis`, `ruff`, `mypy`, `pre-commit`, `detect-secrets` | Quality |

External dependencies: the deployed MCP server (separately owned), the LLM provider API, and a permitted review data source.

---

## Risk Register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | No permitted source for 8–12 weeks of reviews (scraping is prohibited by robots.txt and Google ToS) | Med | High | Import-only design; Route A (Groww Play Console export) or Route B (licensed vendor); collector is a disabled guard |
| R2 | MCP endpoint path or auth differ from assumptions; server is down or redeployed | Med | High | Phase 0 contract spike; `check-mcp` before every publish; configurable URL and token; clear `MCPUnavailableError` |
| R3 | Accidental email send via `gmail_send_email` | Low | Critical | Code allowlist, a test proving it is blocked, no UI control, LLM has no tool access |
| R4 | Duplicate Doc appends (no read-back tool; retries) | Med | Med | Ledger keyed by week and hash; no retries on tool errors; separate keys per target; audit marker line |
| R5 | Ephemeral disk (Streamlit Cloud) loses the ledger | High (on SC) | Med | As built: the hosted dashboard has no ledger and delivery from it is disabled; delivery runs only from the operator's machine. A Railway volume was planned for production but not built |
| R6 | LLM hallucinated quotes or numbers | Med | High | Quotes extracted by code; numbers computed by code; validators fail closed |
| R7 | PII leakage (Indian identifiers, names in free text) | Med | High | Redaction before the LLM; output scan; test corpus; raw store retention limit |
| R8 | Hinglish and code-mixed reviews skew the English sample | High | Med | Confidence threshold; funnel shows the share excluded; policy revisit noted |
| R9 | The strict emoji policy removes many reviews, leaving too few for 3 quotes | Med | Med | Funnel visibility; `lenient` mode keeps reviews for theming but never as quote sources; fail closed if < 3 quotes |
| R10 | The 250-word limit conflicts with 3 themes, 3 quotes, and 3 actions | Med | Low | Quote length cap of 35 words; compact template; constrained re-prompt; validator |
| R11 | Prompt injection inside review text | Low | Med | Delimited data blocks; schema validation; no LLM tool access |
| R12 | LLM cost or rate limits | Low | Med | Batching, caching, token budget, backoff |
| R13 | The Docs scope can edit any accessible Doc, so the wrong document could be targeted | Low | High | Single configured `GOOGLE_DOC_ID`; format validation; test Doc for live tests |
| R14 | **The deployed MCP server is unauthenticated, and its OAuth routes lack a `state` check.** Third parties can send mail, append to Docs, or swap the server's Google account (observed 2026-10-14) | High | Critical | Server-side fix proposed in [mcp-server-security.md](mcp-server-security.md); no production delivery until it is fixed or a hardened fork is deployed; `check_mcp.py` reports `auth_enforced`; client allowlist. **Update 2026-10-17:** delivery uses the bearer-auth Railway deployment `…-b6ec`, which passed the security gate. Residual: the server's Google account and OAuth-route settings are not verified from the client |
| R15 | The 3–7 day Play Console posting lag leaves the newest days of the window missing | High | Low | Record the actual window end (latest date in the data); optional owner-only API top-up for the last 7 days |

---

## Implementation Order (critical path)

1. **Phase 0**: skeleton, CI, config. **Run the MCP contract spike and get the data-source decision first.** Both gate everything else.
2. **Phase 1**: importers and store, so real data is available early.
3. **Phase 2**: preprocessing and PII, so privacy guarantees exist before any LLM call.
4. **Phase 5 (client part)**: in parallel with Phases 3–4: MCP client, allowlist, ledger, and fake-server tests.
5. **Phase 3**: analysis with mocked LLM tests, then a live smoke test.
6. **Phase 4**: report and validators. Then finish the Phase 5 publisher on top of the validated pulse.
7. **Phase 6**: orchestrator, CLI, Streamlit UI.
8. **Deployment**: Railway with a volume (production) and Streamlit Cloud (demo); smoke checks; first supervised weekly run.
