# Handoff: `linkedin funding-import` CLI (v1 / Path B)

**Status:** Spec locked. Ready for implementation.
**Estimated effort:** 3-4 hours (single developer or AI agent).
**Owner:** Cortivo LinkedIn outreach system.
**Related campaign:** `campaigns/recently-funded-non-tech.md` (already scaffolded).

---

## 1. Goal

Add a new CLI command that imports a recently-funded company's **non-technical founder** into the `recently-funded-non-tech` campaign as a prospect, with the funding details captured in `pitch_context` for the drafter.

**Why:** Existing campaigns target prospects by *rhetorical* signal ("posts that look like co-founder asks") — yielding mostly peers, sellers, and noise. The right signal is *behavioral*: a recent funding event means cash + urgency + a likely build-side gap. Crunchbase News announces these events daily; this CLI bridges the manual "skim Crunchbase News → import founders" workflow into the existing outreach pipeline.

**Scope:** Manual, one-at-a-time CLI. Auto-sourcing from RSS is **v2**, explicitly out of scope here.

---

## 2. Locked decisions

All confirmed via two rounds of clarifying questions with the operator.

| Decision | Value | Rationale |
|---|---|---|
| **Source** | Crunchbase News RSS (read by operator, not the system in v1) | Free, hourly updates. v1 keeps it manual for quality control. |
| **Stage filter** | Seed only ($1-5M typical) | Sweet spot — cash to pay an agency + urgency to ship before Series A. |
| **Geo filter** | None applied by the CLI | Operator already filters by skimming Crunchbase News; re-applying a geo regex would only mis-reject founders whose LinkedIn location is blank or unusual. |
| **Sector filter** | None | Operator skips non-fits manually. |
| **Confirmation flow** | Show match → auto-import | Operator skims output; runs again if it looks wrong. |
| **Match strictness** | Strict — score threshold 20 (requires company name in headline) | Zero wrong-person imports > volume. |
| **Cross-campaign dedup** | Skip with warning if prospect already in **another** campaign | Preserves existing campaign attribution + funnel state. |
| **Same-campaign re-import** | Force-overwrite `pitch_context` (status untouched) | Re-running with fresher Crunchbase details should refresh the drafter's input, not silently discard it. |
| **Missing campaign in DB** | Auto-sync the brief from `campaigns/<slug>.md` on the fly | One-command workflow — operator never has to remember `campaign sync` first. |
| **Action log on no-match** | Write `actions` row with `prospect_id=NULL`, `result=skipped_no_match` | Keeps an audit trail of which companies have been searched without success. No `prospects` row created. |
| **CLI flag shape** | Structured (`--company` required, others optional) | Cleaner pitch_context, less typing. |
| **Output verbosity (no match)** | One-line skip reason | Operator re-runs with different name if needed. |
| **Output verbosity (match found)** | Show name, headline, location, score, URL → import → confirm | Fast skim before commit. |
| **Status after import** | `targeted` | Daily cron handles react → connect → DM1 chain. |

---

## 3. Architecture

```
Operator skims Crunchbase News (manual)
        ↓
linkedin funding-import --company "Acme" --round seed --amount "$2M" \
                        --investors "Sequoia, a16z" --description "AI for legal docs"
        ↓
funding_lookup.find_founder()
  ├─ adapter.search(f'"{company}" founder', limit=10)
  ├─ Score each result on company-name match + role match - tech penalty
  ├─ Sort descending; return top non-rejected candidate (caller checks threshold)
  ↓
Threshold check (in CLI)
  └─ If match.score < 20 → log skipped_no_match, exit 1
        ↓
Cross-campaign dedup
  ├─ Already in another campaign → warn + skip (log skipped_dedup)
  ├─ Already in target campaign → force-overwrite pitch_context, log imported
  └─ Else proceed
        ↓
db.upsert_prospect(...)
  ├─ campaign_id = recently-funded-non-tech
  ├─ pitch_context = "Recently closed {round} {amount} from {investors}. 
  │                   Building {company} — {description}."
  ├─ status = "targeted"
  └─ provider_id from search hit
        ↓
db.log_action(pid, "funding-import", payload=structured)
        ↓
Existing pipeline takes over (react → connect_note draft → ...)
```

