# LinkedIn Outreach Agent — Development & Testing Plan

**Owner**: you (solo, possibly small team later)
**Use case**: software agency lead generation
**Last updated**: 2026-05-16

---

## 1. What this delivers

A LinkedIn outreach system that runs your agency lead-gen pipeline with you in the loop only for the high-judgment moments. Concretely:

- **Multiple concurrent campaigns** (e.g., "AI dev pod", "Rails rescue") each with their own pitch brief and ICP
- **Search-driven discovery** via Unipile against your real LinkedIn account
- **Hourly cron** (Mon-Fri 9am–5pm) that does the mechanical work: react to relevant posts, send drafted connection requests, send drafted DMs, poll for replies
- **Telegram bot as the daily UI** — every outbound message lands in your chat with an Approve button. Tap from your phone, message sends. Reply in chat to edit.
- **Reply detection** — new inbound messages surface in Telegram within 15 minutes, sequence halts automatically
- **Audit trail** — every action (sent, drafted, approved, rejected) persisted in SQLite
- **Status dashboard** — `linkedin status` shows pipeline by stage, caps, pending approvals, replies needing attention

**End state**: setup script + cron + Telegram = ~5 min of taps per day on your phone, ~15 min/week refining campaign briefs in Claude Code.

---

## 2. Architecture in 30 seconds

```
┌──────────────────┐
│  campaigns/*.md  │  pitch briefs in version control
└────────┬─────────┘
         │ context
         ▼
   hourly cron (9-5 Mon-Fri)
         │
         │  invokes: claude -p "/draft <kind> <prospect>"
         ▼
   message-drafter subagent  ◄──── uses Claude Code's existing auth, no separate key
         │ returns draft text
         ▼
   pending_drafts table  ─────► Telegram push (Approve/Edit/Reject)
                                       │
                                       ▼  tap approve
                                 Unipile send
                                       │
                                       ▼
                                  LinkedIn
```

Three external services with their own auth:

| Service | Purpose | Credential | Setup |
|---|---|---|---|
| **Unipile** | LinkedIn engagement API | `UNIPILE_API_KEY` + `UNIPILE_ACCOUNT_ID` + `UNIPILE_DSN` | Sign up at unipile.com, connect LinkedIn, copy three values |
| **Telegram** | Approval UI + reply notifications | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | @BotFather for token, @userinfobot for chat ID |
| **Claude Code** | Drafter subagent + interactive use | Existing Claude Code auth | Already set up — no new keys |

No Anthropic API key. No Apollo. No MCP servers. No web UI.

---

## 3. Development phases

Each phase below has: **goal · files · automated test · manual test · done criteria**. Manual tests use Playwright where applicable to verify LinkedIn-side state.

### Phase 0 — Unipile live verification *(GATE)*

**Goal**: confirm `UnipileAdapter` actually works against your real LinkedIn account. Until this is green, nothing downstream is real.

**Files**: `tests/test_unipile_live.py` (new, marked `@pytest.mark.live`)

**Automated test**: one call per adapter method (search/posts/react/connect/dm), all targeting a single throwaway prospect you control (e.g., a secondary account you can clean up after). Run with `pytest -m live`.

**Manual test (Playwright)**: after `react` and `connect` calls succeed via API, open Playwright (or your own browser) and:
1. Navigate to the post URL you reacted to. Verify the reaction shows up under your name in the reactions list.
2. Navigate to the prospect's profile. Verify the connection button now shows "Pending" instead of "Connect".

Use Claude Code's Playwright MCP if available, or `playwright codegen linkedin.com` to record a quick verification flow.

**Done when**: `pytest -m live` is green AND Playwright confirms the reactions/connections actually visible on LinkedIn.

**Time**: 30 min for me + ~15 min for you to sign up for Unipile.

---

### Phase 1 — Schema extensions

**Goal**: extend the DB for agency lead-gen (campaigns, dispositions, follow-up tracking, pending drafts queue).

**Files**: `linkedin_agent/db.py`

