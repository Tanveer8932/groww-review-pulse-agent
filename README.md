# Groww Review Pulse Agent

Turns public Google Play reviews of Groww (`com.nextbillion.groww`) into a weekly
product-feedback pulse of at most 250 words. The pulse has the top 3 themes, exactly 3
verbatim anonymized quotes, and exactly 3 action ideas. It is appended to a Google Doc,
and a Gmail **draft** is created, both through an MCP server. Email is never sent.

Apple App Store reviews are out of scope.

- [Problem statement](docs/problemStatement.md)
- [Architecture](docs/architecture.md)
- [Implementation plan and status](docs/implementation-plan.md)
- [Observed MCP contract](docs/mcp-contract.md)
- [Data-source decision](docs/data-source-decision.md)
- [MCP server security assessment](docs/mcp-server-security.md)

## Setup (Windows / macOS / Linux)

Requires [uv](https://docs.astral.sh/uv/). It installs Python 3.11 if needed.

```bash
uv python install 3.11
uv venv --python 3.11 .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
uv pip install -e ".[dev]"      # or: pip install -e ".[dev]"
cp .env.example .env            # then fill in values; never commit .env
pre-commit install
```

## Common commands

| Task | Command |
|---|---|
| Tests | `pytest` |
| Lint and format | `ruff check . && ruff format --check .` |
| Type check | `mypy` |
| Secret scan | `detect-secrets-hook --baseline .secrets.baseline $(git ls-files)` |
| Run the dashboard (local only) | `streamlit run app/streamlit_app.py --server.address 127.0.0.1` |
| Import review exports | `groww-pulse import data/imports --as-of YYYY-MM-DD` |
| Preprocess (filter, dedupe, redact) | `groww-pulse preprocess --as-of YYYY-MM-DD` |
| Analyze (themes, quotes, actions; needs `GROQ_API_KEY`, or `ANTHROPIC_API_KEY` with `LLM_PROVIDER=anthropic`) | `groww-pulse analyze` |
| Render and verify the weekly pulse (no LLM, nothing published) | `groww-pulse report [--analysis data/runs/<run_id>/analysis.json]` |
| MCP health + security gate (read-only, no tool calls) | `groww-pulse check-mcp` |
| Preview delivery (dry run: no network calls) | `groww-pulse publish [--pulse data/runs/<run_id>]` |
| Deliver: Doc append + Gmail **draft** (needs `DELIVERY_MODE=confirm`) | `groww-pulse publish --confirm [--force]` |
| Verify MCP (Phase 0 script, read-only) | `python scripts/check_mcp.py` |
| Re-lock dependencies | `uv lock && uv export --no-dev --no-hashes --no-emit-project -o requirements.txt` |

`requirements.txt` is exported from `uv.lock` for Streamlit Community Cloud. Do not edit it by hand.

## Dashboard (Phase 6)

`streamlit run app/streamlit_app.py --server.address 127.0.0.1` opens a dashboard of the
newest weekly pulse that passes every publish check (hashes, files, re-render, all Phase 4
validators). Newer pulse files that fail are listed with the reason and never shown as
content. Tabs:

- **Weekly pulse**: ISO week, word count, the validator checklist, the rendered report, and
  the exact plain text that delivery would send;
- **Themes, quotes & actions**: the top 3 themes with the ranking formula, the 3 verified
  quotes with their review IDs, ratings, and dates, and the 3 action ideas;
- **Review statistics**: the preprocessing funnel with exclusion reasons, quote holds, PII
  redaction counts, and the rating mix of each top theme (counts only; no review text other
  than the verified quotes is shown);
- **Delivery**: ledger status per target for this pulse, a blocked status when a different
  version of the same week was already delivered (with a section to finish that version if
  it reached only some targets), a dry-run preview, the confirmed delivery button, and a
  read-only MCP health check;
- **Settings**: readiness and configuration, with secrets shown only as set / not set.

Delivery uses the same publisher as `groww-pulse publish`. The confirmed button stays
disabled until every validator passed, `DELIVERY_MODE=confirm`, the delivery targets and
`MCP_AUTH_TOKEN` are set, this exact pulse was previewed with a dry run in the session, and
the confirmation box is ticked. It appends to the Doc and creates a Gmail **draft**. There
is no send control, and `--force` is CLI-only.

With no validated pulse, the dashboard shows the commands that produce one.

## LLM provider (Phase 3)

This project runs analysis on **Groq Cloud's free tier** (`LLM_PROVIDER=groq`, model
`openai/gpt-oss-120b`, strict JSON-schema outputs). Anthropic stays available with
`LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`.

1. Create a key at <https://console.groq.com/keys> (free plan; no card needed).
2. `cp .env.example .env`, then set `GROQ_API_KEY=` in `.env` (gitignored; never commit it).
3. `groww-pulse analyze`

The free plan allows about 8,000 tokens per minute per model, counting each request's prompt
plus its maximum output. The client keeps every request under that limit: it caps output at
`GROQ_MAX_OUTPUT_TOKENS`, uses a smaller stratified discovery sample, and splits assignment
batches. It also waits between calls instead of failing, so a run on ~70 reviews takes a
few minutes. Validation, the ≤ 5 theme limit, the 3 verified quotes, the 3 actions, and the
provenance checks are the same for both providers. The configured model is the only one
called. There is no automatic switch to another model, a paid tier, or the other provider.

### Pipeline order

`import` → `preprocess` → `analyze` → `report`. Each command writes under `DATA_DIR`
(default `./data`, gitignored).
- `report` re-verifies the newest analysis (or the one passed with `--analysis`): integrity hash, statistics, quotes against the stored reviews and the raw store, actions, the 250-word limit, and a PII scan.
- It then writes `data/runs/<run_id>/pulse.md`, `pulse.txt`, and `pulse.json`.
- It refuses synthetic test data, a stale or edited analysis, and insufficient data, and writes nothing in those cases.
- `publish` delivers a validated, publishable pulse through the MCP server: it appends to `GOOGLE_DOC_ID` and creates a Gmail **draft** for `PULSE_EMAIL_TO`. Email is never sent.
- `publish` is a dry run unless both `--confirm` and `DELIVERY_MODE=confirm` are given.
- Live delivery also needs `MCP_AUTH_TOKEN`, and a server that rejects unauthenticated clients. The deployed server does not do this yet, so live delivery is currently blocked (see [docs/mcp-server-security.md](docs/mcp-server-security.md)).
- Re-runs never duplicate a delivery. The ledger is `data/runs/ledger.jsonl`.
- If a previous attempt's outcome is unknown, the target is blocked: check the Doc and the Drafts folder, then re-run with `--force` only if nothing was written.
- One version per ISO week: once any target received a version of a week, a pulse with different content for that week is `blocked_week_already_delivered`, in the CLI and the UI alike. `--force` does not override this. A partly delivered version can still finish its other targets: `groww-pulse publish --pulse data/runs/<its run> --confirm` (the block message prints the path), or the "Finish the delivered version" section in the Delivery tab.

After upgrading the PII rules or the language filter, re-run `preprocess` and `analyze`.
Older outputs fail the provenance check.

### Preprocessing settings

| Variable | Default | Meaning |
|---|---|---|
| `LANG_MIN_CONFIDENCE` | `0.80` | Detector confidence needed to keep a review as English |
| `LANG_LEXICAL_RESCUE` | `true` | Also keep English scoring 0.50–0.80 when at least 85% of its ordinary words are English and it has no Hinglish markers. `false` = threshold only |
