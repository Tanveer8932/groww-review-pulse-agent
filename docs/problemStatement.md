# Problem Statement — Groww Review Pulse Agent

| Field | Value |
|---|---|
| Document status | Draft v1 |
| Last updated | 2026-10-14 |
| Owner | Groww Review Pulse Agent team |
| Related docs | [architecture.md](architecture.md), [implementation-plan.md](implementation-plan.md) |

---

## 1. Background

Groww (`com.nextbillion.groww`) is a high-volume Indian investing app on the Google Play Store. Thousands of public reviews arrive every month. They describe login failures, KYC delays, order execution problems, UI regressions, customer support gaps, and more. Product and support teams have no lightweight, repeatable way to turn that stream into a short, trustworthy weekly signal.

Doing this by hand is slow, inconsistent, and prone to cherry-picking. Asking an LLM to "summarize reviews" without controls creates a different problem: invented quotes, made-up statistics, and claims of delivery that never happened. This project builds an agent that is useful **and** auditable.

## 2. Problem

> Product stakeholders need a concise, evidence-backed weekly summary of what Groww's Google Play reviewers are saying, delivered into the tools they already use (Google Docs and Gmail). Every quote and number in it must be traceable to real public reviews.

## 3. Goals

1. **Collect** public Google Play reviews of Groww from the most recent 8–12 weeks, using only permitted data sources.
2. **Import** reviews from CSV, JSON, and JSONL exports, so the pipeline works without any live collection.
3. **Clean** the data: keep English reviews with more than 8 words, apply the emoji policy, deduplicate, and redact PII.
4. **Analyze**: group reviews into at most 5 meaningful themes and rank them.
5. **Report**: produce a weekly pulse of at most 250 words with:
   - the top 3 themes,
   - exactly 3 genuine, verbatim, anonymized review snippets,
   - exactly 3 concrete action ideas.
6. **Deliver** through the existing Google Workspace MCP server:
   - append the pulse to an existing Google Doc,
   - create a Gmail **draft**. Nothing is ever sent automatically.
7. **Operate** through a Streamlit UI that runs and previews the analysis, and deploy it to Streamlit Community Cloud or Railway.

## 4. Non-Goals (Out of Scope)

| Item | Reason |
|---|---|
| **Apple App Store reviews** | Explicitly excluded from the current scope. No App Store collector, importer mapping, or UI option is built. The design leaves room for another source later, but no App Store work is planned or tested. |
| Sending emails | The agent only creates Gmail drafts. A human reviews and sends them. |
| Replying to reviews | Needs developer-account access to the Play Console, which we do not have. |
| Non-English analysis or translation | Non-English reviews are excluded, not translated. |
| Real-time or streaming monitoring | The cadence is a weekly batch. |
| Sentiment dashboards and long-term BI | Only the pulse and a run-level preview are in scope. |
| Using Google REST APIs directly for Docs or Gmail | All Docs and Gmail actions go through the MCP server. |
| Bypassing authentication, CAPTCHAs, rate limits, or robots rules | Prohibited by policy (see §7). |

## 5. Users and Stakeholders

| Persona | Need |
|---|---|
| Product manager (primary) | A weekly pulse in a shared Google Doc and a ready-to-send email draft |
| Pipeline operator | Run imports, preview the output, and publish deliberately, with clear errors |
| Engineering / support leads | Themes backed by real quotes they can trust and act on |
| Compliance / privacy reviewer | Proof that no PII leaks and that data sources are permitted |

## 6. Functional Requirements

### 6.1 Data acquisition
- **FR-1** Collect publicly accessible Groww Play Store reviews published in a configurable window of 8–12 weeks (default: 12) ending on the run date.
- **FR-2** Import reviews from CSV, JSON (array of objects), and JSONL files. Column mapping is configurable.
- **FR-3** Normalize every record into one canonical review schema and persist it to `reviews.jsonl`.
- **FR-4** Record provenance on every review: source type, source file or collector, and collection timestamp.

### 6.2 Preprocessing
- **FR-5** Keep only reviews with **more than 8 words** (≥ 9 whitespace-delimited word tokens after normalization).
- **FR-6** Exclude reviews not detected as English, using a confidence threshold.
- **FR-7** Emoji policy (default **strict**): exclude any review that contains one or more emoji, including emoji-only reviews. A configurable `lenient` mode exists, but it never alters text and so never weakens quote verbatimness. See [architecture.md §6.3](architecture.md).
- **FR-8** Deduplicate by source review ID, by exact normalized text, and by near-duplicate similarity.
- **FR-9** Redact PII before any text reaches the LLM, the UI, reports, or MCP: emails, phone numbers, PAN, Aadhaar, UPI IDs, bank account or card numbers, and URLs. Reviewer names, avatars, and profile links are never stored.
- **FR-10** Keep each review's original text in a restricted local raw store, used for audit and quote verification. Only redacted text leaves the preprocessing layer.

### 6.3 Analysis
- **FR-11** Cluster the cleaned reviews into **at most 5** meaningful, mutually distinct themes. Each theme has a name, a description, and member review IDs.
- **FR-12** Rank themes with a transparent, documented score and select the **top 3**.
- **FR-13** Select **exactly 3** snippets, ideally one per top theme. Each must be a verbatim, contiguous substring of a real review's redacted text. Code verifies this; the LLM is not trusted to do it.
- **FR-14** Generate **exactly 3** concrete action ideas, each tied to a top theme.

### 6.4 Reporting
- **FR-15** Render the weekly pulse in at most **250 words**, counted on the final rendered text.
- **FR-16** Include the ISO week label, analysis window, and review counts. All numbers are computed by code, never by the LLM.

