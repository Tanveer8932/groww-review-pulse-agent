# Data-Source Decision Record — Groww Google Play Reviews

| Field | Value |
|---|---|
| Status | **Proposed — NOT approved.** A manually collected dataset (§7) is in use for pipeline validation only; it has no confirmed reuse licence and no legal approval (facts re-verified against official Google sources on 2026-10-14) |
| Date | 2026-10-14 |
| Decides | Open question A6 ([problemStatement.md §10](problemStatement.md)) |
| Scope | Google Play only (`com.nextbillion.groww`). Apple App Store is out of scope |
| Approver | _Operator / project owner: name, date_ (not yet approved) |

## 1. Requirement

The source must provide **real, public Google Play reviews of Groww for the latest 8–12 weeks**. Each review needs its text, star rating, and date; a review ID is also wanted. Use must be permitted by the source's terms, with no authentication bypass and no circumvention.

## 2. Evidence gathered (re-verified 2026-10-14)

| Check | Finding | Source |
|---|---|---|
| Play robots.txt (`User-Agent: *`) | `Disallow: /_` (covers `/_/PlayStoreUi/data/batchexecute`, the internal endpoint that loads reviews) and `Disallow: /store/getreviews` | `https://play.google.com/robots.txt`, fetched 2026-10-14 |
| Google Terms of Service | Prohibits "using automated means to access content from any of our services in violation of the machine-readable instructions on our web pages (for example, robots.txt files…)" | `https://policies.google.com/terms` |
| Open-source "Play scraper" libraries | Fetch reviews via `/_/PlayStoreUi/data/batchexecute`, a disallowed path | Library behaviour |
| Google Play Developer API (Reply to Reviews) | "You can retrieve only the reviews that users have created or modified within the last week." It needs the developer account's OAuth client or a service account with the *Reply to reviews* permission. GET quota is 200/hour. The page itself points to the Play Console CSV for older reviews | `developers.google.com/android-publisher/reply-to-reviews` |
| Play Console review reports | Monthly CSVs at `gs://[developer_bucket_id]/reviews/reviews_[package_name]_YYYYMM.csv` (bucket `pubsite_prod_rev_…`). "Data is captured daily and posted within 3 to 7 days in monthly CSV files." Files are UTF-16. Access needs the *View app information* permission set to **Global**. Google advises not relying on the column count | Play Console Help, answer 6135870 |

**Conclusion:** no compliant **automated public** route exists. The compliant routes are the developer account's own exports (A) and licensed third-party data (B).

## 3. Options evaluated

| # | Option | Permitted? | Covers 8–12 weeks? | Notes |
|---|---|---|---|---|
| A | **Play Console review export from Groww**, shared by a Groww stakeholder with access | Yes, if Groww shares it for this purpose | Yes (monthly files) | Best fidelity and an official source. Needs a Groww sponsor. Files are typically **UTF-16 CSV** (see §5) |
| B | **Licensed review-intelligence vendor export** (an app-review analytics provider whose licence permits internal analysis and export) | Yes, within the vendor licence | Yes | Paid. The licence must allow export and storage of review text and quotation in internal reports |
| C | Public dataset (for example on Kaggle or Hugging Face) | Depends on its licence | **Usually no.** Datasets are typically stale | Acceptable only for development, never for the weekly pulse |
| D | Manual export: an operator reads the public Play listing in a normal browser and records reviews | **Unconfirmed.** Not automated by this project, but no licence or legal review covers reuse of the text | Partially | Slow and error-prone; sample is whatever the browser showed. Fallback only. **The dataset in use (§7) is a variant of this option** |
| E | **Automated public collector** (scraper library or custom fetcher) | **Not permitted.** It uses robots-disallowed paths (`/_`, `/store/getreviews`) | — | **Rejected.** See §4 |

## 4. Decision (proposed)

1. **Primary source: Option A.** If no Groww sponsor is available, use **Option B**. Either way, data enters the system **only through the CSV/JSON/JSONL importers** (Phase 1).
2. **The optional public collector (Option E) is not approved.** `ENABLE_PLAY_COLLECTOR` stays `false`. Recommendation for Phase 1: implement `collection/play_public.py` only as a guard that raises `CollectorDisabledError`, with no fetching code, unless a permitted automated source is identified later and this record is amended.
3. Option C may be used **only for local development**, and never for a published pulse. Synthetic fixtures are used for automated tests.
4. Option D is an emergency fallback. It was planned to carry `manual_export` provenance; that source type is **not implemented**, so such imports are currently tagged `google_play_import` (see §7).

## 5. Import formats to support (Phase 1 input)

