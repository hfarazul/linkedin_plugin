# Handoff: `linkedin hiring-import` CLI (v1)

**Status:** Spec locked. Ready for implementation.
**Estimated effort:** 2-3 hours (single developer or AI agent — most plumbing is reusable).
**Owner:** Cortivo LinkedIn outreach system.
**Related campaign:** new `campaigns/hiring-first-engineer.md` (to be created in this work).

---

## 1. Goal

Add a CLI command that imports a company **actively hiring their first or second engineer** into a new campaign as a prospect, with the hiring signal captured in `pitch_context` for the drafter.

**Why:** The `funding-import` flow targets companies through funding events (cash + urgency to ship). But the broader Cortivo ICP is "operator running a working business who needs tech help" — which only sometimes intersects with funding events. A company **publicly signaling they're hiring engineering** is a direct, unambiguous version of the ICP: they're admitting in writing that they need eng capacity.

Same five-stage funnel as funding-import: source → entity → LinkedIn lookup → ICP gates → outreach. The hard work (founder scoring, team check, audience-label gate, Telegram approval flow) already exists. This variant only differs in the **source** and the **pitch_context wording**.

**Scope:** Manual, one-at-a-time CLI. Operator skims hiring-signal articles or job boards, identifies a company, runs `hiring-import` with the role + posting context. Auto-sourcing from RSS / YC Work at a Startup / LinkedIn Jobs is **v2**, explicitly out of scope here.

---

## 2. Context (what's already built and reusable)

| Component | Reuse as-is | File |
|---|---|---|
| Founder scoring (attribution-bounded, normalized variants, threshold 20) | ✅ no changes | `linkedin_agent/funding_lookup.py` (`find_founder`, `_score_candidate`, `_company_variants`) |
| Team check (CTO + builder-engineer disqualification, past-role detection) | ✅ no changes — the rules "no CTO, < 2 builder engineers" still describe the ICP | `linkedin_agent/funding_lookup.py` (`check_team`, `_is_current_employee`) |
| Drafter agent + audience-label post-check | ✅ applies to all kinds equally | `.claude/agents/message-drafter.md`, `linkedin_agent/drafter.py` (`AUDIENCE_LABELS`, `_contains_audience_label`) |
| Cooldown tracking | ✅ applies if a hiring-import prospect later gets withdrawn | `linkedin_agent/db.py` (`cooldown_until`, `cooldowns` CLI) |
| Telegram approval flow | ✅ no changes | `linkedin_agent/bot_daemon.py`, `linkedin_agent/telegram.py` |
| Send-window + caps + dedup mechanics | ✅ no changes | various |

**Read these before starting:**
- `docs/HANDOFF_funding-import.md` — the parent spec; mirror its style and rigour
- `.claude/skills/news-signal-outreach.md` — the general pattern and pitfalls (Unipile throttle, LinkedIn cooldown, common-word disambiguation, audience-label leak)
- `linkedin_agent/cli.py` — `funding_import` function (the structural template)
- `tests/test_funding_import_cli.py` — the test patterns to mirror

---

## 3. Locked decisions

| Decision | Value | Rationale |
|---|---|---|
| **Source (input modality)** | Manual operator entry via CLI flags | Same v1 model as funding-import. Operator skims a hiring signal (LinkedIn post, Google News, YC Work at a Startup) and runs the CLI. |
| **Stage filter** | Pre-seed / Seed / Series A under $10M (looser than funding-import) | A hiring signal alone is a fit signal; we don't also need cash urgency. Larger companies hiring their Nth engineer fall out via the team check. |
| **Geo filter** | None (mirrors funding-import) | Operator filters at input time. |
| **Confirmation flow** | Show match → auto-import | Same as funding-import. |
| **Match strictness** | Reuse funding-import threshold (≥ 20) | Same scoring logic. |
| **Cross-campaign dedup** | Skip with warning if prospect already in **another** campaign; force-overwrite `pitch_context` if already in **target** campaign | Same as funding-import. Hiring signals can refresh a prospect's pitch_context. |
| **Missing campaign in DB** | Auto-sync from `campaigns/<slug>.md` | Same as funding-import. |
| **Action log on no-match** | Write `actions` row with `prospect_id=NULL`, `result=skipped_no_match` | Same as funding-import. |
| **Default campaign slug** | `hiring-first-engineer` | Must create this campaign brief in scope. |
| **Action kind in log** | `hiring-import` | Distinguishes from `funding-import` in audit queries. |
| **CLI command name** | `linkedin hiring-import` | Parallel to `linkedin funding-import`. |
| **Search budget per import** | 4 units (same as funding-import: 1 founder + 3 team check) | Reused logic. |

