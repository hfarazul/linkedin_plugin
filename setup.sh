#!/usr/bin/env bash
# Setup script — fresh clone → operational in ~10 minutes.
#
# Steps:
#   1. Check Python 3.11+
#   2. Create venv, install deps
#   3. Install Playwright Chromium (fallback adapter)
#   4. Initialize SQLite DB
#   5. Prompt for Unipile creds (or keep existing .env)
#   6. Optionally validate Unipile auth (`pytest -m live`)
#   7. Prompt for Telegram bot token + auto-extract chat_id via getUpdates
#   8. Offer to install crontab line
#
# Non-interactive mode for tests:
#   LINKEDIN_SETUP_NONINTERACTIVE=1  — skip every prompt; assume .env already populated
#
# Usage:
#   ./setup.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# --- helpers ---------------------------------------------------------------

bold()   { printf "\033[1m%s\033[0m\n" "$1"; }
green()  { printf "\033[32m%s\033[0m\n" "$1"; }
yellow() { printf "\033[33m%s\033[0m\n" "$1"; }
red()    { printf "\033[31m%s\033[0m\n" "$1"; }

NONINTERACTIVE="${LINKEDIN_SETUP_NONINTERACTIVE:-}"

ask() {
    # Echo prompt, read value into the named global. In non-interactive mode
    # accept the default (passed as second arg) without prompting.
    local var="$1"
    local prompt="$2"
    local default="${3:-}"
    if [ -n "$NONINTERACTIVE" ]; then
        printf -v "$var" '%s' "$default"
        return
    fi
    if [ -n "$default" ]; then
        read -r -p "$prompt [$default]: " value
        value="${value:-$default}"
    else
        read -r -p "$prompt: " value
    fi
    printf -v "$var" '%s' "$value"
}

confirm() {
    # Y/n prompt — defaults to yes. In non-interactive mode always true.
    local prompt="$1"
    if [ -n "$NONINTERACTIVE" ]; then
        return 0
    fi
    read -r -p "$prompt [Y/n]: " reply
    case "$reply" in
        [Nn]*) return 1 ;;
        *)     return 0 ;;
    esac
}

# --- 1. Python version check ------------------------------------------------

bold "Step 1/7  Checking Python version"
# Prefer the newest available interpreter; the system `python3` on many Macs
# is still 3.9, but the project requires 3.11+.
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        ver="$("$candidate" -c 'import sys; print("{}.{}".format(sys.version_info.major, sys.version_info.minor))')"
        major="${ver%%.*}"
        minor="${ver##*.}"
        if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON_BIN="$(command -v "$candidate")"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    red "No Python 3.11+ found on PATH. Install via Homebrew: brew install python@3.12"
    exit 1
fi
green "✓ Using $($PYTHON_BIN --version) at $PYTHON_BIN"

# --- 2. venv + deps ---------------------------------------------------------

bold "Step 2/7  Creating venv and installing deps"
if [ ! -d .venv ]; then
    "$PYTHON_BIN" -m venv .venv
fi
# shellcheck source=/dev/null
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e ".[dev]"
green "✓ Dependencies installed"

# --- 3. Playwright (optional, kept as fallback adapter) ---------------------

bold "Step 3/7  Installing Playwright Chromium (fallback adapter)"
if confirm "Install Playwright Chromium? Needed only for the Playwright backend, not Unipile"; then
    playwright install chromium >/dev/null 2>&1 && green "✓ Playwright Chromium installed" || \
        yellow "⚠ Playwright install failed — fine if you're only using Unipile"
else
    yellow "↪ Skipped Playwright"
fi

# --- 4. DB init -------------------------------------------------------------

bold "Step 4/7  Initializing SQLite DB"
python -m linkedin_agent init
green "✓ DB ready"

# --- 5. .env setup ----------------------------------------------------------

bold "Step 5/7  Configuring credentials (.env)"
if [ -f .env ]; then
    if ! confirm ".env already exists — keep it as-is?"; then
        cp .env ".env.backup-$(date +%s)"
        yellow "(backed up existing .env)"
        rm .env
    fi
fi

if [ ! -f .env ]; then
    cp .env.example .env
    ask UNIPILE_KEY     "Unipile API key"
    ask UNIPILE_ACCT    "Unipile account ID"
    ask UNIPILE_DSN_VAL "Unipile DSN" "api1.unipile.com:13xxx"
    # In-place sed (BSD-friendly): use a sed with `''` extension
    sed -i.bak \
        -e "s|^UNIPILE_API_KEY=.*|UNIPILE_API_KEY=${UNIPILE_KEY}|" \
        -e "s|^UNIPILE_ACCOUNT_ID=.*|UNIPILE_ACCOUNT_ID=${UNIPILE_ACCT}|" \
        -e "s|^UNIPILE_DSN=.*|UNIPILE_DSN=${UNIPILE_DSN_VAL}|" \
        -e "s|^LINKEDIN_BACKEND=.*|LINKEDIN_BACKEND=unipile|" \
        .env
    rm -f .env.bak
    green "✓ Unipile creds written to .env"
