"""Retargeted when Unipile was removed.

These previously mocked Unipile's /messages endpoint with respx. The
assertions were always about the reply workflow — inbound lands, a reply is
drafted, Telegram gets the card, and a drafter failure degrades to a plain
notification — so they now run against a fake provider through the capability
router instead of a transport that no longer exists.

Tests for the poll module's auto-reply drafting wiring.

When a new inbound message comes in, poll_once should:
  1. Insert the inbound into the messages table.
  2. Flip prospect status to 'replied'.
  3. Cancel any pending followup drafts (existing behavior).
  4. Invoke the drafter for kind='reply' and push the result to Telegram
     for approval — the NEW behavior. Falls back to a plain notify_reply
     if the drafter fails.
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeProvider, FakeTelegramClient, fake_router

# Note: poll.py does `from .telegram import TelegramClient` at module load
# time. To inject the fake, we patch `linkedin_agent.poll.TelegramClient`
# (the imported reference), not `linkedin_agent.telegram.TelegramClient`
# (the source class). Tests use monkeypatch.setattr(poll_mod, ...) for this.


def _cfg(**overrides):
    base = {
        "backend": "fake",
        "daily_max_reactions": 30,
        "daily_max_connections": 20,
        "daily_max_dms": 10,
        "daily_max_searches": 50,
        "action_delay_min": 0,
        "action_delay_max": 0,
        "dry_run": False,
        "unipile_api_key": "test-key",
        "unipile_account_id": "test-account",
        "unipile_dsn": "api21.unipile.com:15165",
        "telegram_bot_token": "test-token",
        "telegram_chat_id": 12345,
        "playwright_state_path": None,
    }
    base.update(overrides)
    return type("CFG", (), base)()


def _stub_drafter_ok(kind, prospect_id, recent_posts=None):
    """A drafter stub that always succeeds with a fixed reply body."""
    assert kind == "reply", f"poll should call drafter with 'reply', got {kind!r}"
    return "Stub reply — glad you're open. Drop a few time windows."


def _stub_drafter_insufficient(kind, prospect_id, recent_posts=None):
    """A drafter stub that raises (simulating INSUFFICIENT_CONTEXT or error)."""
    from linkedin_agent.drafter import DrafterError
    raise DrafterError("INSUFFICIENT_CONTEXT — stub")


@pytest.mark.integration
def test_poll_auto_drafts_reply_on_new_inbound(db_env, monkeypatch):
    """End-to-end: an inbound message lands → poll invokes the drafter →
    a pending_drafts row is created with kind='reply' → Telegram gets the
    draft card (with inbound embedded), not just a plain notify."""
    from linkedin_agent import db, poll as poll_mod, telegram as tg_mod
    from linkedin_agent.providers.capabilities import InboundMessage

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/auto-draft-test",
        full_name="Auto Draft Test",
        company="Acme",
        provider_id="ACoAUTODRAFT",
    )

    provider = FakeProvider(inbox=[InboundMessage(
        external_id="msg-1", prospect_provider_id="ACoAUTODRAFT",
        body="Thanks for connecting — happy to chat.",
        sent_at="2026-09-02T10:00:00Z", thread_id="chat-1",
        is_from_me=False, source="phantombuster")])

    # Replace the real TelegramClient with our fake; we don't want HTTP to TG.
    fake_tg = FakeTelegramClient(_cfg())
    # poll.py does `from .telegram import TelegramClient` — patch the imported
    # reference, not the source module.
    monkeypatch.setattr(poll_mod, "TelegramClient", lambda c: fake_tg)

    result = poll_mod.poll_once(_cfg(), router=fake_router(provider), notify=True, drafter=_stub_drafter_ok)

    assert result.new_inbound == 1
    # Status flipped
    assert db.get_prospect(pid)["status"] == "replied"
    # A reply draft was enqueued
    drafts = [d for d in db.list_pending_drafts() if d["prospect_id"] == pid and d["kind"] == "reply"]
    assert len(drafts) == 1
    assert drafts[0]["body"].startswith("Stub reply")
    # Draft card was pushed (NOT the plain notify_reply path)
    assert len(fake_tg.drafts_pushed) == 1
    pushed = fake_tg.drafts_pushed[0]
    assert pushed.kind == "reply"
    assert pushed.inbound_excerpt == "Thanks for connecting — happy to chat."
    # The plain notify_reply path should NOT have run (no double-pings)
    assert fake_tg.replies_notified == []


@pytest.mark.integration
def test_poll_falls_back_to_notify_when_drafter_fails(db_env, monkeypatch):
    """If the drafter raises (e.g. INSUFFICIENT_CONTEXT or claude error), poll
    still records the inbound and falls back to the plain notify_reply alert
    so the user is informed something landed."""
    from linkedin_agent import db, poll as poll_mod, telegram as tg_mod
    from linkedin_agent.providers.capabilities import InboundMessage

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/fallback-test",
        full_name="Fallback Test",
        provider_id="ACoFALLBACK",
    )

    provider = FakeProvider(inbox=[InboundMessage(
        external_id="msg-1", prospect_provider_id="ACoFALLBACK",
        body="ok",
        sent_at="2026-09-02T10:00:00Z", thread_id="chat-1",
        is_from_me=False, source="phantombuster")])

    fake_tg = FakeTelegramClient(_cfg())
    # poll.py does `from .telegram import TelegramClient` — patch the imported
    # reference, not the source module.
    monkeypatch.setattr(poll_mod, "TelegramClient", lambda c: fake_tg)

    result = poll_mod.poll_once(_cfg(), router=fake_router(provider), notify=True, drafter=_stub_drafter_insufficient)

    assert result.new_inbound == 1
    # No draft created
    drafts = [d for d in db.list_pending_drafts() if d["prospect_id"] == pid]
    assert drafts == []
    # No draft card pushed
    assert fake_tg.drafts_pushed == []
    # But plain notification DID go out so the user knows something landed
    assert len(fake_tg.replies_notified) == 1


@pytest.mark.integration
def test_poll_skips_drafting_when_disabled(db_env, monkeypatch):
    """draft_replies=False reverts to legacy notify-only behavior. Used by
    tests / by anyone who wants to keep the drafter out of the poll loop."""
    from linkedin_agent import db, poll as poll_mod, telegram as tg_mod
    from linkedin_agent.providers.capabilities import InboundMessage

    db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/disabled-test",
        full_name="Disabled Test",
        provider_id="ACoDISABLED",
    )

    provider = FakeProvider(inbox=[InboundMessage(
        external_id="msg-1", prospect_provider_id="ACoDISABLED",
        body="Real substantive reply.",
        sent_at="2026-09-02T10:00:00Z", thread_id="chat-1",
        is_from_me=False, source="phantombuster")])

    fake_tg = FakeTelegramClient(_cfg())
    # poll.py does `from .telegram import TelegramClient` — patch the imported
    # reference, not the source module.
    monkeypatch.setattr(poll_mod, "TelegramClient", lambda c: fake_tg)

    # Sentinel drafter — must not be called when draft_replies=False
    def forbidden(*args, **kwargs):
        raise AssertionError("drafter should not run when draft_replies=False")

    result = poll_mod.poll_once(_cfg(), router=fake_router(provider), notify=True, draft_replies=False, drafter=forbidden)

    assert result.new_inbound == 1
    # No draft pushed, but plain notify happened
    assert fake_tg.drafts_pushed == []
    assert len(fake_tg.replies_notified) == 1


@pytest.mark.integration
def test_stale_inbound_does_not_halt_a_live_sequence(db_env, monkeypatch):
    """A backlog message must not cancel drafts or flip status.

    The Inbox Scraper is incremental, so its first run against a real account
    returns months of history — the captured inbox held threads whose last
    message was 15 months old. The staleness check existed but ran *after* the
    cancel-and-flip block, so every matching prospect had their live sequence
    permanently halted by a conversation that ended over a year ago, and
    nothing downstream could undo it.

    Recorded and notified: yes, both are wanted. Acting on the pipeline: no.
    """
    from linkedin_agent import db, poll as poll_mod
    from linkedin_agent.providers.capabilities import InboundMessage

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/stale-backlog",
        full_name="Stale Backlog",
        provider_id="ACoSTALE",
    )
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET status='dm_sent' WHERE id=?", (pid,))
    live_draft = db.enqueue_draft(pid, "dm2", "a live follow-up awaiting approval")

    provider = FakeProvider(inbox=[InboundMessage(
        external_id="msg-old", prospect_provider_id="ACoSTALE",
        body="Sure, let's talk next quarter.",
        sent_at="2025-05-01T10:00:00Z", thread_id="chat-old",
        is_from_me=False, source="phantombuster")])

    fake_tg = FakeTelegramClient(_cfg())
    monkeypatch.setattr(poll_mod, "TelegramClient", lambda c: fake_tg)

    def forbidden(*a, **k):
        raise AssertionError("a 15-month-old message must not be auto-drafted")

    result = poll_mod.poll_once(_cfg(), router=fake_router(provider),
                                notify=True, drafter=forbidden)

    # Recorded, so dedup and history both work.
    assert result.new_inbound == 1
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT external_id FROM messages WHERE prospect_id=? AND direction='inbound'",
            (pid,)).fetchall()
    assert [r["external_id"] for r in rows] == ["msg-old"]

    # But the live sequence is untouched.
    assert db.get_prospect(pid)["status"] == "dm_sent"
    assert db.get_draft(live_draft)["status"] == "pending"

    # Logged distinguishably, and the operator still hears about it.
    with db.connect() as conn:
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM actions WHERE prospect_id=?", (pid,)).fetchall()]
    assert "reply_stale" in kinds and "reply" not in kinds
    assert len(fake_tg.replies_notified) == 1


@pytest.mark.integration
def test_recent_inbound_still_halts_the_sequence(db_env, monkeypatch):
    """The counterpart: the staleness guard must not disarm normal replies."""
    from datetime import datetime, timedelta, timezone
    from linkedin_agent import db, poll as poll_mod
    from linkedin_agent.providers.capabilities import InboundMessage

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/fresh-reply",
        full_name="Fresh Reply",
        provider_id="ACoFRESH",
    )
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET status='dm_sent' WHERE id=?", (pid,))
    live_draft = db.enqueue_draft(pid, "dm2", "a live follow-up awaiting approval")

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    provider = FakeProvider(inbox=[InboundMessage(
        external_id="msg-new", prospect_provider_id="ACoFRESH",
        body="Interested — what does this look like?",
        sent_at=yesterday, thread_id="chat-new",
        is_from_me=False, source="phantombuster")])

    fake_tg = FakeTelegramClient(_cfg())
    monkeypatch.setattr(poll_mod, "TelegramClient", lambda c: fake_tg)

    poll_mod.poll_once(_cfg(), router=fake_router(provider), notify=True,
                       drafter=_stub_drafter_ok)

    assert db.get_prospect(pid)["status"] == "replied"
    assert db.get_draft(live_draft)["status"] == "rejected"
