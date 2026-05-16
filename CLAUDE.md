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

1. **Writing a campaign brief.** The user iterates with you on `campaigns/<slug>.md`. Help them name pain points specifically (not generically), pick proof points that match the ICP, and set tone.
2. **Replying to an interested prospect.** When a reply lands, the user often pastes the inbound text into Claude Code and asks for a thoughtful response. Read the prior thread (in `messages` table) for context.
3. **Tuning the drafter prompt.** If drafts feel off, iterate on `.claude/agents/message-drafter.md`. Edit and immediately rerun `linkedin daily` to see the new style.
4. **One-off prospect work.** "Draft a custom DM2 for prospect 5 — they replied with a question about pricing." Use the message-drafter subagent directly.

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
