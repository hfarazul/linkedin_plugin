#!/usr/bin/env bash
# Install the LinkedIn bot daemon as a macOS LaunchAgent.
#
# After install:
#   • The bot daemon auto-starts when you log in
#   • Restarts within 10s if it crashes
#   • Logs to data/bot-daemon.{out,err}.log
#   • Survives sleep/wake (the daemon itself uses long-polling so it just resumes)
#
# Idempotent: safe to re-run after code/env changes.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$PROJECT_DIR/scripts/com.cortivo.linkedin-bot.plist.template"
PLIST_NAME="com.cortivo.linkedin-bot.plist"
TARGET="$HOME/Library/LaunchAgents/$PLIST_NAME"
VENV_PY="$PROJECT_DIR/.venv/bin/python"
LABEL="com.cortivo.linkedin-bot"

if [ ! -f "$VENV_PY" ]; then
    echo "✗ Python venv not found at $VENV_PY"
    echo "  Run ./setup.sh first (or python3 -m venv .venv && pip install -e .[dev])"
    exit 1
fi

if [ ! -f "$TEMPLATE" ]; then
    echo "✗ Plist template missing at $TEMPLATE"
    exit 1
fi

# Unload existing instance (if any) so we can replace it cleanly.
if launchctl list | grep -q "$LABEL"; then
    echo "→ Unloading existing $LABEL"
    launchctl unload "$TARGET" 2>/dev/null || true
fi

# Build the final plist by substituting placeholders.
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$PROJECT_DIR/data"
sed \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__VENV_PYTHON__|$VENV_PY|g" \
    "$TEMPLATE" > "$TARGET"

# Load it. With macOS 13+ launchctl wants bootstrap, but `load` still works.
launchctl load "$TARGET"

# Verify it's running
sleep 1
if launchctl list | grep -q "$LABEL"; then
    pid=$(launchctl list | awk -v label="$LABEL" '$3 == label { print $1 }')
    echo "✓ Installed and running (PID $pid)"
    echo
    echo "  Plist:   $TARGET"
    echo "  Logs:    $PROJECT_DIR/data/bot-daemon.out.log"
    echo "           $PROJECT_DIR/data/bot-daemon.err.log"
    echo
    echo "  Tail logs:   tail -f data/bot-daemon.out.log"
    echo "  Stop:        launchctl unload ~/Library/LaunchAgents/$PLIST_NAME"
    echo "  Restart:     launchctl unload ~/Library/LaunchAgents/$PLIST_NAME && launchctl load ~/Library/LaunchAgents/$PLIST_NAME"
else
    echo "✗ Load attempted but daemon not showing up. Check:"
    echo "  tail $PROJECT_DIR/data/bot-daemon.err.log"
    exit 1
fi