**Schema additions**:
```sql
CREATE TABLE campaigns (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  brief_path TEXT NOT NULL,        -- campaigns/<slug>.md
  target_icp TEXT,
  status TEXT DEFAULT 'active',    -- active | paused | archived
  created_at TEXT NOT NULL
);

CREATE TABLE pending_drafts (
  id INTEGER PRIMARY KEY,
  prospect_id INTEGER REFERENCES prospects(id),
  kind TEXT NOT NULL,              -- connect_note | dm1 | dm2 | dm3
  body TEXT NOT NULL,
  status TEXT DEFAULT 'pending',   -- pending | approved | rejected | sent
  telegram_message_id INTEGER,
  drafted_at TEXT NOT NULL,
  decided_at TEXT
);

-- prospects gains:
ALTER TABLE prospects ADD COLUMN campaign_id INTEGER REFERENCES campaigns(id);
ALTER TABLE prospects ADD COLUMN disposition TEXT;
ALTER TABLE prospects ADD COLUMN last_dm_at TEXT;
ALTER TABLE prospects ADD COLUMN dm_count INTEGER DEFAULT 0;
ALTER TABLE prospects ADD COLUMN pitch_context TEXT;
```

**Automated test**: extend `tests/test_smoke.py` — assert all new tables/columns exist after `init_db()`. Existing tests must still pass.

**Manual test**: `sqlite3 data/outreach.db ".schema"` — eyeball the result.

**Done when**: smoke tests green, schema visible.

**Time**: 1 hour.

---

### Phase 2 — Drafter (Claude Code subagent + thin Python wrapper)

**Goal**: produce drafts for connect notes, DM1, DM2, DM3. No Anthropic API key needed.

**Files**:
- `.claude/agents/message-drafter.md` — subagent definition with system prompt encoding the no-spam-tells rules
- `linkedin_agent/drafter.py` — wrapper that builds a context payload and invokes `claude -p` headless, captures stdout

**Drafter prompt structure**:
- System: "You draft LinkedIn outreach messages. Hard rules: reference specific detail from prospect's post or role; never use spam tells ('I came across…', 'I noticed you…'); ≤300 chars for connect notes, 2-3 short paragraphs for DMs; no links in DM1; one clear ask or zero asks."
- User input: campaign brief markdown + prospect profile JSON + recent posts + prior thread (for DM2/DM3)
- Output: message body only, no preamble

**Automated test**:
- Unit test with a stubbed `claude -p` call (write a fake `claude` shim that returns canned text) — verify drafter.py builds the right context, parses output cleanly, rejects empty/error responses.
- Integration test marked `@pytest.mark.live_claude` — actually invokes `claude -p` against a fixture campaign + prospect. Inspects the draft for forbidden phrases.

**Manual test**: in interactive Claude Code, invoke the message-drafter subagent on prospect ID 1 across all four kinds. Read the outputs. Look for: spam tells, generic phrases, length compliance, accurate reference to the prospect's post.

**Done when**: 5 manually-reviewed drafts pass the smell test, automated tests green.

**Time**: 45 min build + 15 min manual review.

---

### Phase 3 — Campaign management

**Goal**: CRUD for campaigns, attach prospects to campaigns at search time.

**Files**:
- `linkedin_agent/cli.py` — add `campaign` subcommand group
- `campaigns/` directory — markdown files with YAML frontmatter

**New commands**:
```
linkedin campaign create <slug>          # scaffolds campaigns/<slug>.md
linkedin campaign list                   # table view
linkedin campaign show <slug>            # full brief
linkedin campaign assign <prospect> <slug>
linkedin search "<query>" --campaign <slug> --limit N
```

**Campaign brief template** (`campaigns/<slug>.md`):
```markdown
---
slug: ai-dev-pod
name: AI Dev Pod
status: active
target_icp: Series A-C SaaS founders, eng team 5-30, no ML team yet
---

# Pitch
[Your service offering, 3-5 lines]

# Pain points we address
- ...

# Proof points
- Case study: <name>
- Result: <metric>

# Tone
[How you want the drafter to sound: direct, consultative, etc.]
```

**Automated test**: smoke test — create campaign, list, assign prospect, search with `--campaign`. Verify FK populates.

**Manual test**: create a real campaign for one of your services. Read the brief Claude generates back when you run `linkedin campaign show`. Looks right? Good.

**Done when**: smoke tests green, one real campaign brief written and committed to repo.

**Time**: 30 min.

---