---

## 4. Architecture

```
Operator skims hiring signal — LinkedIn post / Google News / YC / job board
        ↓
linkedin hiring-import --company "Acme" --role "first engineer" \
                       --posted "2 days ago" --description "AI for legal docs"
        ↓
funding_lookup.find_founder()           [REUSED]
  ├─ adapter.search(f'"{company}" founder', limit=10)
  ├─ Score each result (attribution-bounded match)
  └─ Return top non-rejected candidate
        ↓
Threshold check (in CLI)                 [REUSED logic, new command]
  └─ If match.score < 20 → log skipped_no_match, exit 1
        ↓
funding_lookup.check_team()              [REUSED]
  ├─ 3 sub-queries: "<company>" CTO, engineer, "founding"
  ├─ Filter to current employees (past-role markers honored)
  └─ Disqualify if CTO found OR 2+ builder engineers
        ↓
Cross-campaign dedup                     [REUSED logic]
  ├─ Already in another campaign → warn + skip
  ├─ Already in target campaign → force-overwrite pitch_context
  └─ Else proceed
        ↓
db.upsert_prospect(...)                  [REUSED]
  ├─ campaign_id = hiring-first-engineer
  ├─ pitch_context = format_hiring_pitch_context(...)   [NEW]
  ├─ status = "targeted"
  └─ provider_id from search hit
        ↓
db.log_action(pid, "hiring-import", payload, ...)  [NEW kind]
        ↓
Existing pipeline takes over (react → connect_note draft → ...)
```

**Net new code surface**: ~50-80 LOC across the CLI command and a small pitch_context formatter. Plus the campaign brief and tests.

---

## 5. CLI specification

### Command

```bash
linkedin hiring-import --company NAME [OPTIONS]
```

### Flags

| Flag | Required | Default | Notes |
|---|---|---|---|
| `--company` | **Yes** | — | Company name (e.g. "Acme AI"). |
| `--role` | No | — | The role they're hiring (e.g. "first engineer", "founding engineer", "technical co-founder", "senior eng"). Feeds pitch_context — gives the drafter a concrete hook. |
| `--posted` | No | — | Free-form when the hiring signal appeared (e.g. "3 days ago", "Jan 15", "this week"). Feeds pitch_context. |
| `--description` | No | — | One-line product summary if known (helps drafter pick a proof point). |
| `--source-url` | No | — | The article/post URL where the operator saw the signal. Logged in the action payload for audit; not surfaced to drafter. |
| `--campaign` | No | `hiring-first-engineer` | Slug of target campaign. |
| `--dry-run` | No | False | Show match + would-be import, but don't write to DB. |

### Example invocations

```bash
# Minimal — just company name
linkedin hiring-import --company "Acme AI"

# Typical: signal seen in a LinkedIn post by the founder
linkedin hiring-import --company "Acme AI" \
  --role "first engineer" \
  --posted "today" \
  --description "AI-native accounting for SMBs" \
  --source-url "https://www.linkedin.com/posts/jane/..."

# Preview match without committing
linkedin hiring-import --company "Acme AI" --dry-run
```

### Output (match found)

