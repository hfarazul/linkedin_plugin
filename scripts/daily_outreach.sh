#!/usr/bin/env bash
# Cron entry point — runs the full daily cycle (poll → react → connect → DM
# drafts → follow-ups → flush approved-but-not-sent). All decision-making
# happens in Python; Claude Code is only invoked for individual `claude -p`
# drafter calls during message drafting.
#
# Example crontab (hourly Mon-Fri 9am-5pm local):
#
#   0 9-16 * * 1-5 /Users/haquefarazul/Work/Linkedin_outreach/scripts/daily_outreach.sh >> /tmp/linkedin_outreach.log 2>&1
#
# Requires: .env populated, DB initialized, Telegram bot daemon running
# (separately under launchd/systemd or tmux) so approval taps land.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [ ! -f .env ]; then
    echo "[$(date)] .env missing — aborting"
    exit 1
fi

exec .venv/bin/python -m linkedin_agent daily
