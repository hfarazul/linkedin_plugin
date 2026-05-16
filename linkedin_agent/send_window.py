from __future__ import annotations

# Send-window enforcement.
#
# Outbound Unipile writes (react / connect / dm) only happen during business
# hours in the user's local time: 9 AM to 5 PM, Monday to Friday.
#
# Drafts that get approved outside this window stay in status='approved'
# without being sent — the daily cron's `send-approved` step flushes them
# during the next open window.
#
# Pure functions for ease of unit testing with freezegun.

import os
from datetime import datetime, time, timedelta

WINDOW_START_HOUR = 9
WINDOW_END_HOUR   = 17     # exclusive: 17:00 is closed
LAST_WEEKDAY      = 4      # Mon=0 ... Fri=4


def is_open(now: datetime | None = None) -> bool:
    """True iff `now` (local time) is within the send window.
    Defaults to datetime.now() if not provided.

    For tests/subprocesses where freezegun doesn't propagate, set
    LINKEDIN_FAKE_WINDOW=open or =closed to pin the result."""
    override = os.environ.get("LINKEDIN_FAKE_WINDOW", "").strip().lower()
    if override == "open":
        return True
    if override == "closed":
        return False
    if now is None:
        now = datetime.now()
    if now.weekday() > LAST_WEEKDAY:
        return False
    return WINDOW_START_HOUR <= now.hour < WINDOW_END_HOUR


def next_open_time(now: datetime | None = None) -> datetime:
    """Return the next moment the send window will open.
    Returns `now` itself if the window is currently open."""
    if now is None:
        now = datetime.now()
    if is_open(now):
        return now

    candidate = now.replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    # If today is a weekday but we're past the window, jump to tomorrow's 9am.
    if now.weekday() <= LAST_WEEKDAY and now.hour >= WINDOW_END_HOUR:
        candidate += timedelta(days=1)
    # If we're before 9am on a weekday, candidate is already correct.
    # Now advance past any weekend days.
    while candidate.weekday() > LAST_WEEKDAY:
        candidate += timedelta(days=1)
    # Ensure it's at 09:00 (timedelta preserved the time component above only
    # in the "next-day rollover" branch, so we re-set defensively).
    candidate = candidate.replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    return candidate


def format_next_open(now: datetime | None = None) -> str:
    """User-facing string like 'Mon 9:00 AM' for Telegram messages."""
    when = next_open_time(now)
    # Cross-platform format — %-I works on POSIX, %#I on Windows. Use a manual fallback.
    return when.strftime("%a %-I:%M %p") if hasattr(when, "strftime") else str(when)