```
Searching LinkedIn for founder of "Acme AI"...
✓ Match (score 28): Jane Smith
  Headline:  CEO @ Acme AI | building agentic accounting for SMBs
  Location:  San Francisco, California, United States
  URL:       https://www.linkedin.com/in/janesmith
  Signals:   founder/CEO of 'acme ai' (attributed); CEO role
  Team:      1 current employee(s) seen — proceeding

✓ Imported prospect #234 into campaign hiring-first-engineer
  pitch_context: Hiring first engineer (posted today). Building Acme AI — 
                 AI-native accounting for SMBs.
```

### Output (no confident match)

```
Searching LinkedIn for founder of "Better"...
✗ No confident founder match (top score 9, threshold 20). 
  Try a more specific name (e.g., "Better Mortgage") or verify the company on LinkedIn manually.
```

### Output (ICP miss — already has eng team)

```
Searching LinkedIn for founder of "Acme AI"...
✓ Match (score 28): Jane Smith
  Headline:  CEO @ Acme AI | building agentic accounting for SMBs
  ...
⚠ ICP miss — has CTO (John Engineer)
  (3 current employee(s) seen across team-check queries)
  Skipped — campaign targets companies without an in-house eng team.
```

### Output (already exists in another campaign)

```
Searching LinkedIn for founder of "Acme AI"...
✓ Match (score 28): Jane Smith
⚠ Jane Smith is already prospect #87 in campaign recently-funded-non-tech (status: connection_sent).
  Skipped — to move her, do it manually via SQL.
```

---

## 6. Implementation plan

### Files to create

| File | Purpose | Approx. LOC |
|---|---|---|
| `campaigns/hiring-first-engineer.md` | Campaign brief — pitch positioning for the hiring-signal ICP | ~50 |
| `tests/test_hiring_import_cli.py` | Integration tests for the CLI command | ~120 |

### Files to modify

| File | Change |
|---|---|
| `linkedin_agent/cli.py` | Add `@cli.command("hiring-import")` block (~90 lines). Mirrors `funding_import` structurally. |
| `linkedin_agent/funding_lookup.py` | Add `format_hiring_pitch_context()` function (~15 LOC). Module name is slightly misleading post-this-change; consider renaming to `lookup.py` in a follow-up, but **don't** do so in this PR — keep the diff focused. |

### Campaign brief — what should be in it

`campaigns/hiring-first-engineer.md` should follow the structure of `campaigns/recently-funded-non-tech.md`:

```yaml
---
slug: hiring-first-engineer
name: Hiring First Engineer
status: active
target_icp: Non-technical founders actively hiring their first or second engineer — a public signal that they need engineering capacity but don't yet have it in-house. Either pre-Series A or post-Series A but lean. The hiring signal can be a LinkedIn post by the founder, a job listing on YC Work at a Startup, or a Google News mention.
---
```

Pitch section: same Cortivo positioning as `recently-funded-non-tech` — pair one senior engineer + AI tooling, ship v1 in 6-10 weeks vs the hiring slog. Pain points should reference the hiring-specific reality:

- 3-month average time from posting a job to a senior engineer's start date
- Compensation + recruiting cost adds 25-30% on top of base
- A wrong first hire takes 6+ months to unwind

Proof points: same as `recently-funded-non-tech` (Experial, Bespoke, Microforge, Mastercard, Amazon). Reuse `campaigns/_cortivo.md` as the canon.

Anti-claims: usual list + "don't reference their job posting itself by URL — operator-skimmed signal should not look surveilled."

### `format_hiring_pitch_context()` (in `funding_lookup.py`)

```python
def format_hiring_pitch_context(
    company: str,
    role: str | None,
    posted: str | None,
    description: str | None,
) -> str:
    """Build the pitch_context string for the drafter, hiring-signal flavor.

    Examples:
      All fields:   "Hiring first engineer (posted today). Building Acme AI
                     — AI-native accounting for SMBs."
      Role only:    "Hiring first engineer. Building Acme AI."
      Just company: "Hiring engineering. Building Acme AI."
    """
    if role and posted:
        hiring_phrase = f"Hiring {role} (posted {posted})"
    elif role:
        hiring_phrase = f"Hiring {role}"
    else:
        hiring_phrase = "Hiring engineering"
    base = f"{hiring_phrase}. Building {company}"
    if description:
        base += f" — {description}"
    return base + "."
```

