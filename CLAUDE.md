# LinkedIn Outreach Agent — Software Agency Lead-Gen

This project runs LinkedIn outreach for a software agency. **You (Claude Code) are the agent.** The Python package `linkedin_agent` is the toolkit you drive.

Day-to-day, most operations are automated by cron. You're invoked when there's *judgment* to apply: writing a campaign brief, drafting a custom message, replying to an interested prospect.

## The system in 30 seconds

```
campaign brief (markdown)
        │
        ▼
hourly cron (`linkedin daily`)
        │  - syncs campaigns from markdown files
        │  - polls Unipile for inbound replies
        │  - reacts to targeted prospects' recent posts
        │  - drafts connect notes / DM1 / DM2 / DM3 via the message-drafter subagent
        ▼
Telegram bot (drafts post with Approve/Edit/Reject buttons)
        │
        ▼  user taps on phone
Unipile API → LinkedIn
```

Drafts approved outside business hours stay queued (`status='approved'`) until the next 9-5 Mon-Fri window.

## Campaign-first workflow

Every prospect belongs to a campaign. Campaigns are markdown files under `campaigns/`:

```
campaigns/
├── ai-dev-pod.md       # AI engineering pod offering, Series A-C SaaS founders
├── rails-rescue.md     # Rails performance/refactor for established teams
```

Each file has YAML frontmatter (slug, name, status, target_icp) plus a markdown body with the pitch, pain points, proof points, and desired tone. The drafter reads the file when generating messages, so editing the brief immediately changes the drafted output.

To create one: `linkedin campaign create <slug>` scaffolds the file; edit it; `linkedin campaign sync` (or any `daily` run) refreshes the DB.

## Pipeline stages

```
targeted → reacted → connection_sent → connected → dm_sent → replied
```

Plus a `disposition` column (set after conversation begins):
`interested · not_fit · ghosted · won · lost · deferred`

`ghosted` auto-applies 14 days after DM3 with no reply. The others are manual flags the user sets after talking to the prospect.

## Daily ops — what the cron does

| Step | What | Approval needed |
|---|---|---|
| `campaigns sync` | Refresh DB from markdown files | — |
| `poll` | Fetch inbound replies, halt sequences, notify Telegram | — |
| `react` | Like recent post of each `targeted` prospect → `reacted` | No (low stakes) |
| `connect` | Draft connect note for each `reacted` prospect | **Yes (Telegram)** |
| `dm1` | Draft first DM for each `connected` prospect with `dm_count=0` | **Yes (Telegram)** |
| `followup` | Draft DM2 (after 4d) / DM3 (after 11d) for `dm_sent` prospects | **Yes (Telegram)** |
| `send-approved` | Flush drafts approved outside the business-hours window | — |
| `auto-ghost` | Mark stale `dm_count=3` prospects as `ghosted` | — |

You as the agency owner only see Telegram approval cards on your phone. Tap to approve, swipe to reject, tap Edit + reply to rewrite.

## When Claude Code is needed (interactive)

The cron handles everything *mechanical*. Claude Code is for the parts that benefit from judgment:

1. **Creating a new campaign** — follow the protocol in the next section. Don't just `campaign create` and let the user write a brief in the dark.
2. **Replying to an interested prospect.** When a reply lands, the user often pastes the inbound text into Claude Code and asks for a thoughtful response. Read the prior thread (in `messages` table) for context.
3. **Tuning the drafter prompt.** If drafts feel off, iterate on `.claude/agents/message-drafter.md`. Edit and immediately rerun `linkedin daily` to see the new style.
4. **One-off prospect work.** "Draft a custom DM2 for prospect 5 — they replied with a question about pricing." Use the message-drafter subagent directly.

## Campaign creation protocol — follow this every time

When the user says "let's create a new campaign" / "I want to target X" / similar:

### Phase 1 — Clarifying questions (don't skip, ask all 8)

Don't accept a one-line ICP. Push for specificity on each:

