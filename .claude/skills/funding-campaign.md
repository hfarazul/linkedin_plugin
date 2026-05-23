---
name: funding-campaign
description: Run a funding-event-sourced LinkedIn outreach campaign — pull recent seed/pre-seed announcements from Google News RSS, identify non-tech founders on LinkedIn, verify the company has no existing engineering team, import survivors, react + draft connect notes, push to Telegram for approval. Use when the user says "run a funding campaign", "find recently-funded non-tech founders", "do another batch of funding-import", or wants to grow the recently-funded-non-tech pipeline.
---

# Funding-event LinkedIn outreach campaign

A funnel that turns "X just raised $Y" news into pipeline prospects. Five filters in sequence:

1. **Google News RSS** — surface recent seed/pre-seed funding events (~50-200 articles/scan, 4 queries deduped)
2. **Title extraction** — pull company name + round + amount via regex; drop meta-articles
3. **LinkedIn founder lookup** — `funding-import --dry-run` searches Unipile for the company's founder, scores by attribution-bounded match
4. **Team check** — disqualify if company already has a CTO or 2+ builder engineers (no agency need)
5. **Operator approval** — Telegram card with connect note draft; tap Approve to send

End-to-end, expect roughly **15-20% of source articles to make it to a sendable draft**.

## When NOT to use this skill

- For prospects who replied or interacted (use the standard `daily` cron + drafter)
- For non-tech-founder ICPs that aren't tied to a funding event (use `search-posts` instead — better signal for "operator with working business needs tech help")
- When the user wants speed over precision — this is deliberately strict (threshold 20, team-check disqualification)

## Hard rules

- **Never re-invite a prospect we just withdrew from.** LinkedIn enforces a ~2-3 week anti-spam cooldown — calls to `/users/invite` return `422 errors/already_invited_recently`. Bumping local caps doesn't help; the block is server-side. Surface this BEFORE recommending any withdrawal action.
- **Withdraw is destructive.** Once an invitation is withdrawn, the recipient's pending-invite view loses it; we cannot retrieve. Confirm scope with the user before batch-withdrawing.
- **Default to dry-run for the first run on any new query batch.** Sneak peek of how the filters behave before consuming Unipile search budget on real imports.
- **One `funding-import` call ≈ 4 Unipile search units** (1 founder lookup + 3 team check). Budget accordingly when sizing a batch.

## Step-by-step playbook

### 1. Pull RSS candidates

Use Google News RSS — proven the highest-volume + most consistent source. **Crunchbase News RSS is too thin** (10 items/feed, mostly meta-articles) and Cloudflare-protected. TechCrunch + Sifted are useful supplements but not necessary for v1.

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

Expect **~100 items per query, ~200-300 after dedup**. About half are real per-company funding events; the rest are meta pieces and trend articles.

### 2. Extract company name + round + amount

Title-pattern based, mostly regex. The patterns to match:

- `"<Company> Raises $<X> [Pre-Seed|Seed]"`
- `"<Company> Closes $<X> ..."`
- `"<Company> Announces Seed Round"`
- `"<Company>: $<X> Pre-Seed Raised"`
- `"Exclusive: <Company>, ..."`

Use the working code at `/tmp/funding_candidates.json`-style intermediate state (the campaign-batching script reuses it).

**Skip patterns** (meta articles to drop): "Week's 10 Biggest", "Top 10", "Where Founded Founders Went", "Funding Trends", "Roundup", "Briefing". Also drop **Series A+** rounds — the campaign brief targets pre-seed/seed only.

**Watch out for extraction noise**: titles like `"Israeli AI Startup Conntour raises ..."` — the regex would grab "Israeli AI Startup Conntour" as the company name. Strip common prefixes (`"<Country> Startup"`, `"<Sector> Startup"`, `"AI-Powered Drone Supplier"`) before passing to `funding-import`. The CLI itself doesn't clean these.

### 3. Dry-run each candidate

For each unique company name, run:

```bash
linkedin funding-import --company "<Name>" --dry-run
```

Categorize the output:
- **`would_import`** (score ≥ 20, no CTO, < 2 builder engineers) → candidate for real import
- **`has_cto` / `has_eng_team`** → ICP miss, skip
- **`below_threshold`** (founder appears with score < 20) → noise, skip
- **`no_match`** → Unipile returned nothing, skip (may be flakiness — see pitfalls)