### CLI command structure

Model the `hiring_import` function on `funding_import` in `linkedin_agent/cli.py`. The skeleton:

```python
@cli.command("hiring-import")
@click.option("--company", required=True, ...)
@click.option("--role", default=None, ...)
@click.option("--posted", default=None, ...)
@click.option("--description", default=None, ...)
@click.option("--source-url", "source_url", default=None, ...)
@click.option("--campaign", default="hiring-first-engineer", show_default=True, ...)
@click.option("--dry-run", "dry_run", is_flag=True, default=False, ...)
def hiring_import(company, role, posted, description, source_url, campaign, dry_run):
    """..."""
    from . import funding_lookup
    cfg, adapter = _adapter()
    try:
        # 1. Resolve campaign (auto-sync from markdown if not in DB yet)
        # 2. safety.check_cap(cfg, "search")
        # 3. funding_lookup.find_founder(adapter, company)
        # 4. Log 'search' action (1 unit budget)
        # 5. If no match / below threshold → log skipped_no_match, exit 1
        # 6. funding_lookup.check_team(adapter, company, founder_url=...)
        # 7. If disqualified → log skipped_has_eng_team, exit 1
        # 8. Dedup check (cross-campaign / same-campaign)
        # 9. pitch_context = funding_lookup.format_hiring_pitch_context(...)
        # 10. If dry-run → print, return
        # 11. db.upsert_prospect + set_pitch_context if existing
        # 12. db.log_action(pid, "hiring-import", json.dumps({...}), ...)
    finally:
        adapter.close()
```

Use the existing `funding_import` function in `cli.py` as the source-of-truth template. **You should be able to copy ~80% of that code unchanged and only swap the action kind + pitch_context call.**

### Action log payload

```python
db.log_action(
    pid, "hiring-import",
    json.dumps({
        "company": company,
        "role": role,
        "posted": posted,
        "description": description,
        "source_url": source_url,
        "campaign": campaign_slug,
        "match_score": match.score,
        "match_signals": match.signals,
        "team_check": {
            "cto_found": team.cto_found,
            "cto_name": team.cto_name,
            "builder_engineers": team.builder_engineers,
            "employees_seen": team.employees_seen,
        },
        "result": "imported" | "skipped_no_match" | "skipped_dedup" | "skipped_has_eng_team",
    }),
    match.hit.linkedin_url if match else "",
    cfg.dry_run,
)
```

---

## 7. Test plan

### Unit tests

Add to `tests/test_funding_lookup.py` (since the new function lives in that module):

1. **`test_format_hiring_pitch_context_full`** — all fields present
2. **`test_format_hiring_pitch_context_role_only`** — just role, no posted/description
3. **`test_format_hiring_pitch_context_minimal`** — just company name
4. **`test_format_hiring_pitch_context_posted_no_role`** — edge case (skip the "posted" phrasing when role is absent)

### Integration tests (`test_hiring_import_cli.py`)

Mirror `test_funding_import_cli.py` exactly, swapping `funding-import` → `hiring-import` and the payload fields. The fake adapter already supports the `LINKEDIN_FAKE_EMPTY_TEAM_CHECK` knob, so most tests come for free.

5. **`test_cli_imports_clean_match`** — Stub adapter, run CLI, verify prospect inserted with correct pitch_context.
6. **`test_cli_auto_syncs_missing_campaign`** — Verify auto-sync from `campaigns/hiring-first-engineer.md`.
7. **`test_cli_skips_on_no_match`** — Empty search, exit 1, action logged with prospect_id=NULL.
8. **`test_cli_skips_below_threshold`** — Founder appears but score < 20.
9. **`test_cli_skips_on_cross_campaign_dedup`** — Pre-seed a prospect in another campaign; CLI warns + skips.
10. **`test_cli_same_campaign_reimport_overwrites_pitch_context`** — Re-running on same campaign refreshes pitch_context.
11. **`test_cli_dry_run_no_writes`** — `--dry-run` flag → no DB row, no action logged.
12. **`test_cli_optional_fields_omitted`** — Just `--company`, pitch_context falls back to "Hiring engineering. Building X."
13. **`test_cli_logs_action_with_structured_payload`** — Verify all fields appear in the actions row.
14. **`test_cli_respects_search_cap`** — When search cap exhausted, exits with cap error.
15. **`test_cli_skips_when_team_check_finds_cto`** — With `LINKEDIN_FAKE_EMPTY_TEAM_CHECK` removed, FakeAdapter's CTO-containing headlines trigger the disqualification correctly.