---

## 4. CLI specification

### Command

```bash
linkedin funding-import --company NAME [OPTIONS]
```

### Flags

| Flag | Required | Default | Notes |
|---|---|---|---|
| `--company` | **Yes** | — | Company name as it appears in Crunchbase News (e.g. "Acme AI"). |
| `--round` | No | — | Pre-seed / Seed / Series A. Free-form. Feeds pitch_context. |
| `--amount` | No | — | Round size (e.g. "$2M", "$3.5M"). Feeds pitch_context. |
| `--investors` | No | — | Comma-separated investor names (e.g. "Sequoia, a16z, Naval"). |
| `--description` | No | — | One-line product summary if known (helps the drafter pick a proof point). |
| `--campaign` | No | `recently-funded-non-tech` | Slug of target campaign. Default makes sense; overridable for edge cases. |
| `--dry-run` | No | False | Show match + would-be import, but don't write to DB. |

### Example invocations

```bash
# Minimal — just company name
linkedin funding-import --company "Acme AI"

# Typical (5 inputs from a Crunchbase article)
linkedin funding-import --company "Acme AI" --round seed --amount "$2.5M" \
                       --investors "Sequoia, a16z" \
                       --description "AI agent for SMB accounting"

# Preview match without committing
linkedin funding-import --company "Acme AI" --dry-run
```

### Output (match found)

```
Searching LinkedIn for founder of "Acme AI"...
✓ Match (score 28): Jane Smith
  Headline:  CEO @ Acme AI | building agentic accounting for SMBs
  Location:  San Francisco, California, United States
  URL:       https://www.linkedin.com/in/janesmith
  Signals:   company name in headline; CEO role; founder role

✓ Imported prospect #234 into campaign recently-funded-non-tech
  pitch_context: Recently closed seed $2.5M from Sequoia, a16z. Building 
                 Acme AI — AI agent for SMB accounting.
```

### Output (no confident match)

```
Searching LinkedIn for founder of "Better"...
✗ No confident founder match (top score 9, threshold 20). 
  Try a more specific name (e.g., "Better Mortgage") or verify the company on LinkedIn manually.
```

### Output (already exists in another campaign)

```
Searching LinkedIn for founder of "Acme AI"...
✓ Match (score 28): Jane Smith
⚠ Jane Smith is already prospect #87 in campaign non-tech-founder-mvp (status: connection_sent).
  Skipped — to move her, do it manually via SQL.
```

---

## 5. Implementation plan

### Files to create

| File | Purpose | Approx. LOC |
|---|---|---|
| `linkedin_agent/funding_lookup.py` | `find_founder()` + scoring + match dataclass | ~120 |
| `tests/test_funding_lookup.py` | Unit tests for scoring + lookup behavior | ~150 |
| `tests/test_funding_import_cli.py` | Integration tests for the CLI command | ~120 |

### Files to modify

| File | Change |
|---|---|
| `linkedin_agent/cli.py` | Add `@cli.command("funding-import")` block (~80 lines) |

### Scoring rules (locked)

In `_score_candidate(hit, company)`:

| Signal | Score change |
|---|---|
| No founder/CEO keyword in headline | **-100 (early-return; rejected)** |
| Company name (case-insensitive) in headline | **+20** |
| CEO or "Chief Executive" in headline | **+8** |
| Founder / Co-Founder / Cofounder in headline | **+5** |
| Any TECH_ROLE_KEYWORDS in headline (CTO, VP Eng, engineer, etc.) | **-15** (counts once) |
| Any NOT_FOUNDER_KEYWORDS (investor, recruiter, journalist, etc.) | **-30** (counts once) |

