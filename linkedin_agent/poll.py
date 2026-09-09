from __future__ import annotations

# Inbound message polling.
#
# Strategy: each poll fetches recent inbound messages through
# Capability.INBOX_READ and relies on the unique index on
# messages.external_id for idempotency — duplicate inserts return None and get
# skipped, so we don't need a cursor/timestamp tracking table.
#
# Provider-agnostic. Unipile returns individual messages with native ids;
# PhantomBuster's Inbox Scraper returns one row per thread carrying only the
# latest message, with no per-message id, so its provider synthesises a
# deterministic one. Both arrive here as InboundMessage and this module cannot
# tell them apart.
#
# Latency differs and is worth knowing: Unipile polls on demand, while the
# Inbox Scraper is capped at 8 launches/day (~every 3h). Accepted deliberately
# for B2B reply times; pin inbox_read back to Unipile via a router override if
# that stops being true.
#
# For each new inbound message we:
#   1. Look up the prospect by sender provider_id.
#   2. Insert the inbound row into messages with external_id = unipile id.
#   3. Flip prospect.status to 'replied'          — replies to us only.
#   4. Cancel any pending/approved drafts for this prospect (a reply halts
#      the follow-up sequence — Phase 6's auto-followup respects this).
#      Replies to us only, for the same reason.
#   5. Push a Telegram notification with the excerpt + profile link.
#
# Steps 3 and 4 are gated on whether the inbound post-dates our last outbound
# (see _answers_our_last_outbound). A backlog message is recorded and notified
# but must not act on a live pipeline. Auto-drafting a reply carries a second,
# separate gate on age — REPLY_DRAFT_MAX_AGE_DAYS, see _is_stale — because a
# genuine reply that arrived during a polling gap should halt the sequence
# without also being answered automatically weeks late.
#
# Messages where the sender provider_id doesn't match any prospect we know
# about are skipped silently — those are random LinkedIn DMs (recruiters, etc.)
# that aren't part of our outreach pipeline.

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Sequence

from . import db
from .config import Config
from .providers import Capability, build_router
from .telegram import TelegramClient, TelegramError

logger = logging.getLogger("linkedin.poll")

# Per-poll batch size. Tune up if we ever miss messages between polls.
DEFAULT_BATCH = 50

# An inbound older than this is recorded but never auto-drafted against.
#
# The Inbox Scraper is incremental, so its FIRST run against a real account
# returns the whole backlog. The captured inbox from agent 1327575646342095
# contained threads whose last message was 15 months old. Without this guard,
# switching the provider would flood Telegram with drafted replies to
# year-old messages — and one approved by reflex would be genuinely
# embarrassing to send.
#
# The message is still recorded (history and dedup both need it) and the
# operator is still notified; only the automatic drafting is suppressed.
REPLY_DRAFT_MAX_AGE_DAYS = int(os.getenv("REPLY_DRAFT_MAX_AGE_DAYS", "30"))


def _answers_our_last_outbound(prospect_id: int, sent_at: str | None) -> bool | None:
    """True if this inbound post-dates the last thing we sent them.

    Age alone is the wrong discriminator for "should this halt the sequence".
    It cannot tell a 15-month-old backlog thread from a genuine reply that
    arrived during a gap in polling — and this branch creates exactly such a
    gap, between the last poll on the old provider and the first on the new
    one. A reply older than REPLY_DRAFT_MAX_AGE_DAYS is still a reply, and
    continuing to send DM2 and DM3 at someone who already answered is worse
    than the backlog problem the age gate was added for.

    Ordering answers it precisely: an inbound newer than our most recent
    outbound is a response to us whatever its age; one older than everything we
    sent is history that predates the conversation.

    Returns None when we have never sent them anything, because then there is
    no outbound to compare against and nothing they could be replying to. The
    caller falls back to the age gate for that case.
    """
    if not sent_at:
        return None
    with db.connect() as conn:
        row = conn.execute(
            """SELECT MAX(sent_at) AS last_out FROM messages
                WHERE prospect_id = ? AND direction = 'outbound'""",
            (prospect_id,)).fetchone()
    last_out = row["last_out"] if row else None
    if not last_out:
        return None
    try:
        inbound_at = datetime.fromisoformat(str(sent_at).replace("Z", "+00:00"))
        outbound_at = datetime.fromisoformat(str(last_out).replace("Z", "+00:00"))
    except ValueError:
        return None
    if inbound_at.tzinfo is None:
        inbound_at = inbound_at.replace(tzinfo=timezone.utc)
    if outbound_at.tzinfo is None:
        outbound_at = outbound_at.replace(tzinfo=timezone.utc)
    return inbound_at > outbound_at