### Phase 4 — Telegram bot (the daily UI) *[LONG POLE]*

**Goal**: every outbound message gets approved/rejected via tap in Telegram. Replies notify in same chat.

**Files**:
- `linkedin_agent/telegram_bot.py` — long-running process using `python-telegram-bot` library
- `linkedin_agent/cli.py` — `telegram-setup` wizard, `bot-run` to start the daemon

**Bot capabilities**:
- On draft enqueue: post draft text + inline keyboard `[✅ Approve] [✏️ Edit] [❌ Reject]`. Store `telegram_message_id` in pending_drafts.
- Approve tap → callback → mark draft approved → trigger Unipile send → edit original message in chat: "✓ sent at HH:MM"
- Edit button → bot replies "Send me the new text". User replies with revised text. Bot updates draft body, re-shows with same buttons.
- Reject tap → mark rejected → edit original: "✗ rejected"
- New reply detected (Phase 5) → bot posts: "💬 New reply from <name> (<company>): <text>" + link to LinkedIn thread

**`telegram-setup` wizard**:
1. Prompts for bot token. Validates with `getMe`.
2. Prompts user to message the bot once (any text). Bot captures the chat ID from the first incoming message.
3. Writes both to `.env`.

**Automated test**: fake the Telegram API client. Test:
- Draft enqueued → API called with right payload
- Approve callback → triggers send via UnipileAdapter (mocked at fake level)
- Edit flow → captures new text into pending_drafts.body

**Manual test**:
1. Start bot: `python -m linkedin_agent bot-run`
2. From your phone, message the bot. Confirm `telegram-setup` wizard captures chat ID.
3. Manually enqueue a fake draft: `python -m linkedin_agent _debug_enqueue 1 dm1 "test draft body"`
4. Bot should push to Telegram with buttons. Tap Approve. With Unipile in dry-run mode, verify pending_drafts.status = 'approved' and an action row was logged.
5. Repeat with Edit: tap Edit, send replacement text, tap Approve. Verify body updated.
6. Repeat with Reject: tap Reject. Verify status = 'rejected', no Unipile call.

**Done when**: full Approve/Edit/Reject loop works from phone. Each path leaves DB in correct state.

**Time**: 2 hours build + 30 min manual testing.

---

### Phase 5 — Inbound polling

**Goal**: detect replies, halt sequences, notify in Telegram.

**Files**: `linkedin_agent/poll.py`, new CLI subcommand `linkedin poll`.

**Behavior**:
- Calls Unipile `GET /messages?since=<last_poll>` for threads where we have outbound history.
- For each new inbound message: write to `messages` table (direction=inbound), set prospect.status='replied'.
- Cancel any pending follow-up drafts for that prospect (mark rejected with reason="reply_received").
- Push notification to Telegram with text excerpt + LinkedIn link.

**Automated test**: fake adapter returns 2 inbound messages; verify DB writes, status flip, pending_drafts cancellation.

**Manual test**:
1. Configure with a secondary LinkedIn account.
2. From the secondary account, send a message to a prospect you've already "DM'd" via the primary.
3. Run `linkedin poll`. Verify:
   - DB has the inbound message
   - Status changed to 'replied'
   - Telegram received the notification
   - `linkedin status` shows the prospect in "replies needing attention"

**Done when**: end-to-end reply detection works in <15 min from receipt.

**Time**: 30 min.

---

### Phase 6 — Follow-up scheduler + auto-ghost

**Goal**: find prospects needing DM2 or DM3, draft via drafter, enqueue for approval. Auto-ghost stale ones.

**Files**: `linkedin_agent/followup.py`, `linkedin followup` subcommand.

**Logic**:
```python
# DM2 candidates: status='dm_sent', dm_count==1, now - last_dm_at >= 4 days, no reply
# DM3 candidates: status='dm_sent', dm_count==2, now - last_dm_at >= 11 days, no reply
# Auto-ghost: dm_count==3 AND now - last_dm_at >= 14 days AND status != 'replied'
```

**Automated test**: seed DB with prospects at various dm_count/last_dm_at states; verify only correct candidates get drafted; auto-ghost correctly applied.

