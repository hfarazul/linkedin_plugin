#!/usr/bin/env bash
# Cron entry point. Runs Claude Code in headless mode against this project's
# CLAUDE.md instructions. Example crontab line (weekdays at 09:30 local):
#
#   30 9 * * 1-5 /Users/haquefarazul/Work/Linkedin_outreach/scripts/daily_outreach.sh >> /tmp/linkedin_outreach.log 2>&1
#
# Requires: claude CLI on PATH, project .env populated, DB initialized.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [ ! -f .env ]; then
    echo "[$(date)] .env missing — aborting"
    exit 1
fi

PROMPT='Run today'\''s LinkedIn outreach following the playbook in CLAUDE.md.

Budget: stay well under all daily caps. If any cap is at 80%+, stop and report.

Sequence:
1. Run `caps` and report current usage.
2. If targeted-prospect bucket is empty, do not search for more today — work what we have.
3. Warm up step: for up to 5 `targeted` prospects with relevant recent posts, react.
4. Connect step: for up to 5 `reacted` prospects, send a personalized connection request (note must reference the post you reacted to).
5. Follow-up step: for up to 3 `connected` prospects, send a DM that opens with the connection context, not a pitch.
6. End with a one-paragraph summary: counts per action, anything skipped, anything that looked off.

If you see captcha, unusual-activity warnings, or selectors failing, stop immediately and report.'

exec claude -p "$PROMPT" --output-format text
