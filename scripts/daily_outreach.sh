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

# Cron's default PATH is /usr/bin:/bin (or similar minimal). The drafter
# shells out to `claude -p` which is typically at ~/.local/bin/claude (npm
# global). Other deps may live in /opt/homebrew/bin. Make sure both are
# findable regardless of who launches us.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

# `claude -p` resolves its auth state via the macOS keychain, which needs
# USER (and a logged-in user session) to be available. macOS cron passes
# USER naturally, but launchd and some sandboxes don't — fall back to
# whoami so the script works under any restricted env.
export USER="${USER:-$(whoami)}"
export LOGNAME="${LOGNAME:-$USER}"

exec .venv/bin/python -m linkedin_agent daily