**Manual test**:
1. Manually edit DB: set a test prospect to `dm_sent`, `dm_count=1`, `last_dm_at = now - 5 days`.
2. Run `linkedin followup`.
3. Verify a DM2 draft appears in Telegram.
4. Edit the DB: set `dm_count=3`, `last_dm_at = now - 15 days`. Run `linkedin followup`. Verify disposition='ghosted'.

**Done when**: timing logic correct, Telegram drafts appear with right content.

**Time**: 30 min.

---

### Phase 7 — Send-window enforcement

**Goal**: no Unipile sends outside 9am-5pm Mon-Fri local time. Approvals tapped outside window queue automatically.

**Files**: `linkedin_agent/send_window.py`.

**Implementation**: `is_send_window_open()` returns bool. Called inside the approval handler before calling Unipile. If closed, leave draft as 'approved' but don't send; daily cron picks up approved drafts during the window.

**Automated test**: freeze time at various points, assert behavior.

**Manual test**:
1. Set system clock or mock to Saturday 2pm.
2. Approve a draft via Telegram.
3. Verify: Unipile not called, draft remains `status='approved'`, Telegram replies "queued for next window".
4. Set clock to Monday 10am, run `linkedin send-approved`. Verify draft sends and status → 'sent'.

**Done when**: no sends ever fire outside window. Approved drafts wait correctly.

**Time**: 15 min.

---

### Phase 8 — Daily orchestration

**Goal**: one command does the entire daily cycle. Cron runs this hourly.

**Files**: `linkedin_agent/daily.py`, `linkedin daily` subcommand, update `scripts/daily_outreach.sh`.

**Sequence**:
1. `poll` — inbound message check
2. `react` — find targeted prospects with relevant recent posts, react (auto, no draft)
3. `connect` — find reacted prospects, draft connect note, enqueue
4. `dm1` — find connected prospects with no DM, draft DM1, enqueue
5. `followup` — find DM follow-up candidates, draft DM2/DM3, enqueue
6. `send-approved` — send any drafts approved during the window
7. `auto-ghost` — flip stale prospects

Each step respects daily caps. Each step logs to actions table. Each step is interruptible.

**Automated test**: smoke test the full chain against fake adapter. Verify proper sequencing, caps respected, no double-sends.

**Manual test**:
1. Set up a small campaign with 3 real prospects.
2. Cron OFF. Run `linkedin daily` manually at 10am Monday.
3. Verify each step's logs in `actions` table.
4. Verify Telegram received drafts.
5. Approve them on your phone.
6. Wait an hour, run `linkedin daily` again. Verify follow-up timing logic kicks in correctly.

**Done when**: a complete day's outreach runs end-to-end without intervention beyond Telegram taps.

**Time**: 30 min.

---

### Phase 9 — Status dashboard

**Goal**: one command, full picture.

**Files**: `linkedin_agent/cli.py:status`.

**Output**:
```
┌─────────────────────────────────────────────────────────────┐
│  Caps today      react 8/30 · connect 4/20 · dm 2/10        │
│  Window status   OPEN (closes 5:00 PM, 3h 12m remaining)    │
├─────────────────────────────────────────────────────────────┤
│  Pipeline by stage                                          │
│    targeted          12                                     │
│    reacted            6                                     │
│    connection_sent    4                                     │
│    connected          3                                     │
│    dm_sent            5                                     │
│    replied            2  ⚠ NEEDS ATTENTION                  │
├─────────────────────────────────────────────────────────────┤
│  Pending approvals     3 in Telegram                        │
│  Today's follow-ups    1 due (Alex Chen, DM2)               │
│                                                             │
│  Recent replies:                                            │
│    • Sarah Liu (Beta Co) — 2h ago                           │
│       "Hey, interested but slammed this week..."            │
│    • Marcus Reed (Gamma) — 8h ago                           │
│       "What's the typical engagement length?"               │
└─────────────────────────────────────────────────────────────┘
```

**Manual test**: cosmetic — does it read well at a glance? Iterate.

**Time**: 30 min.

---

### Phase 10 — Setup script

**Goal**: someone clones the repo → 10 min later they're operational.

**Files**: `setup.sh` (replaces ad-hoc README steps).