1. **Who, specifically?** Role + company stage + size. ("Founders" is too broad; "non-tech founders of pre-seed B2B SaaS, under $1M ARR" is workable.)
2. **Where?** Country/region/cities. (Default to US/Europe unless told otherwise — see Unipile proxy notes in the doc.)
3. **What pain?** 2-3 specific points the prospect would recognize.
4. **Why now?** What trigger/timing makes them open to outreach this quarter?
5. **What Cortivo angle?** Mutual connections? Shared school (IIT)? Specific vertical we've shipped in (fintech via Mastercard/Bespoke, retail via Coca-Cola, etc.)?
6. **Anti-claims?** What this campaign explicitly avoids saying. (E.g., "don't pitch as cheap" / "don't reference Upwork to VC-track founders.")
7. **Tone?** Financial/operational? Peer-to-peer? Consultative? Technical?
8. **Search queries?** What 2-3 LinkedIn classic-search keyword strings would surface this ICP? Remember classic search only does keyword matching — phrases like "we just raised" return investors talking about deals.

### Phase 2 — Search validation (mandatory gate)

For each candidate query (≥1, ideally 2-3):

```
linkedin validate-query "<query>" --limit 10 --campaign <slug-once-created>
```

This grades each result on geography + role keywords + noise exclusion. The CLI exits 0 if keepers ≥ 6/10, exits 1 with the table of issues otherwise.

- **All queries pass**: proceed to Phase 3.
- **All queries fail**: iterate on the search terms with the user. Common fixes — add a specific city ("non-technical founder Boston"), add a stage qualifier ("seed-stage founder"), drop a phrase that matches investor vocabulary.
- **Mixed**: use only the queries that pass.

### Phase 3 — Generate the brief

Once at least one query passes validation, write `campaigns/<slug>.md` synthesizing the answers from Phase 1. Use the structure in `campaigns/_cortivo.md` as the canon. Per-campaign frontmatter overrides are optional:

```yaml
---
slug: ...
name: ...
status: active
target_icp: <long descriptive sentence>
# Optional ICP heuristic overrides for validate-query:
icp_role_required: "founder|ceo|owner"
icp_role_excluded: "investor|vc|venture|coach"
icp_geo_required: "United States|, CA\\b|United Kingdom"
---
```

Then `linkedin campaign sync` and show the user the rendered brief.

### Phase 4 — First import is small

Don't import 50 prospects at once. **Import 5-10 first**, eyeball them in `linkedin pipeline --status targeted`, and only scale up after a couple have moved through to `connected`. This catches campaign mismatches early.

### What to push back on

If the user gives vague answers, ask follow-ups. Specifically:
- "Founders" → push for stage + vertical
- "AI companies" → push for buyer profile (founder? CTO? Head of Product?)
- "Tech founders" → push for non-technical vs technical (huge ICP-fit signal)

A campaign with vague positioning produces drafts that read templated. The whole point of the protocol is to surface specificity that the drafter can latch onto.

## CLI reference

All commands are `python -m linkedin_agent <subcommand>` (or `linkedin <subcommand>` if the venv is activated).

### Day-to-day
| Command | Purpose |
|---|---|
| `status` | One-shot dashboard: caps, window status, pipeline by stage, replies, due follow-ups |
| `daily` | Run the full cron cycle once |
| `caps` | Usage vs. daily caps |
| `poll` | Fetch inbound replies only |
| `pipeline [--status STATUS]` | List prospects |

### Campaigns
| Command | Purpose |
|---|---|
| `campaign create <slug>` | Scaffold a new campaign markdown file + DB row |
| `campaign sync` | Re-read all `campaigns/*.md` into the DB |
| `campaign list` | All campaigns |
| `campaign show <slug>` | Print the full brief |
| `campaign archive <slug>` | Mark archived (no new work) |
| `campaign assign <prospect_id> <slug>` | Attach a prospect to a campaign |

### Discovery + manual outreach
| Command | Purpose |
|---|---|
| `search "<query>" --campaign <slug> --limit N` | Search LinkedIn, import N prospects into the campaign |
| `posts <prospect_id>` | Recent posts |
| `react <prospect_id>` | React manually |
| `connect <prospect_id> --note "..."` | Send connection request manually |
| `dm <prospect_id> "<body>"` | Send DM manually |