fi

# --- 6. Validate Unipile auth (optional) ------------------------------------

bold "Step 6/7  Validating Unipile auth (live test)"
if confirm "Run live Unipile validation tests?"; then
    if pytest -m live tests/test_unipile_live.py -q 2>&1 | tail -5; then
        green "✓ Unipile validated"
    else
        red "✗ Live tests failed — check your credentials and rerun"
        yellow "  You can still continue; fix this before running 'linkedin daily'"
    fi
else
    yellow "↪ Skipped validation"
fi

# --- 7. Telegram bot --------------------------------------------------------

bold "Step 7/7  Telegram bot setup"

if grep -q "^TELEGRAM_BOT_TOKEN=." .env; then
    yellow "(TELEGRAM_BOT_TOKEN already set — skipping bot setup)"
else
    cat <<'EOF'
Create a Telegram bot:
  1. In Telegram, open: https://t.me/BotFather
  2. Send /newbot
  3. Pick a display name and unique username ending in 'bot'
  4. Copy the bot token (looks like 1234567890:AAH...XyZ)
EOF
    ask TG_TOKEN "Telegram bot token"

    cat <<EOF

Now message your bot once so it can reply:
  • In Telegram, search for your bot's username and tap Start
  • Send any text message (e.g. "hi")

EOF
    if ! confirm "Have you messaged the bot?"; then
        red "Bot setup requires you to send it a message first. Re-run setup.sh when ready."
        exit 1
    fi

    # Fetch chat_id from getUpdates
    CHAT_ID="$(curl -s "https://api.telegram.org/bot${TG_TOKEN}/getUpdates" \
        | python -c "
import json, sys
d = json.load(sys.stdin)
for u in reversed(d.get('result', [])):
    msg = u.get('message') or u.get('channel_post')
    if msg and msg.get('chat', {}).get('id'):
        print(msg['chat']['id'])
        sys.exit(0)
sys.exit(1)
" 2>/dev/null || true)"

    if [ -z "$CHAT_ID" ]; then
        red "Couldn't read chat_id from getUpdates. Either you haven't messaged the bot yet,"
        red "or the token is wrong. Run setup.sh again or set TELEGRAM_CHAT_ID manually."
        exit 1
    fi

    sed -i.bak \
        -e "/^TELEGRAM_BOT_TOKEN=/d" \
        -e "/^TELEGRAM_CHAT_ID=/d" \
        .env
    printf '\nTELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' "$TG_TOKEN" "$CHAT_ID" >> .env
    rm -f .env.bak

    green "✓ Telegram bot wired (chat_id=$CHAT_ID)"
    # Send a confirmation message via the bot
    curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        -H 'Content-Type: application/json' \
        -d "{\"chat_id\": ${CHAT_ID}, \"text\": \"✅ linkedin-agent setup complete. Drafts will land here for approval.\"}" >/dev/null
fi

# --- crontab offer ---------------------------------------------------------

bold "Cron schedule (optional)"
CRON_LINE="0 9-16 * * 1-5 $PROJECT_DIR/scripts/daily_outreach.sh >> /tmp/linkedin_outreach.log 2>&1"
echo "To run daily orchestration hourly Mon-Fri 9am-5pm, add:"
echo "  $CRON_LINE"

# Cron install is always opt-in. Skipped in non-interactive mode because it
# touches the user's crontab — needs explicit consent.
if [ -z "$NONINTERACTIVE" ]; then
    if confirm "Install this crontab line now?"; then
        if (crontab -l 2>/dev/null | grep -v daily_outreach.sh; echo "$CRON_LINE") | crontab - 2>/dev/null; then
            green "✓ Crontab installed"
        else
            yellow "⚠ Could not install crontab (permission denied or no access). Add the line above manually."
        fi
    fi
else
    yellow "↪ Skipped crontab install (non-interactive mode)"
fi

echo
bold "Setup complete."
echo "  • Run: linkedin status              # see pipeline at a glance"
echo "  • Run: linkedin campaign create my-campaign   # start a campaign"
echo "  • Run: linkedin bot-run             # start the Telegram daemon (separate terminal)"