**Steps**:
1. Check Python 3.11+. If not, point to install.
2. Create venv, install deps.
3. `playwright install chromium` (kept as fallback adapter).
4. `linkedin init` — create DB.
5. Prompt for Unipile creds, write to `.env`.
6. Run `pytest -m live` to validate Unipile.
7. Prompt user to message bot for `telegram-setup`.
8. Test Telegram bot.
9. Install cron entry.
10. Print "Run `linkedin status` to verify."

**Manual test**: nuke the repo, clone fresh, run `./setup.sh`, verify 10-min target.

**Time**: 30 min.

---

### Phase 11 — CLAUDE.md update

**Goal**: rewrite playbook for agency lead-gen (current version is generic).

**Files**: `CLAUDE.md`.

**Changes**:
- Campaign-first workflow
- Reference the drafter subagent for personalized writing
- Document the Telegram approval flow (so Claude knows drafts go there, not to it)
- Update example prompts ("draft a DM2 for prospect 5", "what replies need attention?")

**Time**: 15 min.

---

### Phase 12 — Extended smoke tests

**Goal**: smoke test covers everything we added (campaigns, drafts, follow-ups, dispositions).

**Files**: `tests/test_smoke.py`.

**New tests**:
- Campaign create/list/assign
- Drafter wrapper (stubbed claude -p)
- Pending drafts state machine (pending → approved → sent)
- Follow-up scheduler date math
- Auto-ghost trigger
- Status dashboard renders without error

**Time**: 30 min.

---

## 4. Testing strategy summary

Three layers:

### Layer 1: Offline smoke tests (run on every change)
- `pytest` — no network, no LinkedIn, no Telegram. Fake adapter + stubbed external calls.
- Covers: DB transitions, CLI subcommand wiring, draft state machine, rate-limit gates, date math.
- Target: <5 sec to run.
- **Catches**: regressions in logic, schema mistakes, CLI breakage.

### Layer 2: Live integration tests (run on demand)
- `pytest -m live` — hits real Unipile API and your real LinkedIn account.
- Covers: adapter endpoint paths, real API responses, real status changes on LinkedIn.
- Costs: a few API calls per run.
- **Catches**: Unipile endpoint drift, account-level issues (caps hit, account flagged).
- Marked opt-in so casual `pytest` runs don't burn API calls.

### Layer 3: Manual verification (after live actions)
- **Playwright sessions to verify LinkedIn-side state**:
  - After a `react` API call: navigate to the post, find reactions list, verify your name is in it.
  - After a `connect` API call: navigate to the prospect's profile, verify "Pending" badge.
  - After a `dm` API call: navigate to messages, verify the DM appears in the thread.
- **Phone-based Telegram UX testing**:
  - Receive a draft on your phone. Tap Approve. Verify message sends within seconds.
  - Edit flow: tap Edit, reply with new text, approve.
  - Reject flow.
- **Reply flow end-to-end**:
  - From a second LinkedIn account, message a prospect.
  - Verify within 15 min: Telegram notification, status updated, sequence halted.

This three-layer setup gives confidence at the cost-appropriate level. Smoke tests run constantly; live tests run a few times during dev + before each release; manual checks happen during phase manual-test steps and again whenever you change adapter-touching code.

---

## 5. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Unipile endpoint paths drift from our placeholders | Medium | Phase 0 blocked | Live test catches it; ~30 min to fix paths |
| LinkedIn flags account despite caps | Low-Medium | Account restricted | Conservative caps (already in `.env`); business-hours window (Phase 7); secondary account for dev |
| Drafter writes spam-sounding messages | Medium | Reply rate tanks | Phase 2 manual review of 5+ drafts; iterate prompt; subagent prompt is short and editable |
| Telegram bot loses connection during long idle | Medium | Approvals don't reach you | python-telegram-bot auto-reconnects; supervisor (launchd/systemd) restarts on crash; Phase 10 setup script configures this |
| Cron runs over budget (caps exceeded mid-day) | Low | Some actions skipped | Hard cap in `safety.py` is fatal; daily run logs which actions were skipped |
| Polling misses a reply | Low | Late response | Polling every 15 min is sufficient for B2B context; if reply lands at 4:59pm, you see it by 5:14pm |
| Telegram rate limits (30 msgs/sec/bot) | Very Low | Bot temporarily blocked | Far below the limit at our send volumes |
| Multi-touch DMs send to ghosted prospects | Low | Wasted touches, mild spam | Phase 6 auto-ghost; Phase 5 reply detection halts sequences |
| Secondary disposition states needed later | Medium | Schema change | `disposition` is TEXT — extensible, no migration |