### Coverage target

All tests run via `pytest` default suite (no live API calls). Full existing suite must still pass.

---

## 8. Edge cases

| Case | Handling |
|---|---|
| Company hiring "AI engineer" but already has 1 ML engineer + 1 software engineer | `check_team` catches: 2+ builder engineers → ICP miss, skip. Correct. |
| Company "hiring first engineer" but actually has a CTO who's the only "engineer" | `check_team` finds the CTO → skip. Correct (the CTO IS their eng team for now). |
| Operator misspells the role ("frist engineer") | The role string is passed through to `pitch_context` unchanged. The drafter is robust to typos; usually fine. Operator can re-run with corrected spelling if the draft reads poorly. |
| Role is something tangential ("VP marketing") | The system doesn't validate. The drafter will incorporate it into the connect note, which may produce an awkward result. Operator should constrain themselves to engineering-related roles. |
| `--posted` is far in the past ("6 months ago") | The system doesn't validate freshness. If the posting is stale, the drafter may misframe the urgency. Operator's call. |
| Same company has multiple hiring signals at different times | Re-importing same company in same campaign force-overwrites pitch_context — the latest signal wins. Status untouched. |
| Founder's LinkedIn shows them looking for a co-founder, not yet a company | Score will be low (no company name to match against). Below threshold → skip. Operator should wait until the company has a name. |

---

## 9. Out of scope (v2+)

These are explicitly NOT in v1. Listed here so they don't accidentally creep in.

- **Auto-sourcing from RSS** — e.g. `linkedin hiring-import-auto --source google-news` that pulls a feed and loops over `find_founder` for each. The `news-signal-outreach` skill describes the pattern.
- **YC Work at a Startup integration** — public job board for YC companies; high-density ICP. Worth a dedicated source adapter in v2.
- **LinkedIn Jobs via Unipile** — would be ideal if Unipile supports a `linkedin/jobs/search` endpoint. Check + add in v2.
- **Indeed / Wellfound integration** — broader job boards, lower density. v3.
- **Auto-detection of "first engineer" vs "Nth engineer"** — currently the operator passes the role text. v2 could parse a job posting URL to extract this.
- **Job-posting freshness check** — automatically skip postings older than N days. v2.
- **Cross-source dedup** — if the same company surfaces in both a funding-event RSS and a hiring RSS within a week, dedup to one import. Comes after multi-source v2.

---

## 10. Acceptance criteria

The v1 build is done when **all** of these hold:

1. ✅ `linkedin hiring-import --company "X"` runs without crashing on any string input.
2. ✅ A successful match outputs name, headline, location, score, URL, signals, team check — then imports the prospect with the structured pitch_context.
3. ✅ A failed match (below threshold) outputs the top score + clear next-step suggestion, with no `prospects` row writes (only an `actions` row).
4. ✅ ICP miss from `check_team` outputs the disqualification reason and skips.
5. ✅ A cross-campaign dedup outputs the existing campaign + status, with no prospect writes.
6. ✅ Same-campaign re-import force-overwrites pitch_context (status untouched).
7. ✅ `--dry-run` works as advertised (preview only, no writes).
8. ✅ Action log has a `hiring-import` row for every attempted import (imported / skipped_no_match / skipped_dedup / skipped_has_eng_team).
9. ✅ All 15 listed tests pass.
10. ✅ Full existing test suite still passes (no regressions).
11. ✅ `campaigns/hiring-first-engineer.md` exists and is loadable via `linkedin campaign sync`.
12. ✅ The drafter audience-label gate still catches "non-tech founder" / "first-time founder" leaks for drafts generated from this campaign's prospects (no new test needed — the gate is at the drafter layer and already covered).
13. ✅ Committed and pushed to a feature branch.

