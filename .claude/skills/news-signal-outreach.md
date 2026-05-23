---
name: news-signal-outreach
description: Combine a news/RSS source with Unipile's LinkedIn API to drive a targeted outreach campaign — pull signals (funding rounds, job postings, acquisitions, product launches, IPO filings, etc.) from RSS, parse out the relevant entity, look it up on LinkedIn, filter through the campaign's ICP gates, then react + draft + push to Telegram for approval. Use when the user wants to drive outreach from a news signal: "find recently-funded non-tech founders", "target companies hiring their first engineer", "reach out to founders of recently-acquired startups", "run another funding-import batch", etc.
---

# News-signal-driven LinkedIn outreach

A reusable pattern that converts external news into pipeline prospects:

```
news source → entity extraction → LinkedIn match → ICP gates → outreach
```

The most-developed instance today is **funding events** (`recently-funded-non-tech` campaign), but the same plumbing applies to any signal where a public news article identifies a company/person worth approaching.

## Variants of the pattern

| Signal | Source examples | Entity | Why it's a good signal |
|---|---|---|---|
| **Funding round** | Crunchbase News, TechCrunch, Google News RSS for `"raises $" seed` | Company name → founder | Cash + urgency + likely build-side gap |
| **Hiring "first engineer"** | LinkedIn jobs RSS, AngelList Talent, "we're hiring" posts | Company name → CEO/founder | Active admission they need eng help |
| **Acquisition (acquirer side)** | TechCrunch deals feed | Acquirer → CTO/Head of Eng | Integration work; budget unlocked |
| **Product launch (post-seed)** | Product Hunt, HN "Show HN" | Company name → founder | Validation that v1 shipped — DM1 lands harder |
| **IPO / S-1 filing** | SEC EDGAR filings RSS | Company name → Head of Eng | Pre-IPO scaling pressure |
| **Conference attendance** | Event speaker lists, conference Twitter | Person name | Specific shared context for the connect note |

Each variant differs in:
1. **Source feed/API** — what URL we hit
2. **Entity extraction** — what regex/heuristic pulls the right thing from titles
3. **Which ICP gates apply** — team-check is funding-specific; other signals need different filters
4. **`pitch_context` template** — wording the drafter uses to reference the signal

## When NOT to use this pattern

- For prospects who already replied or interacted (use the standard `daily` cron + drafter)
- For ICPs **not** tied to a public news event — e.g. "operators of working businesses needing tech help" surfaces better via LinkedIn post-search (`search-posts`) than via funding events
- When speed beats precision — this is deliberately strict (threshold 20, team-check, manual operator approval)

## The five hard rules

These hold regardless of which signal variant you're running.

1. **Never re-invite a recently-withdrawn prospect.** LinkedIn enforces a ~2-3 week anti-spam cooldown — calls to `/users/invite` return `422 errors/already_invited_recently`. Bumping local caps does not help; the block is server-side. **If the user asks to withdraw + redraft, surface this BEFORE executing**. The 7 prospects currently in cooldown (run `linkedin cooldowns`) are the receipt for getting this wrong.
2. **Default to `--dry-run` on the first scan of a new query batch.** Cheap preview before consuming Unipile budget on real imports.
3. **One `funding-import` call ≈ 4 Unipile search units** (1 founder lookup + 3 team check). Budget accordingly; cap is 200/24h.
4. **Pace burst calls — and budget for the day-level limit.** Two throttle tiers: (a) Unipile-side burst throttle after ~25 rapid-fire searches, clears in 60-180s; (b) LinkedIn account-level people-search throttle after ~150-200 people-searches/day, clears in **6-24 hours** and only affects people-search (posts still work). Sleep 2s between calls AND cap total imports at ~25/day. See "Search throttling — two distinct tiers" section for diagnostics.
5. **Withdraw is destructive.** Once an invitation is withdrawn, the recipient's pending-invite view loses it; we cannot retrieve. Confirm scope with the user before batch-withdrawing.

## Worked example: funding-events campaign

Below is the funding-event variant in full. Other variants follow the same shape; substitute source / extraction / ICP-gate as needed.

### 1. Pull RSS candidates

Google News RSS is the workhorse source — 100 items/query, aggregates TechCrunch + Crunchbase News + FinSMEs + Yahoo Finance + others. **Crunchbase News RSS directly is too thin** (10 items/feed, mostly meta-articles) and Cloudflare-protected.

