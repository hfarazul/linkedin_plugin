from __future__ import annotations

# Inbound message polling.
#
# Strategy: each poll fetches the N most recent messages from Unipile and
# filters for inbound (is_sender == 0). We rely on a unique index on
# messages.external_id for idempotency — duplicate inserts return None and
# get skipped, so we don't need a cursor/timestamp tracking table.
#
# For each new inbound message we:
#   1. Look up the prospect by sender provider_id.
#   2. Insert the inbound row into messages with external_id = unipile id.
#   3. Flip prospect.status to 'replied'.
#   4. Cancel any pending/approved drafts for this prospect (a reply halts
#      the follow-up sequence — Phase 6's auto-followup respects this).
#   5. Push a Telegram notification with the excerpt + profile link.
#
# Messages where the sender provider_id doesn't match any prospect we know
# about are skipped silently — those are random LinkedIn DMs (recruiters, etc.)
# that aren't part of our outreach pipeline.

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

import httpx

from . import db
from .config import Config
from .telegram import TelegramClient, TelegramError

logger = logging.getLogger("linkedin.poll")

# Per-poll batch size. Tune up if we ever miss messages between polls.
DEFAULT_BATCH = 50


@dataclass
class PollResult:
    fetched: int
    new_inbound: int
    matched_prospects: int
    notifications_sent: int
    skipped_unknown_sender: int


class _UnipileMessages:
    """Thin client just for the /messages endpoint (no need to hold the full
    adapter open for polling)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client = httpx.Client(
            base_url=f"https://{cfg.unipile_dsn}/api/v1",
            headers={"X-API-KEY": cfg.unipile_api_key, "accept": "application/json"},
            timeout=60.0,
        )

    def close(self) -> None:
        self._client.close()

    def recent(self, limit: int = DEFAULT_BATCH) -> list[dict]:
        r = self._client.get(
            "/messages",
            params={"account_id": self.cfg.unipile_account_id, "limit": limit},
        )
        r.raise_for_status()
        return r.json().get("items", [])


def poll_once(cfg: Config, limit: int = DEFAULT_BATCH, notify: bool = True) -> PollResult:
    """Run one polling cycle. Returns a summary so cron-driven callers can log it."""
    db.init_db()
    msg_client = _UnipileMessages(cfg)
    tg: TelegramClient | None = None
    if notify:
        try:
            tg = TelegramClient(cfg)
        except TelegramError as e:
            logger.warning("telegram disabled (%s) — running poll without notifications", e)
            tg = None

    fetched = new_inbound = matched = sent_notifs = skipped = 0

    try:
        messages = msg_client.recent(limit=limit)
        fetched = len(messages)
        # Process oldest-first so notifications arrive in chronological order
        for m in reversed(messages):
            if m.get("is_sender"):
                continue   # outbound — we already have it (or it's noise)

            sender_id = m.get("sender_id")
            if not sender_id:
                continue

            prospect = db.get_prospect_by_provider_id(sender_id)
            if not prospect:
                skipped += 1
                continue
            matched += 1

            external_id = m.get("id") or m.get("provider_id")
            inserted_id = db.record_message(
                prospect_id=int(prospect["id"]),
                direction="inbound",
                body=m.get("text") or "",
                external_id=external_id,
            )
            if inserted_id is None:
                # Duplicate — we've already processed this message in a prior poll.
                continue

            new_inbound += 1

            # Halt the follow-up sequence for this prospect.
            cancelled = db.cancel_pending_drafts_for(
                int(prospect["id"]), reason="reply_received"
            )
            if cancelled:
                logger.info(
                    "cancelled %d pending draft(s) for prospect %d (reply received)",
                    cancelled, prospect["id"],
                )

            db.set_status(int(prospect["id"]), "replied")
            db.log_action(
                int(prospect["id"]),
                "reply",
                None,
                external_id,
                False,
            )

            if tg:
                try:
                    tg.notify_reply(
                        prospect_name=prospect["full_name"],
                        prospect_company=prospect["company"],
                        body=m.get("text") or "",
                        thread_url=prospect["linkedin_url"],
                    )
                    sent_notifs += 1
                except TelegramError as e:
                    logger.warning("telegram notify failed: %s", e)

    finally:
        msg_client.close()
        if tg:
            tg.close()

    return PollResult(
        fetched=fetched,
        new_inbound=new_inbound,
        matched_prospects=matched,
        notifications_sent=sent_notifs,
        skipped_unknown_sender=skipped,
    )