**Pace it.** Run dry-runs sequentially with a **2-second sleep between calls**. Unipile throttles after bursts of ~25 rapid-fire calls — symptoms are empty results for ALL queries, not 4xx errors. If you see ALL queries returning empty, wait 60-180 seconds and retry.

A reasonable batch size is **25 candidates per scan**, run twice with a pause if you want a deeper sweep. Cap is hard at 200 search budget per 24h.

### 4. Spot-check the would-imports manually

Before committing to import, eyeball each would-import's:

- **Founder headline** — does the founder look genuinely non-technical? (Headlines like "Sales Specialist @ Nike | Founder of X" are usually wrong-person false matches; the X is probably a side project they don't own.)
- **Location** — campaign targets US + UK + Europe. Korea / Australia / Brazil candidates fall outside the brief, even if everything else passes.
- **Round size** — anything over $20M "seed" is typically a Series A+ mislabeled or a giant extension. Question the fit; the brief targets sub-$3M typical.

Aim to import **clearly-non-tech founders only**. The pipeline downstream invests Cortivo's reputation in each one; a wrong-person import poisons the campaign.

### 5. Real import + react + draft + push

For each survivor:

```bash
# Real import (4 search calls consumed)
linkedin funding-import \
  --company "<Name>" \
  --round seed \
  --amount '$XM' \
  --investors "Sequoia, a16z" \
  --description "<one-liner from the article>"

# React to their most recent post (1 react call consumed)
linkedin react <prospect_id>

# Drafter + Telegram push — invoke programmatically, not via daily cron
# (so we don't accidentally batch-process the rest of the targeted queue)
```

For the draft + push step, use the in-process pattern (see `scripts/` or recreate inline):

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
    for pid in (...):  # list of newly-imported prospect IDs
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

The operator reviews each card on Telegram. Approve → connection request goes out. Reject → no harm. Edit → tweak on phone, then approve.

## Tooling notes

### Useful CLI commands

| Command | Purpose |
|---|---|
| `linkedin caps` | Check current usage vs. daily limits (search/react/connect/dm) |
| `linkedin status` | Pipeline dashboard — stages, replies, pending drafts, due follow-ups |
| `linkedin cooldowns` | List prospects on outreach cooldown (e.g. post-withdraw) |
| `linkedin cooldowns --revive-expired` | Flip cleared-cooldown prospects back to `reacted` |
| `linkedin pipeline --status targeted` | Inspect freshly-imported (pre-react) prospects |
| `linkedin funding-import --company X --dry-run` | One-off preview without DB writes |
| `linkedin react <pid>` | Manually react to one prospect's most recent post |

### Key Unipile endpoints

| Endpoint | Method | Use |
|---|---|---|
| `POST /linkedin/search` | search by keyword | founder lookup, team check, prospect search |
| `GET /users/{provider_id}/posts` | recent posts | drafter input |
| `POST /users/invite` | send connection request | the bot daemon's send path |
| `GET /users/invite/sent` | list pending sent invitations | post-withdraw verification |
| `DELETE /users/invite/sent/{invitation_id}` | withdraw an invitation | cleanup (but see cooldown warning) |
| `POST /chats` | send DM (multipart form-data) | DM1/DM2/DM3 / replies |

All require `account_id` parameter (set in `.env` as `UNIPILE_ACCOUNT_ID`). All return `422` with structured JSON error objects when LinkedIn rejects.

### Common 422 types worth handling

- `errors/already_invited_recently` — withdraw cooldown (~2-3 weeks)
- `errors/already_invited` — invitation still pending; cancel first if you want to re-send
- `errors/connection_already_exists` — they're already a 1st-degree connection; skip the invite step and DM directly
- `errors/cannot_invite` — generic rejection; usually means LinkedIn flagged the account temporarily

## Pitfalls (learned the hard way)

### LinkedIn re-invite cooldown after withdrawal

**Once you withdraw an invitation, you cannot re-invite the same person for ~2-3 weeks** (server-side, not local). The endpoint returns:

```json
{"status": 422, "type": "errors/already_invited_recently",
 "title": "Should delay new invitation to this recipient", ...}
```

Bumping `DAILY_MAX_CONNECTIONS` does not help. There is no API override.

**If a draft is awkward but already sent**: prefer to leave it (most prospects don't deeply re-read connection notes) or write a tighter DM1 to course-correct. Only withdraw if the message is actively damaging.

When the user asks to withdraw + redraft, **proactively warn about the cooldown** before executing. The cleanup work happens fast; the cooldown lasts 3 weeks.

### Unipile rate-limiting in bursts

Running 25+ searches back-to-back (e.g. the dry-run loop) triggers a short-term throttle. Symptoms:
- All subsequent searches return empty arrays (not 4xx — just no items)
- Affects all queries equally, even basic sanity checks like `search("founder")`
- Clears after 60-180 seconds of idle

**Mitigation**: sleep 2 seconds between calls in batch scripts. If a batch's sends start failing with empty results, wait 3 minutes and retry.

### Company-name string match: common-word names

Companies named with common words ("Better", "Notion", "AI") collide with unrelated profiles in LinkedIn search. The `funding-import` matcher uses whole-word matching, but it can't disambiguate "Better Mortgage" vs "The Better Collab" if you pass just `--company "Better"`. **Pass the longer, disambiguated name** when the company has a common-word substring (`--company "Better Mortgage"`, `--company "Notion Labs"`).

The CLI's no-match message already nudges this: "Try a more specific name (e.g. 'Better Mortgage')".

### Hyphen / suffix normalization

`funding-import` normalizes company variants internally (`"Browser Use"` → matches `"browser-use"`, `"Bland AI"` → matches `"Bland"`). This is in `funding_lookup._company_variants()`. If you see a no-match for a company you know exists on LinkedIn, check whether the founder's headline uses a different form than the article title — and if a NEW variant pattern needs to be added.

### Drafter audience-segment leak

Hard rule #8 in `.claude/agents/message-drafter.md` forbids phrases like "non-tech founder(s)", "non-technical founder(s)", "first-time founder(s)". The drafter has a `_contains_audience_label` post-check that retries with a hint. **If you change campaign brief text that uses these phrases internally, the prompt rule + post-check still protect the output.** Don't remove either.

### "INSUFFICIENT_CONTEXT" from the drafter is OK

Some prospects don't have substantive recent posts. The drafter correctly returns `INSUFFICIENT_CONTEXT` rather than fabricating a generic message. These prospects either get marked `skipped` by the daily cron or stay at `reacted` for a later attempt. Don't override.

## Source quality reference (when picking RSS queries)

Ranked from probed sample data:

| Source | Items/feed | Per-company-funding density | Notes |
|---|---|---|---|
| Google News RSS (search) | 100 | ~60-70% | Best aggregator; queries customizable |
| TechCrunch `/venture/feed/` | 20 | ~50% | Skewed to bigger deals ($20M+) |
| Sifted (Europe) | 24 | ~30% | European angle complement |
| Crunchbase News RSS | 10 | ~20% | Mostly meta articles; CF-protected |
| VentureBeat funding | 0 (404) | n/a | Feed has moved; not currently usable |

Default to Google News RSS as the primary source. Pull from multiple categories (TC, Sifted) only if Google News is missing a specific geo or sector you're targeting.

## Conversion rate reality (from real batches)

From 75 candidates dry-run across 3 batches on 2026-05-23/24:

- ~16-20% reach `would_import` after threshold + team-check
- ~25-30% disqualified by team-check (has CTO or has eng team) — the campaign's defining filter actually fires often
- ~40-50% no-match (Unipile flakiness contributes, but also genuinely thin LinkedIn presence)
- ~10% below-threshold (founder exists but not enough signal)

**To net 10 sendable connect notes, plan for ~50-60 dry-runs.** At 4 search units each, that's 200-240 search budget. Run the campaign over **two days** if you want to stay under the 200/24h cap.

## File pointers

- `linkedin_agent/funding_lookup.py` — scoring, team check, normalization
- `linkedin_agent/cli.py` (`funding_import` command) — orchestration
- `.claude/agents/message-drafter.md` — drafter prompt (incl. hard rule #8)
- `linkedin_agent/drafter.py` — `_contains_audience_label` post-check + retry
- `docs/HANDOFF_funding-import.md` — original spec for funding-import
- `campaigns/recently-funded-non-tech.md` — campaign brief (pitch, anti-claims, tone)