**`min_score = 20`** — the only way to clear this is company-name-in-headline AND no negative flags. This implements "strict — require company name in headline" cleanly.

### Constants (locked)

```python
FOUNDER_KEYWORDS = ("founder", "co-founder", "cofounder", "ceo", "chief executive")

TECH_ROLE_KEYWORDS = (
    "cto", "chief technology", "vp engineering", "vp eng", 
    "head of engineering", "head of tech", "software engineer", 
    "senior engineer", "lead engineer", "principal engineer", "staff engineer", 
    "tech lead", "ml engineer", "ai engineer", "data engineer", 
    "devops engineer", "full-stack developer", "fullstack developer",
    "backend developer", "frontend developer", "android developer", "ios developer",
)

NOT_FOUNDER_KEYWORDS = (
    "investor at", "venture partner", "general partner", "managing partner",
    "vc partner", "principal at", "associate at", "scout at",
    "recruiter", "talent acquisition", "headhunter",
    "journalist", "reporter", "writer at",
)
```

### pitch_context format

```python
def _format_pitch_context(company, round_type, amount, investors, description):
    """Build the pitch_context string for the drafter."""
    funding_phrase = "Recently funded"
    if round_type or amount:
        parts = [p for p in [round_type, amount] if p]
        funding_phrase = f"Recently closed {' '.join(parts)}"
    if investors:
        funding_phrase += f" from {investors}"
    base = f"{funding_phrase}. Building {company}"
    if description:
        base += f" — {description}"
    return base + "."
```

**Examples:**
- All fields: `"Recently closed seed $2.5M from Sequoia, a16z. Building Acme AI — AI agent for SMB accounting."`
- Just company + round: `"Recently closed seed. Building Acme AI."`
- Just company: `"Recently funded. Building Acme AI."`

### Action log payload

```python
db.log_action(
    pid, "funding-import",
    json.dumps({
        "company": company,
        "round": round_type,
        "amount": amount,
        "investors": investors,
        "description": description,
        "campaign": campaign_slug,
        "match_score": match.score,
        "match_signals": match.signals,
        "result": "imported" | "skipped_no_match" | "skipped_dedup",
    }),
    match.hit.linkedin_url if match else "",
    cfg.dry_run,
)
```

---

## 6. Test plan

### Unit tests (`test_funding_lookup.py`)

1. **`test_score_candidate_strong_match`** — Headline "CEO @ Acme AI" for company "Acme AI" → score ≥ 28
2. **`test_score_candidate_rejects_no_founder_role`** — Headline "Software Engineer @ Acme" → score = -100
3. **`test_score_candidate_penalizes_cto`** — Headline "CTO @ Acme AI" → score around 5 (company match + founder + CTO penalty = 20+5-15)
4. **`test_score_candidate_rejects_investor`** — Headline "Investor at Sequoia | Building Acme as side project" → heavily negative
5. **`test_score_candidate_no_company_in_headline`** — Headline "Founder of stealth startup" for company "Acme" → score = 5 (founder only, below threshold)
6. **`test_find_founder_returns_top_match`** — Stub adapter.search returns 3 candidates with varying scores; verify top is returned
7. **`test_find_founder_returns_None_below_threshold`** — All candidates score < 20; returns None
8. **`test_find_founder_handles_search_exception`** — Search raises HTTPError; returns None gracefully

### Integration tests (`test_funding_import_cli.py`)

9. **`test_cli_imports_clean_match`** — Stub adapter, run CLI, verify prospect inserted with correct pitch_context
10. **`test_cli_skips_on_no_match`** — Stub returns low-score results, CLI exits non-zero with clear message, no DB writes
11. **`test_cli_skips_on_cross_campaign_dedup`** — Pre-seed a prospect into another campaign with same linkedin_url; CLI warns + skips
12. **`test_cli_dry_run_no_writes`** — `--dry-run` flag → match shown but no DB row added, no action logged
13. **`test_cli_optional_fields_omitted`** — Just `--company`, no other flags; pitch_context still formatted correctly
14. **`test_cli_logs_action_with_payload`** — After successful import, verify `actions` table has `kind='funding-import'` with structured payload

