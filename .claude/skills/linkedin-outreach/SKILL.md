---
name: linkedin-outreach
description: Run a LinkedIn outreach session — search prospects, react to their posts, send connection requests, and follow up with DMs. Use when the user says "do outreach", "find prospects in <space>", "follow up on connections", or invokes /linkedin-outreach. All actions flow through `python -m linkedin_agent` and respect daily safety caps.
---

# LinkedIn outreach skill

Use the CLI documented in `CLAUDE.md` at the project root. Do **not** call adapter Python directly — go through the `python -m linkedin_agent` subcommands so rate limits and the action log stay correct.

## Quick reference

- New prospects: `python -m linkedin_agent search "<icp query>" --limit N`
- Pipeline view: `python -m linkedin_agent pipeline [--status STATUS]`
- Warm up: `python -m linkedin_agent posts <id>` then `... react <id>`
- Connect: `python -m linkedin_agent connect <id> --note "..."`
- DM: `python -m linkedin_agent dm <id> "..."`
- Budget check: `python -m linkedin_agent caps`

## Decision flow

1. Run `caps` first. If any cap is at limit, surface that and stop.
2. Pick the right stage to work based on what the user asked:
   - "find prospects" → `search` then stop.
   - "warm up" / "react to posts" → iterate `targeted` prospects, react where relevant.
   - "send connections" → iterate `reacted` prospects.
   - "follow up" → iterate `connected` prospects with a DM.
   - "do outreach" with no specifier → run the full playbook in CLAUDE.md.
3. Always personalize notes and DMs based on `posts` output. Never use a template.
4. Report a short summary at the end: actions taken, current caps usage.