The importers accept CSV, JSON (array), and JSONL with a configurable column map. Expected inputs:

**Play Console reviews CSV.** Headers as commonly seen; confirm against a real file before relying on them:

| Column | Maps to |
|---|---|
| `Package Name` | `app_id`, which must equal `com.nextbillion.groww` |
| `App Version Name` | `app_version` |
| `Reviewer Language` | Hint only; language is re-detected |
| `Review Submit Date and Time` / `Review Submit Millis Since Epoch` | `created_at` (UTC) |
| `Star Rating` | `rating` |
| `Review Title`, `Review Text` | Review text |
| `Review Link` | Format per Google's docs: `https://play.google.com/apps/publish/?dev_acc=<…>#ReviewPlace:id=<…>`. The review ID is in the **URL fragment** (`ReviewPlace:id=`), not a query parameter. If it's absent, fall back to the hash rule in architecture §5.1 |
| `Developer Reply …` | **Dropped** |

Encoding: Play Console exports are **UTF-16** (Google's docs say to convert them to UTF-8), usually with a BOM. The importer must detect UTF-8, UTF-8-BOM, and UTF-16, and must map columns **by header name**, never by position.

**Feasibility (2026-10-14).** A throwaway in-memory check with synthetic strings (nothing persisted) confirmed that `pandas` reads the documented header set from UTF-16-with-BOM bytes. It handled quoted commas, converted `Review Submit Millis Since Epoch` to UTC, and extracted `ReviewPlace:id=` from `Review Link`. JSON arrays and JSONL parse with the standard library. This is evidence of feasibility, not a Phase 1 test.

## 5a. How to obtain the latest 12 weeks of real reviews

**Route A — Groww Play Console export (recommended):**
1. A Groww Play Console admin grants the operator, or a service account, **View app information = Global** for `com.nextbillion.groww`.
2. Download the monthly files that cover the window. Twelve weeks spans **4 calendar months**; on 2026-10-14 that's `202607`, `202608`, `202609`, and the partial `202610`:
   `gsutil cp gs://pubsite_prod_rev_<id>/reviews/reviews_com.nextbillion.groww_2026{07,08,09,10}.csv data/imports/`
   (or Play Console → Download reports → Reviews).
3. Because of the **3–7 day posting lag**, the newest days may be missing. Either set the window end to the latest date present in the files and record it, or (optionally, owner credentials only) top up the last 7 days via the Reply to Reviews API.
4. Repeat weekly; the importer upserts by `review_id`, so overlapping files are safe.

**Route B — licensed vendor export (if Groww cannot share):**
1. Choose an app-review analytics vendor whose **licence explicitly permits** export, internal analysis, and quoting short anonymized excerpts in internal documents.
2. Export `com.nextbillion.groww`, Google Play, the last 12 weeks, as CSV or JSON, including review ID, text, rating, and date.
3. Keep the licence reference in this record.

**Generic export** (vendor, JSON, or JSONL): default aliases are `reviewId`, `content`/`text`, `score`/`rating`, `at`/`date`, and `reviewCreatedVersion`. Reviewer name, avatar, and profile fields are discarded at ingest.

## 5b. Investigation of actually accessible sources (2026-10-14, no Play Console access assumed)

Target window: **2026-07-23 → 2026-10-14** (12 weeks). Every source was checked for dates, licence, export, collection method, and permission to quote.

### Free sources: none usable

| Source (checked) | Dates found | Licence | Collection method | Verdict |
|---|---|---|---|---|
| Hugging Face `Avaneeshkarthik/groww-app-reviews` (`groww_reviews_last5y.csv`, 46 MB, created 2025-09-28) | 2020-09-28 → 2025-09-26 (header and tail checked via HTTP range; not downloaded in full) | **None** declared; no dataset card | Columns `reviewId,content,score,at,userName,thumbsUpCount,appVersion` match `google-play-scraper` output and include reviewer names | **Rejected**: outside the window, unlicensed, scraped |
| GitHub `masiwalruchi-lgtm/groww-review-insights-analyser` `data/reviews.csv` | 2026-06-27 → 2026-09-18; 13,603 Play + 50 App Store rows | **None** (all rights reserved) | Undocumented; a student project for this same exercise | **Rejected**: unlicensed, unknown provenance, ends 26 days before the window end |
| GitHub `bhavyaamahajann/Weekly_Review_Insights_UsingGoogleMCP` `reviews_clean.csv` | No dates in the file; 646 rows | **None** | `requirements.txt` lists **`google-play-scraper`** | **Rejected**: prohibited collection method, unlicensed |
| GitHub `Manasa-ad/…`, `rxm-gupta/…` | 2026-07-06 → 2026-09-17 | **None** | Undocumented; mostly App Store rows (500 of 503; 22 of 49) | **Rejected**: unlicensed, out of scope, tiny |
| GitHub `harikesh1001/…` | — | **None** | File named `…_synthetic_demo.csv` | **Rejected**: synthetic |
| Kaggle (web search) | No Groww-specific dataset found; generic Play datasets are years old | Varies | Typically scraped | **Not usable** |
| Free "export" tools (browser extensions, free web exporters) | Live | Tool terms only | Automated collection from Play on the user's behalf | **Rejected**: the same as scraping under §4 |
| Public app page `play.google.com/store/apps/details?id=com.nextbillion.groww` (one GET; robots.txt **allows** this path) | Embeds a small "most relevant" sample (~20 review-shaped IDs; timestamps 2026-07-16 → 2026-10-13) | Google ToS; reviewers own their text | "See all reviews" loads through the **disallowed** `/_/` endpoint | **Not sufficient**: a biased handful, not 8–12 weeks. Not extracted |

Copies of the third-party files were downloaded **only to inspect dates and columns**, then deleted. Nothing was imported, and nothing is in `data/`.

### Paid sources: available, but need purchase or sign-up and a collection-method check

| Vendor | Covers apps you don't own? | Export | Price (vendor page or listings, 2026) | Data notes |
|---|---|---|---|---|
| **42matters** App Reviews API | Yes ("any Android app") | JSON API | Small plan or above; 14-day trial; price on request | Returns `author_hash` (no names), `rating`, `lang`, `content`, `date`, `app_version`. Default 30 days per request; a "Historical Package" covers older data. Docs say "all the rights belong to their respective owners"; licence terms are in the contract |
| **Appbot** | Yes ("Track any competitor app") | CSV/XLSX on **Large ($166+/mo annual) and Premium** only | 2-week trial, no card | History depth not stated |
| **AppFollow** | Yes (competitor monitoring) | CSV/Excel from Essential (~$129/mo annual), per listings | Free plan up to 2 apps | Pricing page not machine-readable; verify directly |
| **AppTweak** | Yes (reviews endpoint) | API and automated exports | Quote-based | Returns text, rating, country, device |
| **SerpApi / DataForSEO / Apify actors** | Yes | API | SerpApi 250 free searches/mo then $25+/mo; DataForSEO ~$5 per 1M reviews; Apify per-run | **Scraping as a service.** SerpApi says reviews are "parsed" from Play pages, and its own blog is titled "How to scrape Google Play Store app reviews". **Rejected under §4**: buying it does not change the collection method |

**Unresolved for every vendor:** none of them publicly documents *how* it collects reviews of apps it doesn't own. Before buying, get a **written representation** of the collection method, plus licence terms that permit:
- storing review text;
- internal analysis;
- quoting short anonymized excerpts in an internal Google Doc and email.

### Quoting verbatim snippets
Reviewers keep copyright in their text. Google's Terms license it to Google, not to third parties. A vendor licence can grant internal-use rights, but **whether anonymized verbatim snippets may be quoted needs the vendor's licence text and your legal sign-off**. This record does not settle it.

### Developer-only (excluded: no Play Console access)
- The Play Console monthly CSVs (Route A).
- The Reply to Reviews API, which also only covers the last 7 days.

### Conclusion (2026-10-14)
**No compliant, accessible dataset of real Groww Play reviews for the target window exists without operator action.** The blocker is commercial and legal, not technical:
1. Choose a licensed vendor (best technical fit: 42matters, or Appbot Large).
2. Obtain the vendor's written statement on collection method and licence.
3. Buy or start a trial under your own account.
4. Export **Google Play only**, `com.nextbillion.groww`, 2026-07-23 → 2026-10-14.

Phase 1 can then import it.

**Compatibility notes (unverified until a real file exists):**
- 42matters fields map through the generic aliases: `content`, `rating`, `date`, `app_version`, `lang`. It provides no review ID, so IDs will be derived.
- If an API response wraps reviews in an object (for example `{"reviews": [...]}`), the JSON importer will reject the file. Save the array as JSON or JSONL first.
- Appbot and AppFollow CSV headers are unknown. Use `--map` if auto-mapping misses a column.

## 6. Action required to close this decision

- [ ] The operator chooses a route. **Route A is unavailable** (no Play Console access, 2026-10-14), so Route B applies: a licensed vendor, with a written collection-method representation. Record the approver and date above.
- [ ] The operator places a real export for the latest 8–12 weeks in `data/imports/` (gitignored).
- [ ] The operator confirms that the licence or terms allow internal analysis and quoting of anonymized snippets in an internal Google Doc and email draft.
- [ ] **For the dataset in use (§7):** a decision, with evidence, on whether manual browser collection plus offline parsing is an acceptable source, and whether its text may be stored and quoted. Until then it is validation data only.

## 7. Dataset in use (2026-10-14): manually collected, not approved

This section records what was actually imported. It does **not** approve the source.

| Field | Value |
|---|---|
| Source | The **public** Groww listing on Google Play |
| Package | `com.nextbillion.groww` |
| Collection method | Collected **manually by the operator** through the public browser interface. The operator saved the browser's Network responses for the review pages and parsed them **offline** into a CSV. No collector, scraper, or script in this project fetched anything |
| File | `data/imports/groww_reviews_verified_combined.csv` (gitignored), 73 rows, SHA-256 `f1d5c57db5be16aa7ea92a4b1304780b1e0a2126a512ed0f71bbd94afea88f3b` |
| Columns | `reviewId, content, score, at` (UTF-8 with BOM) |
| Supplied by | The operator, 2026-10-14. Described as deduplicated; "combined" from more than one saved response |
| Imported | 2026-10-14, `groww-pulse import … --as-of 2026-10-14`: 73 accepted, 0 quarantined |

**What this record does not claim:**
- **No Google Play Console authorization.** The file is not a Play Console export and did not come from Groww or the developer account.
- **No confirmed reuse licence.** Reviewers keep copyright in their text; Google's Terms licence it to Google, not to third parties (§5b, "Quoting verbatim snippets").
- **No legal approval.** Whether this collection method and the storage and quotation of the text are permitted has not been reviewed. Note that the review pages load from the `/_/` path that robots.txt disallows for automated agents (§2). Collection here was manual, but this record does not settle whether that distinction is sufficient.
- **No independent verification** of the parsing step. The project received the CSV, not the saved Network responses, so it cannot check that the parse was complete or exact. Four texts look truncated or malformed and two more look spliced or cut at the start (see the implementation plan, Phase 2 "Quote holds").

**Provenance as stored.** Each raw record carries `source_file`, `ingested_at`, and the import `run_id`. Its `source` field is `google_play_import`, the only import type the code has; it means "a file of Google Play reviews imported by hand", not "a Play Console export". The planned `manual_export` source type (§4) is not implemented, so the collection method is recorded **here**, not in the records.

### 7a. Coverage

| | |
|---|---|
| Target window | 2026-07-23 → 2026-10-14 (12 weeks, `--as-of 2026-10-14`) |
| Actual dates | **2026-07-24 → 2026-09-19** (58 days), on 36 distinct days |
| Missing | **2026-09-20 → 2026-10-14** (25 days, about 3.5 weeks): no reviews at all. The last 3 window weeks are empty, and the most recent week covered (Sep 16–22) has data only up to Sep 19 |
| Reviews per window week | 3, 7, 4, 2, 9, 22, 7, 11, 8, 0, 0, 0 |
| By month | July 3, August 43, September 27 |

A pulse built from this file describes **late July to mid-September**, not "the latest 12 weeks". Any report must state the actual date range.

### 7b. Sample-selection limitations

- **Not a complete or random sample.** 73 reviews over 58 days is a small subset of the reviews posted in that period. What the browser loaded depended on the page's sort order and how far the operator scrolled; that order (for example "Most relevant" or "Newest") was not recorded.
- **Rating skew.** 1★ 40 (55%), 2★ 5, 3★ 8, 4★ 7, 5★ 13. The share of low ratings may reflect the selection, not the user base. Theme shares and rankings describe **this sample only** and must not be presented as Groww-wide proportions.
- **Uneven over time.** 22 of 73 reviews fall in one week (Aug 26 – Sep 1), and several weeks have 2–4.
- **Edits.** If a reviewer edited a review, the saved response shows only the version visible when it was saved, with its date.
- **Length.** 11 texts are at or near Google Play's 500-character limit; some of them end mid-word, so the parse or the source may have cut them.

### 7c. Fields not available

- **App version:** absent for all 73 reviews, so issues can't be tied to a release.
- **Time of day:** dates only (`YYYY-MM-DD`); stored as UTC midnight.
- **Thumbs-up count, review title, reviewer language, developer reply:** absent. (Reviewer identity fields are discarded at ingest anyway.)

### 7d. Permitted use until approval

Pipeline validation and internal development only. **Do not publish, send, or share** any output built from this dataset until §6 is closed for it. Phase 5 delivery remains blocked separately by the MCP security gate.