### Telegram + bot daemon
| Command | Purpose |
|---|---|
| `bot-run` | Start the Telegram daemon (long-running; runs alongside cron) |
| `telegram-test` | Sanity check |
| `telegram-push-draft <draft_id>` | Manually re-push a draft to Telegram |
| `_debug-enqueue <pid> <kind> "<body>" [--no-push]` | Enqueue without invoking the drafter (testing) |

### Send-window
| Command | Purpose |
|---|---|
| `send-approved [--force]` | Flush any `approved` drafts that were queued outside the window |

## Safety rules

1. **Never bypass the CLI.** Adapter methods don't enforce rate limits. Always use `python -m linkedin_agent <subcommand>`.
2. **Always check `caps` or `status` before bulk actions.** Hard caps (default 30 reactions / 20 connections / 10 DMs / 50 searches per 24h) will raise.
3. **Don't push drafts to LinkedIn that haven't been approved in Telegram.** The approval flow is the human-in-the-loop quality check.
4. **Stop if anything looks off.** Captcha screens, "unusual activity" warnings, or unexpected 4xx responses from Unipile → tell the user, don't retry.

## State

- DB: `data/outreach.db` (SQLite). Inspect with `sqlite3 data/outreach.db`.
- Campaign briefs: `campaigns/*.md`. Source of truth — DB rows are derived.
- Telegram session: lives in your phone; chat_id captured at setup.
- Unipile session: managed by Unipile (cookie-based or browser-based on their side).
- Action log: `actions` table — every API call, drafter result, status transition.

## Headless mode

`scripts/daily_outreach.sh` is the cron entry: `0 9-16 * * 1-5 .../daily_outreach.sh >> /tmp/log 2>&1`. It calls `linkedin daily` directly — no `claude -p` involved at the cron level. The drafter subagent is the only place Claude Code is invoked, and only during drafting.

## Deployment — dedicated Mac (always-on host)

For 24/7 operation (so the cron fires and Telegram approvals process anytime), the recommended setup is a dedicated Mac (laptop or mini) acting as the always-on host. Steps:

1. **Clone the repo** to `~/Work/Linkedin_outreach` on the host Mac.
2. **Run setup.sh** to create the venv, install deps, pull `playwright` Chromium, initialize the DB, and copy `.env` (Unipile + Telegram creds).
3. **Install Claude Code on the host** and run `claude /login` once — the drafter invokes `claude -p` and needs the host machine authenticated.
4. **Install the bot daemon as a LaunchAgent**:
   ```
   ./scripts/install_launchd.sh
   ```
   This puts a plist under `~/Library/LaunchAgents/com.cortivo.linkedin-bot.plist` that:
   - Starts the daemon on login
   - Auto-restarts within 10s if it crashes
   - Logs to `data/bot-daemon.{out,err}.log`
5. **Install the daily cron** (replace path):
   ```
   crontab -e
   # add:
   0 9-16 * * 1-5 /Users/<you>/Work/Linkedin_outreach/scripts/daily_outreach.sh >> /tmp/linkedin_outreach.log 2>&1
   ```
6. **Prevent sleep during work hours**. macOS will suspend the daemon when the lid closes (laptop) or the system sleeps. Options:
   - **System Settings → Lock Screen → "Prevent automatic sleeping when display is off"** (clamshell laptops)
   - Or run `caffeinate -d &` in a startup item
   - For a Mac mini: just set `Energy → Prevent automatic sleeping` and you're done
7. **Verify**:
   - `launchctl list | grep linkedin-bot` → shows the running daemon's PID
   - `tail -f data/bot-daemon.out.log` → watch its activity in real time
   - Open Telegram, message the bot, confirm taps still process

### Daemon lifecycle commands
- Stop:    `launchctl unload ~/Library/LaunchAgents/com.cortivo.linkedin-bot.plist`
- Start:   `launchctl load ~/Library/LaunchAgents/com.cortivo.linkedin-bot.plist`
- Restart: unload + load (or re-run `./scripts/install_launchd.sh` — it's idempotent)
- Logs:    `data/bot-daemon.out.log` and `data/bot-daemon.err.log`

### Migrating between Macs

The only state to copy is:
- `data/outreach.db` — SQLite, scp it
- `.env` — secrets, scp it
- `campaigns/*.md` — campaign briefs, already in git

Re-run `./scripts/install_launchd.sh` on the new Mac and you're back online.