```python
import httpx, xml.etree.ElementTree as ET, urllib.parse

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/rss+xml, application/xml;q=0.9, */*;q=0.8',
}
QUERIES = [
    '"raises $" seed',
    'pre-seed funding round',
    '"announces seed round"',
    'seed funding round startup',
]

candidates = {}  # dedup by guid
for q in QUERIES:
    qs = urllib.parse.quote(q)
    r = httpx.get(
        f'https://news.google.com/rss/search?q={qs}&hl=en-US&gl=US&ceid=US:en',
        headers=HEADERS, timeout=15, follow_redirects=True,
    )
    root = ET.fromstring(r.text)
    for item in root.findall('.//item'):
        guid = item.findtext('guid') or item.findtext('link')
        candidates[guid] = {
            'title': item.findtext('title') or '',
            'link': item.findtext('link') or '',
            'pub': item.findtext('pubDate') or '',
        }
```

Expect ~200-300 unique items after 4-query dedup. Roughly half are real per-company funding events; the rest are meta pieces.

### 2. Extract entity (company name) from titles

For funding events, regex these patterns:

- `"<Company> Raises $<X> [Pre-Seed|Seed]"`
- `"<Company> Closes $<X> ..."`
- `"<Company> Announces Seed Round"`
- `"<Company>: $<X> Pre-Seed Raised"`
- `"Exclusive: <Company>, ..."`

**Skip meta-article patterns**: "Week's 10 Biggest", "Top 10", "Where Founded Founders Went", "Funding Trends", "Roundup", "Briefing". Drop Series A+ rounds — campaign brief targets pre-seed/seed only.

**Watch out for extraction noise**: titles like `"Israeli AI Startup Conntour raises..."` make the regex grab "Israeli AI Startup Conntour" as the company. Strip common prefixes (`<Country> Startup`, `<Sector> Startup`, `AI-Powered <Anything> Startup`) before passing the company name to `funding-import`.

### 3. Dry-run each candidate

```bash
linkedin funding-import --company "<Name>" --dry-run
```

