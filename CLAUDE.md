# LinkedIn Outreach Agent

This project is a CLI-driven LinkedIn outreach agent. **You (Claude Code) are the agent.** The Python package `linkedin_agent` is the toolkit you call.

## How to operate

Always work through the CLI rather than writing one-off scripts. The CLI handles rate limits, the action log, dry-run mode, and status transitions for you.

```
python -m linkedin_agent <subcommand> [args]
```

Subcommands:

| Command | Purpose |
|---|---|
| `init` | Create the SQLite DB. Run once. |
| `auth` | Interactive LinkedIn login (Playwright backend only). |
| `search "<query>" --limit N` | Search LinkedIn and import N prospects. |
| `pipeline [--status STATUS]` | Show prospects, optionally filtered. |
| `posts <prospect_id>` | Show a prospect's recent posts. |
| `react <prospect_id> [--reaction LIKE]` | React to their latest post → status `reacted`. |
| `connect <prospect_id> [--note "…"]` | Send a connection request → `connection_sent`. |
| `dm <prospect_id> "<body>"` | Send a DM → `dm_sent`. |
| `caps` | Show today's usage vs. daily caps. |

## Pipeline stages

```
targeted → reacted → connection_sent → connected → dm_sent → replied
```

`connected` and `replied` are set manually for now (LinkedIn doesn't reliably push these to us). Check `pipeline --status connection_sent` periodically and promote prospects you see have accepted.

## Standard outreach playbook

When the user says "do today's outreach" or similar, follow this sequence:

1. `caps` — confirm we have budget left for the day.
2. `pipeline --status targeted` — list prospects awaiting first touch.
3. For each `targeted` prospect, in order, up to the remaining reaction cap:
   - `posts <id> --limit 1` to see what they posted.
   - If recent post is relevant to their work (not a generic share), `react <id>`.
   - If no recent posts, skip (don't connect cold without a reason).
4. `pipeline --status reacted` — prospects we've warmed up.
5. For each, send `connect <id> --note "<personalized 1-line note>"`. The note must reference their post or role; never use a generic template.
6. `pipeline --status connected` — accepted invites.
7. For each, send a `dm` that opens with the connection context (their post, mutual interest), not a pitch.

## Personalization rules

- Notes and DMs must reference something specific: a post, a project, their company's recent news.
- Never write "I came across your profile" or any variant of "I noticed you…". These are spam tells.
- Keep notes ≤ 300 chars (LinkedIn limit). Aim for 200.
- DMs: 2-3 short paragraphs max. No links in the first message.

## Safety rules — read these before acting

1. **Always run `caps` before a batch action.** If a cap is at or near the limit, stop.
2. **Never bypass the CLI.** Don't call adapter methods directly — they don't enforce rate limits.
3. **Dry-run first when unsure.** Set `DRY_RUN=1` in `.env` for a session if you're testing.
4. **Stop immediately if anything looks off** — captcha screenshots, "unusual activity" messages, blank search results from Playwright. Tell the user and wait.
5. **Don't add new prospects faster than you can work them.** Search ≤ 20/day; the funnel narrows.

## Headless mode (scheduled runs)

`scripts/daily_outreach.sh` is the cron entry point. It invokes Claude Code headless with a fixed prompt that runs through the standard playbook above. The agent (you) operates with no human in the loop for those runs — be extra conservative.

## State

- DB: `data/outreach.db` (SQLite). Inspect with `sqlite3 data/outreach.db` if needed.
- Playwright session: `playwright_state/state.json` (gitignored).
- Action log: `actions` table, queryable for audit.