### Coverage target

All tests run via `pytest` default suite (no live API calls). The test stubs `_invoke_claude` is unnecessary here — funding_lookup uses the adapter's `search()` which is already stubbable via `FakeAdapter`.

---

## 7. Edge cases and how we handle them

| Case | Handling |
|---|---|
| Common-word company name ("Better", "Notion") returning many false matches | High threshold (20) usually filters them out. If somehow a wrong match scores ≥20, the operator catches it in the CLI output before connect-note is sent (next cron). |
| Two co-founders (one tech, one non-tech) match the search | Tech-role penalty (-15) makes the non-tech one rank higher, which is what we want. |
| Company name with special characters (`Acme & Co`, `Acme.ai`) | Pass through as-is to Unipile search. Search engine handles. |
| Search returns 0 hits | Returns None → CLI shows "no candidates found for company". |
| Search returns hits but none clear the threshold | Returns None → CLI shows top score. Operator can retry with different spelling. |
| Prospect already in target campaign | Force-overwrite `pitch_context` with the new funding details; leave `status` alone (don't bump someone back to `targeted`). Log result=imported. |
| Prospect in OTHER active campaign | Skip with warning. Operator does manual move via SQL if intentional. |
| Search cap exhausted | `safety.check_cap("search")` raises → CLI exits with cap-hit error message. Same pattern as `search` command. |
| Founder has stale provider_id (matches the Rawi 422 incident) | Caught at the drafter / DM-send level, not here. Out of scope for funding_lookup. |

---

## 8. Phase B (v2) — full automation roadmap

v1 keeps the operator in the loop (skim Crunchbase News → CLI per company). v2 closes the loop by **auto-sourcing** funding events from the Crunchbase News RSS feed and looping over the existing v1 `find_founder` logic. **v2 is OUT of scope for the current build** but its shape is locked here so v1 is designed forward-compatible.

### v2 goal

Replace the manual skim step with a periodic, automated pull:

```
Cron (e.g. 0 6 * * *) or `linkedin funding-import-auto --days 1`
        ↓
crunchbase_news_rss.py — fetch RSS, parse <item>s
        ↓
funding_extractor.py — for each article:
  - Filter out summary articles ("10 biggest rounds this week", listicles)
  - Extract structured FundingEvent: {company, round, amount, investors, description, source_url}
  - Filter to seed stage only ($1-5M, "seed" / "pre-seed" in text)
        ↓
Dedup: skip if article URL already in seen_funding_articles table
        ↓
For each new FundingEvent:
  - Call v1's find_founder(company)  ← UNCHANGED v1 dependency
  - If match: upsert prospect with the v1 pitch_context format
  - If no match: log + skip
        ↓
Report: "Processed N articles → M funding events → K prospects imported"
```

### v1 → v2 contract (forward-compat constraints on v1)

So that v2 can layer on cleanly, v1 must respect three contracts:

1. **`find_founder(adapter, company)` is the public API.** v2 calls it in a loop. Do not couple it to CLI state (e.g. `click.echo`), printing, or `sys.exit`. Return values (FounderMatch or None) carry all the info. CLI-only concerns (output formatting, exit codes) stay in `cli.py`.
2. **`_format_pitch_context()` is the public formatter.** Both v1 CLI and v2 auto-importer use the same function. Same inputs → same output string. Don't inline this logic in the CLI handler.
3. **`upsert_prospect(...)` + action-log payload shape** stays stable. v2 writes the same `kind='funding-import'` action with the same payload keys. The only addition in v2 will be a new optional payload field `source_url` pointing to the Crunchbase article.

### New files in v2 (do not create in v1)

| File | Purpose |
|---|---|
| `linkedin_agent/funding_sources/__init__.py` | Package marker |
| `linkedin_agent/funding_sources/crunchbase_news.py` | RSS fetch + XML parse → `list[CrunchbaseArticle]` |
| `linkedin_agent/funding_sources/extractor.py` | Article → `FundingEvent`. Includes the "is this a funding announcement?" filter. |
| `linkedin_agent/funding_sources/dedup.py` | `seen_funding_articles` table accessor (article URL → seen_at) |
| `tests/test_crunchbase_news.py` | Mocked HTTP returning sample RSS XML |
| `tests/test_funding_extractor.py` | Sample article titles/content → expected FundingEvent (or None) |

### New DB table in v2

```sql
CREATE TABLE IF NOT EXISTS seen_funding_articles (
    article_url TEXT PRIMARY KEY,    -- Crunchbase News article permalink
    company     TEXT,                 -- extracted company name (for debugging)
    seen_at     TEXT NOT NULL,        -- ISO 8601 timestamp
    result      TEXT NOT NULL         -- 'imported' | 'skipped_no_match' | 'skipped_not_funding' | 'skipped_dedup_prospect' | 'skipped_wrong_stage'
);
CREATE INDEX IF NOT EXISTS idx_seen_funding_articles_seen_at ON seen_funding_articles(seen_at);
```

### New CLI command in v2

```bash
linkedin funding-import-auto [OPTIONS]
```

| Flag | Default | Notes |
|---|---|---|
| `--days` | 1 | Look back N days in the RSS feed |
| `--limit` | 50 | Max articles to process per run |
| `--dry-run` | False | Show what WOULD be imported, write nothing |
| `--campaign` | `recently-funded-non-tech` | Target campaign (same default as v1) |

Reports at the end: `Processed 18 articles → 9 funding events → 4 imported (3 skipped no-match, 2 skipped dedup)`.

### v2's biggest design challenge — funding-event extraction

Parsing free-text articles ("Acme AI raises $2.5M in seed round led by Sequoia, with participation from a16z and Naval Ravikant…") into structured fields is the hardest part of v2. Three options, in order of complexity:

| Approach | Pros | Cons | Recommended for v2 |
|---|---|---|---|
| **Pure regex** | Fast, deterministic, no LLM cost | Brittle to phrasing variation. False positives ("Acme acquires $2M company" matches "$2M raises" if you're sloppy with regex). | First — start here. |
| **LLM extraction (`claude -p`)** | Robust to phrasing. Handles edge cases. | 1 `claude -p` call per article. Cost: ~$0 via current auth; latency ~5-10s/article. Cron-context fragility (see incident 2026-05-21). | Fallback only — if regex fails to extract. |
| **Hybrid** | Best of both | More code | Best — regex first, LLM fallback. |

**Initial v2 build recommendation: pure regex only.** Write the regex against 20-30 sample articles. Measure false-positive rate. If >5%, add LLM fallback in v2.1.

### v2 phasing & effort

| Phase | What | Effort |
|---|---|---|
| 2a | `crunchbase_news.py` — RSS fetch + parse | ~2h |
| 2b | `extractor.py` — article filter + regex extraction | ~2-3h |
| 2c | `dedup.py` + new DB table + migration | ~1h |
| 2d | `funding-import-auto` CLI + orchestration | ~1-2h |
| 2e | Tests (`test_crunchbase_news.py`, `test_funding_extractor.py`, integration) | ~2h |
| 2f | Cron entry + Telegram summary on completion | ~1h |
| **Total** | | **~9-11h** |

### v2 acceptance criteria

When v2 ships:

1. ✅ `linkedin funding-import-auto --days 1` processes the day's RSS, reports counts
2. ✅ No duplicate imports across runs (dedup table works)
3. ✅ Listicle articles ("Top 10 rounds this week") are silently filtered out
4. ✅ Failed extractions are logged but don't crash the batch
5. ✅ `--dry-run` shows what would be imported, no DB writes
6. ✅ Telegram summary on completion (mirrors `linkedin daily`'s summary card)
7. ✅ All v1 tests still pass (regression check — v1's API surface unchanged)

### When to build v2

Build v2 **only after** v1 has been used for ~2 weeks AND:
- Operator confirms `find_founder` quality is consistently good (≥80% match accuracy on manual imports)
- Volume justifies automation (operator imports 5+/day consistently for 2 weeks)
- Operator wants the cognitive load off their morning routine

If after 2 weeks v1's match quality is bad (false positives, wrong founders), the right move is NOT to build v2 — it's to fix the matching scoring or move to Sales Nav before automating further.

### Future phases beyond v2 (v3+)

Listed here only so we don't accidentally scope them into v2:

- **Multi-source aggregation** (Failory weekly newsletter, TechCrunch funding articles, AngelList company updates, Strictly VC newsletter, VC firm Twitter feeds)
- **Sector + size filters** added to extractor (skip biotech, hardware, defense)
- **Cross-source dedup by company name** (same company announced in CB + TC + Failory → one import)
- **Active monitoring** of imported prospects' company growth signals (employee count delta, hiring announcements)
- **Crunchbase Pro integration** if free RSS becomes insufficient (currently $99/mo for richer filters via UI; $49/mo for limited API)

---

## 8b. Out of scope for v1 specifically (won't be done now)

These were considered for v1 and rejected:

- **CSV bulk import.** Operator chose CLI one-at-a-time for quality control.
- **Interactive y/N confirmation prompts.** `--dry-run` covers this.
- **Manual override flags** (`--force-prospect-id N`, `--accept-best-effort`). Could add post-v1 if pattern of needing them emerges.
- **Re-ranking on additional signals** (mutual connections, profile completeness). The score logic is sufficient for v1.

---

## 9. Acceptance criteria

The v1 build is done when **all** of these hold:

1. ✅ `linkedin funding-import --company "X"` runs without crashing on any string input
2. ✅ A successful match outputs name, headline, location, score, URL, signals — then imports the prospect with the structured pitch_context
3. ✅ A failed match (below threshold) outputs the top score + clear next-step suggestion, with no DB writes
4. ✅ A cross-campaign dedup outputs the existing campaign + status, with no DB changes
5. ✅ `--dry-run` works as advertised (preview only, no writes)
6. ✅ Action log has a `funding-import` row for every attempted import (whether imported, skipped, or dedup'd)
7. ✅ All 14 listed tests pass
8. ✅ Full existing test suite still passes (no regressions)
9. ✅ Committed and pushed to `main`

---

## 10. Open questions for v1 reviewer (none)

After two rounds of clarifying questions with the operator, all design decisions are locked. If any ambiguity surfaces during implementation, **stop and ask** — don't guess. The cost of one extra clarifying question is much lower than the cost of building the wrong thing.

---

## Appendix A: Sample workflow (operator perspective)

**Morning routine:**

1. Operator opens https://news.crunchbase.com/ on phone/laptop
2. Skims the "Funded" section for ~5 minutes
3. For each interesting funded company, runs:
   ```bash
   linkedin funding-import \
     --company "Acme AI" \
     --round seed --amount "$2.5M" \
     --investors "Sequoia, a16z" \
     --description "AI agent for SMB accounting"
   ```
4. CLI shows match — operator skims (5 sec) — moves on to next
5. Total session: ~5-10 minutes for 5-10 imports
6. The imported prospects then flow through the existing pipeline:
   - Next `linkedin daily` (manual or cron) → react + draft connect note
   - Operator approves drafts on Telegram
   - Standard outreach proceeds

**Expected throughput:** ~5-10 imports/day → ~30-50 invites/week through the system (after dedup + accept rate). At a conservative 30% acceptance + 30% reply, that's ~3-5 real conversations/week from this channel alone.