---

## 6. Execution order with timing

```
Day 1 (parallel work):
  You: sign up for Unipile, message @BotFather for Telegram bot
       (~22 min of human time)
  Me:  Phase 1 (schema) + Phase 2 (drafter) + Phase 3 (campaigns)
       + Phase 7 (send window) + Phase 11 (CLAUDE.md)
       (~3.5 hours of focused build)

Day 1 evening (with your creds):
  Together: Phase 0 (Unipile live test) — fix any endpoint mismatches
            Phase 12 (smoke test extensions)

Day 2:
  Me:  Phase 4 (Telegram bot — the long pole, 2.5h)
       + Phase 5 (polling, 30m)
       + Phase 6 (follow-ups, 30m)
  You: manual testing on phone

Day 3:
  Me:  Phase 8 (daily orchestration)
       + Phase 9 (status)
       + Phase 10 (setup script)
  You: end-to-end manual test with 3-5 real prospects
```

Total: 2-3 days. After Day 3, the system is in production for solo use.

---

## 7. What "done" looks like

A new teammate can:
1. Clone the repo
2. Run `./setup.sh`
3. Answer prompts (Unipile creds, Telegram bot)
4. 10 minutes later, run `linkedin campaign create my-first-campaign`
5. Edit the brief markdown
6. Run `linkedin search "fintech founder NYC" --campaign my-first-campaign --limit 5`
7. Add cron entry
8. Receive their first draft in Telegram within an hour
9. Tap approve, watch the message land on LinkedIn

If that flow works, we're done.

---

## Appendix A: File map

```
linkedin_outreach/
├── docs/PLAN.md                              # this file
├── CLAUDE.md                                 # updated for agency use
├── README.md                                 # links to docs/
├── setup.sh                                  # NEW (Phase 10)
├── .env.example                              # updated for Telegram
├── pyproject.toml
├── linkedin_agent/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py                                # adds campaign/poll/followup/daily/status/bot-run
│   ├── config.py
│   ├── db.py                                 # Phase 1 schema changes
│   ├── safety.py
│   ├── drafter.py                            # NEW (Phase 2)
│   ├── poll.py                               # NEW (Phase 5)
│   ├── followup.py                           # NEW (Phase 6)
│   ├── send_window.py                        # NEW (Phase 7)
│   ├── daily.py                              # NEW (Phase 8)
│   ├── telegram_bot.py                       # NEW (Phase 4)
│   └── adapters/
│       ├── base.py
│       ├── playwright_adapter.py             # kept as fallback
│       ├── unipile_adapter.py                # endpoint paths verified Phase 0
│       └── fake_adapter.py
├── campaigns/                                # NEW (Phase 3)
│   ├── ai-dev-pod.md
│   └── rails-rescue.md
├── .claude/
│   ├── agents/
│   │   └── message-drafter.md                # NEW (Phase 2)
│   ├── skills/
│   │   └── linkedin-outreach/SKILL.md        # updated
│   └── settings.json                         # NEW (hooks from earlier recommendation)
├── scripts/
│   └── daily_outreach.sh                     # updated (calls linkedin daily)
├── tests/
│   ├── test_smoke.py                         # extended (Phase 12)
│   └── test_unipile_live.py                  # NEW (Phase 0)
└── data/outreach.db                          # gitignored
```

## Appendix B: Commands cheat sheet (post-build)

```bash
# Setup
./setup.sh

# Campaigns
linkedin campaign create ai-dev-pod
linkedin campaign list
linkedin campaign show ai-dev-pod

# Discovery
linkedin search "Series A SaaS founder" --campaign ai-dev-pod --limit 10

# Daily ops (usually via cron)
linkedin daily          # full cycle
linkedin status         # dashboard
linkedin poll           # check inbound only
linkedin followup       # send follow-ups only

# Telegram
linkedin telegram-setup # one-time wizard
linkedin bot-run        # start the bot daemon

# Maintenance
linkedin caps           # rate limit usage
pytest                  # offline smoke
pytest -m live          # live integration
```