### 6.5 Delivery (MCP)
- **FR-17** Append the pulse to an existing Google Doc. The document ID comes from configuration.
- **FR-18** Create a Gmail draft with the pulse. Recipients come from configuration.
- **FR-19** Never call a send-email tool. A code-level tool allowlist blocks it.
- **FR-20** Make delivery idempotent: a pulse for a given ISO week and content hash is appended or drafted at most once, unless the operator forces a re-run.
- **FR-21** Report MCP outcomes exactly as the server returns them. Never claim success without a successful tool response.

### 6.6 Interface and operations
- **FR-22** Provide a Streamlit UI to import or collect data, run each pipeline stage, preview themes, quotes, and the pulse, and publish with explicit confirmation. Dry-run is the default.
- **FR-23** Provide a CLI that runs the same pipeline for scheduled or headless use.

## 7. Data-Source and Compliance Constraints

1. **Permitted sources only.** Use only sources whose terms allow this use:
   - exports the operator is entitled to use (for example, a licensed third-party review dataset or exports Groww shares with us), imported as CSV/JSON/JSONL;
   - **No automated collection.** Phase 0 found that Google Play's robots.txt disallows the review endpoints, and Google's ToS forbids automated access contrary to it, so automated collection is rejected ([data-source-decision.md](data-source-decision.md)).
2. **No circumvention.** No login, no session or cookie reuse, no CAPTCHA solving, no proxy rotation to evade limits, and no access to non-public data.
3. **Minimal data.** Do not store reviewer names, avatars, or profile URLs.
4. **No fabrication.** Every review, quote, count, and MCP result must come from real data or real tool responses. Synthetic data is allowed only in automated tests, must be clearly labelled as fixtures, and must never appear in a published pulse.
5. **Secrets.** Credentials, OAuth tokens, API keys, document IDs, and recipient addresses come from environment variables or platform secret stores. They are never committed. `.env.example` lists every variable with placeholder values.

## 8. Quality Attributes (Non-Functional Requirements)

| ID | Attribute | Requirement |
|---|---|---|
| NFR-1 | Trustworthiness | 100% of published quotes pass automated verbatim verification against stored reviews |
| NFR-2 | Privacy | 0 PII matches in pulse output across the PII test corpus |
| NFR-3 | Idempotency | Re-running the same week does not create duplicate Doc appends or drafts |
| NFR-4 | Reproducibility | Same input, same config, and the LLM cache produce the same pulse. LLM temperature is 0 |
| NFR-5 | Observability | Structured logs with a run ID and a per-stage count funnel; MCP request and response metadata are logged with secrets redacted |
| NFR-6 | Robustness | Typed errors per layer; partial failures leave the system consistent and are surfaced in the UI |
| NFR-7 | Testability | Pytest suite with ≥ 85% line coverage on core packages. LLM and MCP are mocked in unit tests |
| NFR-8 | Performance | End-to-end run on 5,000 raw reviews finishes in under 5 minutes, excluding LLM latency spikes |
| NFR-9 | Portability | Python 3.11+, deployable to Streamlit Community Cloud or Railway without code changes |
| NFR-10 | Cost control | LLM token budget per run is configurable and enforced |

## 9. Success Metrics and Definition of Done

The project is done when:

- [ ] One end-to-end run on real, permitted Groww review data (8–12 week window) produces a pulse that passes every validator: ≤ 5 themes, top 3 shown, exactly 3 verified quotes, exactly 3 actions, ≤ 250 words, no PII.
- [ ] A Google Doc append and a Gmail draft are confirmed by successful MCP tool responses, and the operator manually checks the Doc and the Drafts folder.
- [ ] No email was sent. The send tool is blocked, and a test proves it.
- [ ] Re-running the same week causes no duplicate delivery.
- [ ] The Streamlit app is deployed and reachable, and its health check confirms MCP tool discovery, not just a reachable root URL.
- [ ] All tests pass in CI, and the documentation matches what was built.

## 10. Assumptions and Open Questions

| # | Item | Status |
|---|---|---|
| A1 | The existing MCP server (`saksham20189575/mcp-server-4`) exposes `google_docs_append_content` and `gmail_create_draft`, and also `gmail_send_email`, which must be blocked | **Verified 2026-10-14** via `tools/list`; see [mcp-contract.md](mcp-contract.md) |
| A2 | The deployed server at `https://mcp-server-4-production-0fca.up.railway.app/` speaks MCP over Streamable HTTP | **Verified**: the endpoint is the root path `POST /`, protocol `2025-11-25` |
| A3 | The deployed server holds Google OAuth tokens on its side, and its authenticated Google account can edit the target Doc | **Partly open**: server-side tokens confirmed from source. Which account, token validity, and Doc access need a test write |
| A4 | Whether and how clients must authenticate to the deployed MCP server | **Verified: no client auth** (`auth_enforced: false`), and the OAuth routes lack a `state` check. A fix is proposed in [mcp-server-security.md](mcp-server-security.md); production delivery is gated on it. The client still supports an optional bearer token |
| A5 | The MCP server offers no tool to read a Doc or list drafts, so idempotency must use a local or persistent run ledger | **Verified** via `tools/list` |
| A6 | A permitted source of Groww reviews for 8–12 weeks is available, either an export or an approved collector | **Proposed**: export-only; automated collector rejected. Approval pending; see [data-source-decision.md](data-source-decision.md) |
| A7 | Hinglish (romanized Hindi) reviews are common and are treated as non-English unless the detector is confident they are English | Policy decision; can be revisited |
