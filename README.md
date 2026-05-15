# LinkedIn Outreach Agent

A CLI-driven LinkedIn outreach agent. **Claude Code itself is the agent** — it drives the `linkedin_agent` Python CLI which handles search, reactions, connections, DMs, rate limiting, and state.

Two backends ship with the same interface:
- **Playwright** — free, runs a real browser session. Against LinkedIn's ToS; use a secondary account.
- **Unipile** — paid (~$60/mo), production-grade. Drop-in swap via `LINKEDIN_BACKEND=unipile`.

## Setup

```bash
# 1. Install
cd /Users/haquefarazul/Work/Linkedin_outreach
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium  # only if using the Playwright backend

# 2. Configure
cp .env.example .env
# edit .env to set caps and (optionally) Unipile creds

# 3. Init the DB
python -m linkedin_agent init

# 4. Auth (Playwright only) — opens a browser; log in once
python -m linkedin_agent auth
```

## Daily use

Open Claude Code in this directory and talk to it:

> "Find 10 fintech founders in NYC, then react to their latest posts."
> "Show me the pipeline. Who's ready for a connection request?"
> "Do today's outreach."

Claude reads `CLAUDE.md` for the playbook and drives the CLI:

```bash
python -m linkedin_agent search "fintech founder NYC" --limit 10
python -m linkedin_agent pipeline --status targeted
python -m linkedin_agent posts 1
python -m linkedin_agent react 1
python -m linkedin_agent connect 1 --note "Loved your post on rails for B2B payments…"
python -m linkedin_agent caps
```

`DRY_RUN=1` in `.env` makes every action log-only so you can test the playbook without touching LinkedIn.

## Scheduled runs

`scripts/daily_outreach.sh` calls `claude -p` in headless mode. Add it to cron:

```cron
30 9 * * 1-5 /Users/haquefarazul/Work/Linkedin_outreach/scripts/daily_outreach.sh >> /tmp/linkedin_outreach.log 2>&1
```

The headless run uses the same `CLAUDE.md` playbook but with stricter, fixed limits (see the prompt in the script).

## Switching to Unipile

1. Sign up at unipile.com, connect your LinkedIn account.
2. Set in `.env`: `LINKEDIN_BACKEND=unipile`, `UNIPILE_API_KEY=…`, `UNIPILE_ACCOUNT_ID=…`, `UNIPILE_DSN=…`.
3. That's it. The CLI and CLAUDE.md don't change.

## Safety

Hard caps live in `.env` and are checked before every action via `linkedin_agent/safety.py`. Defaults: 30 reactions/day, 20 connections/day, 10 DMs/day, 50 searches/day. These are deliberately conservative — LinkedIn flags accounts that exceed ~50 connections/day.

Every action (real or dry-run) is logged to the `actions` table. Audit with:

```bash
sqlite3 data/outreach.db "SELECT created_at, kind, prospect_id, dry_run FROM actions ORDER BY id DESC LIMIT 20;"
```

## Project layout

```
linkedin_agent/
  config.py           env + caps
  db.py               SQLite schema + helpers
  safety.py           rate limiter + delays
  cli.py              click subcommands
  adapters/
    base.py           LinkedInAdapter ABC + dataclasses
    playwright_adapter.py
    unipile_adapter.py
.claude/skills/linkedin-outreach/SKILL.md   # /linkedin-outreach slash command
scripts/daily_outreach.sh                   # cron entry point
CLAUDE.md                                   # Claude Code's playbook
```

## Caveats

- **Playwright selectors are fragile.** LinkedIn changes their DOM frequently. If a command starts returning 0 results or hangs, the selectors in `playwright_adapter.py` need updating. The Unipile backend is more stable because their service tracks LinkedIn's UI changes for you.
- **Account risk is real.** Even with conservative caps, sustained automation can trigger restrictions. Don't use your primary account during development.
- **`connected` and `replied` are manual statuses for now.** LinkedIn doesn't expose a clean "they accepted!" signal to either backend. Check the pipeline weekly and promote prospects you see in your inbox.