Categorize the output:
- `would_import` — score ≥ 20, no CTO, < 2 builder engineers → real candidate
- `has_cto` / `has_eng_team` → ICP miss, skip (campaign brief: "no in-house engineering team yet")
- `below_threshold` — founder appears with score < 20 → skip
- `no_match` — Unipile returned nothing → skip (may be flakiness; see hard rule #4)

Reasonable batch size: **25 candidates per scan**, sleep 2s between calls. Wait 60-180s if the throttle trips.

### 4. Spot-check the would-imports

Before committing real imports, eyeball each survivor's:

- **Founder headline** — does it really look non-technical? (Headlines like "Sales Specialist @ Nike | Founder of X" are usually wrong-person false matches.)
- **Location** — campaign brief targets US + UK + Europe; Korea/Australia/Brazil fall outside.
- **Round size** — anything over $20M "seed" is typically a Series A+ mislabeled; question fit.
- **Company-name common-word risk** — pass the **disambiguated** name when applicable: `--company "Better Mortgage"` not `"Better"`. The CLI's no-match nudges this in its error message.

### 5. Real import + react + draft + push

For each survivor:

```bash
linkedin funding-import \
  --company "<Name>" --round seed --amount '$XM' \
  --investors "Sequoia, a16z" \
  --description "<one-liner from the article>"

linkedin react <prospect_id>
```

Then drafter + Telegram push in-process (the daily cron processes the whole queue at once, which is usually not what you want for one-off imports):

```python
from linkedin_agent.config import load
from linkedin_agent.adapters import get_adapter
from linkedin_agent import db
from linkedin_agent.drafter import draft
from linkedin_agent.daily import _fetch_posts_for_draft
from linkedin_agent.telegram import TelegramClient

cfg = load()
adapter = get_adapter(cfg)
tg = TelegramClient(cfg)
try:
    for pid in (...):
        p = db.get_prospect(pid)
        posts = _fetch_posts_for_draft(adapter, p, cache={})
        body = draft('connect_note', pid, recent_posts=posts)
        did = db.enqueue_draft(pid, 'connect_note', body)
        camp = db.get_campaign(int(p['campaign_id'])) if p['campaign_id'] else None
        mid = tg.push_draft_for_approval(
            draft_id=did, kind='connect_note', body=body,
            prospect_name=p['full_name'], prospect_company=p['company'],
            prospect_url=p['linkedin_url'],
            campaign_name=camp['name'] if camp else None,
        )
        db.set_draft_telegram_id(did, mid)
finally:
    adapter.close()
    tg.close()
```

Operator reviews each card on Telegram. Approve → connection request goes out. Reject → no harm. Edit → tweak on phone, then approve.

## Adapting to a new signal variant

When the user says "I want to target X" where X is a different news signal, walk this checklist:

1. **What's the source?**
   - Google News RSS is the easiest default — pick 2-4 queries that surface the signal cleanly
   - Specialized RSS feeds (TechCrunch venture, Sifted, SEC EDGAR) work too if more targeted
   - For non-RSS (e.g. job boards) you may need a scraper instead

2. **What's the entity to extract?**
   - Usually a company name — same regex patterns mostly transfer
   - Sometimes a person name — if so, you'll need to add a `linkedin search <person>` direct lookup instead of the company→founder lookup `funding-import` does

3. **Does `funding-import` apply, or do you need a new command?**
   - If the variant uses **company → founder lookup + ICP gates**, `funding-import` works as-is (just change the `--description` and `--campaign` slug)
   - If the variant needs **a different ICP gate** (e.g. "company DOES have eng team" — opposite of what we want for funding events), you'll need a sibling CLI command. Mirror `funding-import`'s structure in `cli.py`; reuse `funding_lookup.find_founder()` but swap out `check_team()` for the new gate.

4. **What's the `pitch_context` template?**
   - The drafter reads this verbatim and weaves the signal context into the connect note
   - Examples:
     - Funding: `"Recently closed seed $2.5M from Sequoia, a16z. Building Acme AI — AI agent for SMB accounting."`
     - Job posting: `"Hiring their first engineer (posted 3 days ago). Building Acme AI — AI agent for SMB accounting."`
     - Acquisition: `"Just acquired by Stripe. Building Acme AI — payment automation."`

5. **Are there platform-specific cooldowns or limits?**
   - LinkedIn invitations: 3-week withdrawal cooldown (see hard rule #1)
   - LinkedIn DMs: 1st-degree only unless InMail credits available
   - LinkedIn search: ~200-500 calls/day via Unipile; bursts trip a short-term throttle

## Tooling reference

### CLI commands

| Command | Purpose |
|---|---|
| `linkedin caps` | Current usage vs daily limits (search/react/connect/dm) |
| `linkedin status` | Pipeline dashboard — stages, replies, pending drafts, follow-ups |
| `linkedin cooldowns` | Prospects on outreach cooldown (e.g. post-withdraw) |
| `linkedin cooldowns --revive-expired` | Flip cleared-cooldown prospects back to `reacted` |
| `linkedin pipeline --status targeted` | Fresh imports awaiting react |
| `linkedin funding-import --company X --dry-run` | One-off preview without DB writes |
| `linkedin react <pid>` | Manually react to one prospect's most recent post |
| `linkedin daily` | Full cron cycle — process the whole queue at once |

### Unipile endpoints

| Endpoint | Method | Use |
|---|---|---|
| `POST /linkedin/search` | search by keyword | founder lookup, team check, prospect search |
| `GET /users/{provider_id}/posts` | recent posts | drafter input |
| `POST /users/invite` | send connection request | bot daemon send path |
| `GET /users/invite/sent` | list pending sent invitations | post-withdraw verification |
| `DELETE /users/invite/sent/{invitation_id}` | withdraw invitation | cleanup (see cooldown rule) |
| `POST /chats` | send DM (multipart form-data) | DM1 / DM2 / DM3 / replies |

All require `account_id` parameter (set in `.env` as `UNIPILE_ACCOUNT_ID`). All return `422` with structured JSON error bodies when LinkedIn rejects.

### Common 422 error types worth handling

- `errors/already_invited_recently` — withdraw cooldown (~2-3 weeks)
- `errors/already_invited` — invitation still pending; cancel first if you want to re-send
- `errors/connection_already_exists` — they're already a 1st-degree connection; skip invite, DM directly
- `errors/cannot_invite` — generic rejection; usually LinkedIn flagged the account temporarily

## Pitfalls learned the hard way

### LinkedIn re-invite cooldown after withdrawal

**Once you withdraw an invitation, you cannot re-invite the same person for ~2-3 weeks** (server-side, not local). The endpoint returns `422 errors/already_invited_recently`.

When the user asks to withdraw + redraft, **proactively warn about the cooldown** before executing. The cleanup itself is fast; the cooldown lasts 3 weeks.

If a draft is awkward but already sent: prefer to leave it (most prospects don't re-read connection notes) or write a tighter DM1 to course-correct. Only withdraw if the message is actively damaging.

### Search throttling — two distinct tiers

There are **two separate throttling mechanisms** that both manifest as "search returns empty," and they require different responses. Distinguish by symptom:

**Tier 1 — Unipile-side burst throttle** (the friendly one)
- Triggered by 25+ rapid-fire searches in a 2-3 minute window
- All searches return empty (`items: []`, `total_count: 0`)
- Clears after **60-180 seconds of idle**
- Affects people + posts + every query type equally
- Mitigation: 2-second sleep between calls in batch scripts; wait 3 minutes if hit

**Tier 2 — LinkedIn account-level people-search throttle** (the deep one)
- Triggered by ~150-200+ people-searches in a day (the team-check fires 3 per import; this adds up fast across a campaign)
- **Only affects `category=people` queries** — `category=posts` still works fine
- Searches return `200 OK` with `total_count: 0` for ANY keyword (even "Anthropic", basic sanity terms — distinguishing this from Tier 1, which also returns 0 but clears quickly)
- Clears in **6-24 hours** (LinkedIn's anti-abuse decay), not minutes
- No 4xx code, no error body — just empty pages

**How to differentiate when you see empty results:**

```python
# Quick diagnostic — run both endpoints
people = adapter.search('founder', limit=3)
posts  = adapter.search_posts('hiring', limit=3, date_posted='past_week')

if not people and posts:
    # Tier 2: account-level throttle on people-search.
    # Wait until tomorrow. Post-search-based workflows still work.
elif not people and not posts:
    # Tier 1: burst throttle on everything.
    # Wait 60-180s and retry.
else:
    # Working — proceed.
```

**Tier 2 workaround for active campaigns**: since `category=posts` still works, you can:
- Use `linkedin search-posts` to find prospects via their actual content
- Pre-saved candidate queues (collected during normal operation) can still be acted on tomorrow

**To avoid tripping Tier 2**: limit total people-searches to ~100/day. Each funding-import or hiring-import costs 4 (1 founder + 3 team check), so cap at ~25 imports/day across both commands combined. The local `linkedin caps` counter only tracks logged actions, **not Tier 2 toward LinkedIn** — be careful.

### Company-name string match: common-word names

Companies named with common words ("Better", "Notion", "AI") collide with unrelated profiles in LinkedIn search. The matcher uses whole-word matching, but it can't disambiguate "Better Mortgage" vs "The Better Collab" from `--company "Better"` alone. **Pass the longer disambiguated name** when the common-word risk exists.

### Hyphen / suffix normalization

`funding-import` normalizes company variants internally (`"Browser Use"` matches `"browser-use"`, `"Bland AI"` matches `"Bland"`). In `funding_lookup._company_variants()`. If you see a no-match for a company you know exists on LinkedIn, check whether the founder's headline uses a different form than the article title — and consider whether a new variant pattern needs to be added.

### Drafter audience-segment leak

Hard rule #8 in `.claude/agents/message-drafter.md` forbids phrases like "non-tech founder(s)", "non-technical founder(s)", "first-time founder(s)". The drafter has a `_contains_audience_label` post-check that retries with a hint. **If you change campaign brief text that uses these phrases internally, the prompt rule + post-check still protect the output.** Don't remove either.

### "INSUFFICIENT_CONTEXT" from the drafter is OK

Some prospects don't have substantive recent posts. The drafter correctly returns `INSUFFICIENT_CONTEXT` rather than fabricating a generic message. These prospects either get marked `skipped` by the daily cron or stay at `reacted` for a later attempt. Don't override.

## Source quality reference

Ranked from probed sample data (May 2026):

| Source | Items/feed | Per-company-funding density | Notes |
|---|---|---|---|
| Google News RSS (search) | 100 | ~60-70% | Best aggregator; queries customizable |
| TechCrunch `/venture/feed/` | 20 | ~50% | Skewed to bigger deals ($20M+) |
| Sifted (Europe) | 24 | ~30% | European angle complement |
| Crunchbase News RSS | 10 | ~20% | Mostly meta articles; CF-protected |
| VentureBeat funding | 0 (404) | n/a | Feed has moved; not currently usable |

Default to Google News RSS as the primary source. Pull from TC + Sifted only if Google News is missing a specific geo or sector you're targeting.

## Realistic conversion rates (from real funding-event batches)

From 75 candidates dry-run across 3 batches on 2026-05-23/24:

- ~16-20% reach `would_import` after threshold + team-check
- ~25-30% disqualified by team-check (has CTO or has eng team)
- ~40-50% no-match (Unipile flakiness contributes, but also genuinely thin LinkedIn presence)
- ~10% below-threshold (founder exists but not enough signal)

**To net 10 sendable connect notes, plan for ~50-60 dry-runs.** At 4 search units each, that's 200-240 search budget. Run a deep campaign over **two days** if you want to stay under the 200/24h cap.

## File pointers

- `linkedin_agent/funding_lookup.py` — scoring, team check, normalization
- `linkedin_agent/cli.py` (`funding_import` command) — orchestration
- `.claude/agents/message-drafter.md` — drafter prompt (hard rule #8 = no audience labels)
- `linkedin_agent/drafter.py` — `_contains_audience_label` post-check + retry
- `docs/HANDOFF_funding-import.md` — original spec for funding-import
- `campaigns/*.md` — campaign briefs (one per ICP slice)
