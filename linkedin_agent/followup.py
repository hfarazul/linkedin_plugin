from __future__ import annotations

# Follow-up scheduler.
#
# Identifies prospects whose next follow-up DM is due, drafts via the drafter
# subagent, and enqueues for approval through Telegram. Also auto-flips stale
# prospects (no reply after DM3 + buffer) to disposition='ghosted'.
#
# Pure-logic candidate functions are easy to unit-test with freezegun. The
# orchestration loop pulls from the DB and pushes to Telegram.

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import db
from .config import Config

logger = logging.getLogger("linkedin.followup")

# Cadence (days). Each interval is from `last_dm_at`.
DM2_DELAY_DAYS  = 4
DM3_DELAY_DAYS  = 11
GHOST_DELAY_DAYS = 14   # measured from last_dm_at AFTER dm3 sent


@dataclass
class FollowupResult:
    dm2_enqueued: int = 0
    dm3_enqueued: int = 0
    ghosted: int = 0
    drafts_skipped_existing: int = 0
    drafts_failed: int = 0


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    # SQLite ISO strings may have +00:00 suffix from db.now()
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _age_days(last_dm_at: str | None, now: datetime) -> float | None:
    when = _parse_iso(last_dm_at)
    if not when:
        return None
    # Ensure timezone-aware comparison
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - when).total_seconds() / 86400.0


# ----- pure candidate predicates ---------------------------------------------

def is_dm2_due(prospect, now: datetime) -> bool:
    """DM2 is due when we sent DM1 (dm_count==1), they haven't replied, and at
    least DM2_DELAY_DAYS have elapsed since last_dm_at."""
    if prospect["status"] == "replied":
        return False
    if prospect["dm_count"] != 1:
        return False
    age = _age_days(prospect["last_dm_at"], now)
    return age is not None and age >= DM2_DELAY_DAYS


def is_dm3_due(prospect, now: datetime) -> bool:
    if prospect["status"] == "replied":
        return False
    if prospect["dm_count"] != 2:
        return False
    age = _age_days(prospect["last_dm_at"], now)
    return age is not None and age >= DM3_DELAY_DAYS


def is_ghost_candidate(prospect, now: datetime) -> bool:
    """Mark as ghosted if DM3 has been sent and enough time has passed with
    no reply. Doesn't touch prospects already flagged with a disposition."""
    if prospect["disposition"]:
        return False
    if prospect["status"] == "replied":
        return False
    if prospect["dm_count"] < 3:
        return False
    age = _age_days(prospect["last_dm_at"], now)
    return age is not None and age >= GHOST_DELAY_DAYS


# ----- orchestration --------------------------------------------------------

# The drafter signature we depend on. Tests can pass a stub like:
#   lambda kind, prospect_id, recent_posts=None: "fake draft text"
DrafterFn = Callable[..., str]


def _has_existing_pending_or_approved(prospect_id: int, kind: str) -> bool:
    with db.connect() as conn:
        cur = conn.execute(
            """SELECT 1 FROM pending_drafts
               WHERE prospect_id = ? AND kind = ? AND status IN ('pending', 'approved')
               LIMIT 1""",
            (prospect_id, kind),
        )
        return cur.fetchone() is not None


def run_followup_cycle(
    cfg: Config,
    *,
    drafter: DrafterFn,
    telegram=None,
    now: datetime | None = None,
) -> FollowupResult:
    """One pass: enqueue any due DM2 / DM3 drafts, auto-ghost stale prospects.

    Args:
      drafter:  callable returning a draft body, signature `(kind, prospect_id)`.
                Inject a stub in tests; production code passes drafter.draft.
      telegram: a TelegramClient (or FakeTelegramClient) to push approval cards.
                If None, drafts are enqueued silently.
      now:      override the clock for testing. Defaults to UTC now.
    """
    db.init_db()
    if now is None:
        now = datetime.now(timezone.utc)

    result = FollowupResult()
    candidates = db.list_prospects(status="dm_sent", limit=10_000)

    # Step 1 — auto-ghost stale prospects before drafting new follow-ups.
    for p in candidates:
        if is_ghost_candidate(p, now):
            db.set_disposition(int(p["id"]), "ghosted")
            db.log_action(int(p["id"]), "auto_ghost", None, "no reply after dm3", False)
            result.ghosted += 1

    # Refresh after dispositions changed (ghosted prospects no longer need follow-up).
    for p in candidates:
        # Skip if we just ghosted them above
        fresh = db.get_prospect(int(p["id"]))
        if fresh is None or fresh["disposition"] == "ghosted":
            continue

        if is_dm3_due(fresh, now):
            kind = "dm3"
        elif is_dm2_due(fresh, now):
            kind = "dm2"
        else:
            continue

        if _has_existing_pending_or_approved(int(fresh["id"]), kind):
            result.drafts_skipped_existing += 1
            continue

        try:
            body = drafter(kind, int(fresh["id"]))
        except Exception as e:
            logger.warning("drafter failed for prospect %d (%s): %s", fresh["id"], kind, e)
            result.drafts_failed += 1
            continue

        draft_id = db.enqueue_draft(int(fresh["id"]), kind, body)
        if kind == "dm2":
            result.dm2_enqueued += 1
        else:
            result.dm3_enqueued += 1

        if telegram is not None:
            campaign_name = None
            if fresh["campaign_id"]:
                row = db.get_campaign(int(fresh["campaign_id"]))
                campaign_name = row["name"] if row else None
            try:
                msg_id = telegram.push_draft_for_approval(
                    draft_id=draft_id,
                    kind=kind,
                    body=body,
                    prospect_name=fresh["full_name"],
                    prospect_company=fresh["company"],
                    prospect_url=fresh["linkedin_url"],
                    campaign_name=campaign_name,
                )
                db.set_draft_telegram_id(draft_id, msg_id)
            except Exception as e:
                logger.warning("telegram push failed for draft %d: %s", draft_id, e)

    return result