def _is_stale(sent_at: str | None, *, now: datetime | None = None) -> bool:
    """True when an inbound is too old to auto-reply to.

    Unparseable or missing timestamps are treated as NOT stale: Unipile's
    payloads do not always carry one, and suppressing drafts for every message
    we cannot date would silently disable the reply flow.
    """
    if not sent_at:
        return False
    try:
        when = datetime.fromisoformat(str(sent_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - when) > timedelta(days=REPLY_DRAFT_MAX_AGE_DAYS)


@dataclass
class PollResult:
    fetched: int
    new_inbound: int
    matched_prospects: int
    notifications_sent: int
    skipped_unknown_sender: int


def poll_once(
    cfg: Config,
    limit: int = DEFAULT_BATCH,
    notify: bool = True,
    *,
    draft_replies: bool = True,
    drafter=None,
    router=None,
) -> PollResult:
    """Run one polling cycle. Returns a summary so cron-driven callers can log it.

    When `draft_replies` is True (default in production), each new inbound
    triggers an auto-drafted reply that gets pushed to Telegram for approval.
    If the drafter fails or returns INSUFFICIENT_CONTEXT, we fall back to the
    plain notify_reply alert so the user is still informed."""
    if drafter is None:
        from .drafter import draft as default_drafter, DrafterError  # noqa
        drafter = default_drafter
    db.init_db()
    own_router = router is None
    if own_router:
        try:
            router = build_router(cfg)
        except Exception as e:
            logger.warning("no provider configured for polling (%s) — skipping", e)
            return PollResult(fetched=0, new_inbound=0, matched_prospects=0,
                              notifications_sent=0, skipped_unknown_sender=0)
    if not router.owner_of(Capability.INBOX_READ):
        # Nothing can read the inbox — e.g. running `daily` in tests with a
        # fake adapter, or credentials absent. Skip rather than fail the cron.
        logger.info("no provider supports inbox_read — skipping poll")
        if own_router:
            router.close()
        return PollResult(fetched=0, new_inbound=0, matched_prospects=0,
                          notifications_sent=0, skipped_unknown_sender=0)
    tg: TelegramClient | None = None
    if notify:
        try:
            tg = TelegramClient(cfg)
        except TelegramError as e:
            logger.warning("telegram disabled (%s) — running poll without notifications", e)
            tg = None

    fetched = new_inbound = matched = sent_notifs = skipped = 0

    try:
        messages = router.perform(Capability.INBOX_READ, "fetch_inbox", limit)
        fetched = len(messages)
        # Process oldest-first so notifications arrive in chronological order
        for m in reversed(messages):
            if m.is_from_me:
                continue   # outbound — we already have it (or it's noise)

            if not m.prospect_provider_id:
                continue

            prospect = db.get_prospect_by_provider_id(m.prospect_provider_id)
            if not prospect:
                skipped += 1
                continue
            matched += 1

            external_id = m.external_id
            inserted_id = db.record_message(
                prospect_id=int(prospect["id"]),
                direction="inbound",
                body=m.body,
                external_id=external_id,
                thread_id=m.thread_id,
                # The provider's timestamp, not ours: a batch scraper can
                # surface a reply hours after it arrived, and follow-up timing
                # reads this column.
                sent_at=m.sent_at,
            )
            if inserted_id is None:
                # Duplicate — we've already processed this message in a prior poll.
                continue

            new_inbound += 1

            # Decided BEFORE anything acts on the message. The Inbox Scraper is
            # incremental, so its first run against a real account returns the
            # entire backlog — the captured inbox held threads whose last
            # message was 15 months old. Cancelling drafts and flipping status
            # on those permanently halts a live sequence because of a
            # conversation that ended over a year ago, and nothing later in
            # this loop can undo it.
            #
            # Ordering decides it where we have an outbound to compare against:
            # anything newer than our last message to them is a reply and halts
            # the sequence however old it is. Age is only the fallback, for a
            # prospect we have never written to — where there is nothing they
            # could be replying to.
            #
            # A backlog inbound is still recorded and still notified: history
            # and dedup both need the row, and the operator should see it. What
            # it does not do is touch the pipeline.
            #
            # Two separate questions, deliberately not one flag:
            #
            #   halt      should this stop the outbound sequence?  Ordering.
            #   too_old   should we auto-draft a reply to it?      Age.
            #
            # They differ for a genuine reply that arrived during a polling
            # gap. It must halt — chasing someone who already answered is the
            # worse failure — but auto-drafting a response to a 40-day-old
            # message is its own embarrassment, and REPLY_DRAFT_MAX_AGE_DAYS
            # was added for exactly that. Collapsing them into one flag forces
            # a wrong answer to one of the two.
            answers_us = _answers_our_last_outbound(int(prospect["id"]), m.sent_at)
            too_old = _is_stale(m.sent_at)
            halt = answers_us if answers_us is not None else not too_old

            if not halt:
                logger.info(
                    "inbound for prospect %d dates from %s and predates our "
                    "last message to them — recording it, but leaving status "
                    "and pending drafts alone", prospect["id"], m.sent_at)
            else:
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
                "reply_stale" if not halt else "reply",
                None,
                external_id,
                False,
            )

            if tg:
                inbound_body = m.body
                # Auto-draft a reply suggestion. On success we push the draft
                # card (which embeds the inbound at the top); on failure we
                # fall back to the plain notification so the user still sees
                # the reply landed.
                draft_pushed = False
                if draft_replies and inbound_body.strip() and halt and not too_old:
                    try:
                        reply_body = drafter("reply", int(prospect["id"]))
                        draft_id = db.enqueue_draft(
                            int(prospect["id"]), "reply", reply_body,
                        )
                        campaign_name = None
                        if prospect["campaign_id"]:
                            camp = db.get_campaign(int(prospect["campaign_id"]))
                            campaign_name = camp["name"] if camp else None
                        msg_id = tg.push_draft_for_approval(
                            draft_id=draft_id,
                            kind="reply",
                            body=reply_body,
                            prospect_name=prospect["full_name"],
                            prospect_company=prospect["company"],
                            prospect_url=prospect["linkedin_url"],
                            campaign_name=campaign_name,
                            inbound_excerpt=inbound_body,
                        )
                        db.set_draft_telegram_id(draft_id, msg_id)
                        draft_pushed = True
                        sent_notifs += 1
                    except Exception as e:
                        logger.warning(
                            "auto-reply drafter failed for prospect %d: %s — "
                            "falling back to plain notify",
                            prospect["id"], e,
                        )

                if not draft_pushed:
                    try:
                        tg.notify_reply(
                            prospect_name=prospect["full_name"],
                            prospect_company=prospect["company"],
                            body=inbound_body,
                            thread_url=prospect["linkedin_url"],
                        )
                        sent_notifs += 1
                    except TelegramError as e:
                        logger.warning("telegram notify failed: %s", e)

    finally:
        if own_router:
            router.close()
        if tg:
            tg.close()

    return PollResult(
        fetched=fetched,
        new_inbound=new_inbound,
        matched_prospects=matched,
        notifications_sent=sent_notifs,
        skipped_unknown_sender=skipped,
    )
