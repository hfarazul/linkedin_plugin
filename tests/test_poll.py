"""Tests for the poll module's auto-reply drafting wiring.

When a new inbound message comes in, poll_once should:
  1. Insert the inbound into the messages table.
  2. Flip prospect status to 'replied'.
  3. Cancel any pending followup drafts (existing behavior).
  4. Invoke the drafter for kind='reply' and push the result to Telegram
     for approval — the NEW behavior. Falls back to a plain notify_reply
     if the drafter fails.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.fakes import FakeTelegramClient

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
@respx.mock
def test_poll_auto_drafts_reply_on_new_inbound(db_env, monkeypatch):
    """End-to-end: an inbound message lands → poll invokes the drafter →
    a pending_drafts row is created with kind='reply' → Telegram gets the
    draft card (with inbound embedded), not just a plain notify."""
    from linkedin_agent import db, poll as poll_mod, telegram as tg_mod

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/auto-draft-test",
        full_name="Auto Draft Test",
        company="Acme",
        provider_id="ACoAUTODRAFT",
    )

    respx.get("https://api21.unipile.com:15165/api/v1/messages").mock(
        return_value=httpx.Response(200, json={
            "items": [{
                "id": "msg-1",
                "is_sender": False,
                "sender_id": "ACoAUTODRAFT",
                "text": "Thanks for connecting — happy to chat.",
                "chat_id": "chat-1",
            }],
            "cursor": None,
        })
    )

    # Replace the real TelegramClient with our fake; we don't want HTTP to TG.
    fake_tg = FakeTelegramClient(_cfg())
    # poll.py does `from .telegram import TelegramClient` — patch the imported
    # reference, not the source module.
    monkeypatch.setattr(poll_mod, "TelegramClient", lambda c: fake_tg)

    result = poll_mod.poll_once(_cfg(), notify=True, drafter=_stub_drafter_ok)

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
@respx.mock
def test_poll_falls_back_to_notify_when_drafter_fails(db_env, monkeypatch):
    """If the drafter raises (e.g. INSUFFICIENT_CONTEXT or claude error), poll
    still records the inbound and falls back to the plain notify_reply alert
    so the user is informed something landed."""
    from linkedin_agent import db, poll as poll_mod, telegram as tg_mod

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/fallback-test",
        full_name="Fallback Test",
        provider_id="ACoFALLBACK",
    )

    respx.get("https://api21.unipile.com:15165/api/v1/messages").mock(
        return_value=httpx.Response(200, json={
            "items": [{
                "id": "msg-2",
                "is_sender": False,
                "sender_id": "ACoFALLBACK",
                "text": "ok",   # too sparse for the drafter to work with
                "chat_id": "chat-2",
            }],
            "cursor": None,
        })
    )

    fake_tg = FakeTelegramClient(_cfg())
    # poll.py does `from .telegram import TelegramClient` — patch the imported
    # reference, not the source module.
    monkeypatch.setattr(poll_mod, "TelegramClient", lambda c: fake_tg)

    result = poll_mod.poll_once(_cfg(), notify=True, drafter=_stub_drafter_insufficient)

    assert result.new_inbound == 1
    # No draft created
    drafts = [d for d in db.list_pending_drafts() if d["prospect_id"] == pid]
    assert drafts == []
    # No draft card pushed
    assert fake_tg.drafts_pushed == []
    # But plain notification DID go out so the user knows something landed
    assert len(fake_tg.replies_notified) == 1


@pytest.mark.integration
@respx.mock
def test_poll_skips_drafting_when_disabled(db_env, monkeypatch):
    """draft_replies=False reverts to legacy notify-only behavior. Used by
    tests / by anyone who wants to keep the drafter out of the poll loop."""
    from linkedin_agent import db, poll as poll_mod, telegram as tg_mod

    db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/disabled-test",
        full_name="Disabled Test",
        provider_id="ACoDISABLED",
    )

    respx.get("https://api21.unipile.com:15165/api/v1/messages").mock(
        return_value=httpx.Response(200, json={
            "items": [{
                "id": "msg-3",
                "is_sender": False,
                "sender_id": "ACoDISABLED",
                "text": "Real substantive reply.",
                "chat_id": "chat-3",
            }],
            "cursor": None,
        })
    )

    fake_tg = FakeTelegramClient(_cfg())
    # poll.py does `from .telegram import TelegramClient` — patch the imported
    # reference, not the source module.
    monkeypatch.setattr(poll_mod, "TelegramClient", lambda c: fake_tg)

    # Sentinel drafter — must not be called when draft_replies=False
    def forbidden(*args, **kwargs):
        raise AssertionError("drafter should not run when draft_replies=False")

    result = poll_mod.poll_once(_cfg(), notify=True, draft_replies=False, drafter=forbidden)

    assert result.new_inbound == 1
    # No draft pushed, but plain notify happened
    assert fake_tg.drafts_pushed == []
    assert len(fake_tg.replies_notified) == 1