---

## 11. Open questions (raise BEFORE building if any)

After the funding-import experience, these are the spots where ambiguity could cause rework. Surface them to the operator before starting:

- **Campaign brief tone**: the `recently-funded-non-tech` brief uses "post-raise hiring slog" framing heavily. The hiring-signal campaign should use different language since these prospects haven't necessarily raised. Suggest "you posted that you're hiring — let's compare notes on the build-vs-hire math" framing. Confirm with operator.
- **Source-URL handling**: do we persist `source_url` in any prospect column for later reference (e.g. so the drafter can cite "you mentioned in your LinkedIn post..."), or only in the action log? Default: only in the action log (avoid leaking surveillance vibes — see anti-claims).
- **Role taxonomy**: do we want any structured enum for `--role` ("first_engineer" / "founding_engineer" / "senior_engineer" / "technical_cofounder")? Probably not for v1 — keep free-form. The drafter handles free-form better than rigid taxonomies.

If anything else surfaces during implementation, **stop and ask** — the cost of one extra clarifying question is much lower than the cost of building the wrong thing. (Learned this the hard way during the funding-import withdrawal incident — see `docs/HANDOFF_funding-import.md` for that footnote.)

---

## Appendix A: Sample workflow (operator perspective)

**Morning routine:**

1. Operator opens https://www.workatastartup.com/companies?role=engineer (or scrolls LinkedIn for founder posts about hiring)
2. Skims for ~5-10 minutes, identifying companies where the founder is clearly non-technical (heuristics: bio mentions ex-PM / ex-Consulting / ex-MBA / ex-Sales background; or the post explicitly says "I'm a non-technical founder")
3. For each candidate, runs:
   ```bash
   linkedin hiring-import \
     --company "Acme AI" \
     --role "first engineer" \
     --posted "this week" \
     --description "AI-native accounting for SMBs"
   ```
4. CLI shows match — operator skims (5 sec) — moves on to next
5. Total session: ~5-10 minutes for 5-10 imports
6. The imported prospects then flow through the existing pipeline:
   - Next `linkedin daily` (manual or cron) → react + draft connect note via the message-drafter
   - Operator approves drafts on Telegram
   - Standard outreach proceeds (connect → DM1 → DM2 → DM3 with the existing follow-up cadence)

**Expected throughput:** ~5-10 imports/day → ~25-50 invites/week through the system (after dedup + accept rate). Hiring-signal acceptance rates tend to be **higher than funding-event** because the prospect has explicitly self-identified as needing what we offer. Conservative estimate: 40% acceptance × 40% reply → ~4-8 real conversations/week from this channel alone.

---

## Appendix B: Why this variant matters

The `recently-funded-non-tech` campaign targets prospects through a **proxy** signal (just raised → probably needs to ship → maybe a non-tech founder → maybe no eng team). At each link in that chain, the conversion narrows. Real conversion rate from a Google News RSS funding article to a sendable connect note is ~16-20%.

The `hiring-first-engineer` campaign targets prospects through a **direct** signal (publicly hiring engineering → by definition needs engineering capacity → almost certainly fits if team-check passes). Expected funnel:

- Source → entity extraction: same as funding (regex on titles, dedup by company)
- LinkedIn lookup: same as funding (`find_founder`)
- Team check: same as funding, but inversely correlated with the signal — if they're "hiring their first engineer", the team check passing is almost tautological
- Operator spot-check: still required (wrong-person matches, geographic mismatches)

Expected hit rate at v1 (manual): **30-40%** would_import (vs funding's ~16-20%). At v2 (with auto-sourcing from a dedicated job board like YC Work at a Startup): potentially **50-60%** since the source itself is pre-filtered for early-stage startups.

This is the next-best ROI for outbound, ahead of broader signals like industry-event attendance or product-launch news.
