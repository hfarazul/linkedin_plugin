"""Tests for the bot daemon's callback dispatcher.

We construct a BotDaemon with the FakeTelegramClient + fake LinkedIn adapter
and synthesize Telegram update payloads to drive each branch:
  • approve, reject, edit (existing)
  • retry, giveup (fix #3 — fail → retry/giveup buttons work)
  • idempotency on terminal statuses
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeTelegramClient


def _make_cfg(**overrides):
    base = {
        "backend": "fake",
        "daily_max_reactions": 30,
        "daily_max_connections": 20,
        "daily_max_dms": 10,
        "daily_max_searches": 50,
        "action_delay_min": 0,
        "action_delay_max": 0,
        "dry_run": False,
        "unipile_api_key": None,
        "unipile_account_id": None,
        "unipile_dsn": None,
        "telegram_bot_token": "fake-token",
        "telegram_chat_id": 12345,
        "playwright_state_path": None,
    }
    base.update(overrides)
    return type("CFG", (), base)()


def _seed_prospect_and_draft(kind: str, body: str = "hi there test body that is long enough"):
    """Seed one prospect + one pending draft. Returns (prospect_id, draft_id)."""
    from linkedin_agent import db
    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/test-callback",
        full_name="Test Callback",
    )
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET status='reacted' WHERE id=?", (pid,))
    did = db.enqueue_draft(pid, kind, body)
    return pid, did


def _make_daemon(cfg):
    """Build a BotDaemon with a FakeTelegramClient (replaces real TG client)."""
    from linkedin_agent.bot_daemon import BotDaemon
    from linkedin_agent import bot_daemon as bot_daemon_mod
    from linkedin_agent.adapters import get_adapter

    # Daemon's __init__ constructs a TelegramClient internally — patch it out
    # by monkeypatching the class reference before construction.
    fake_tg = FakeTelegramClient(cfg)
    bot_daemon_mod.TelegramClient = lambda c: fake_tg

    daemon = BotDaemon(cfg)
    return daemon, fake_tg


def _callback_event(action: str, payload: int, message_id: int = 100, chat_id: int = 12345) -> dict:
    return {
        "id": "cb_query_id",
        "data": f"{action}:{payload}",
        "message": {"message_id": message_id, "chat": {"id": chat_id}},
    }


# ===== retry path ==========================================================

@pytest.mark.integration
def test_retry_callback_routes_to_approve_path(db_env, monkeypatch):
    """A retry:<draft_id> callback walks the same code path as approve.
    Previously this resolved to 'Unknown action'."""
    from linkedin_agent import db
    monkeypatch.setenv("LINKEDIN_FAKE_WINDOW", "open")   # so _approve actually sends
    cfg = _make_cfg(dry_run=True)
    pid, did = _seed_prospect_and_draft("connect_note", "hand-written body for retry test that is long enough")
    daemon, fake_tg = _make_daemon(cfg)

    try:
        daemon._dispatch({"callback_query": _callback_event("retry", did)})
    finally:
        daemon.close()

    # Draft should now be 'sent' (dry_run path through send_draft_via_adapter)
    assert db.get_draft(did)["status"] == "sent"
    # And the callback was answered (not silently dropped as "Unknown action")
    answers = [a.text for a in fake_tg.callback_answers]
    assert all("Unknown action" not in (a or "") for a in answers)


# ===== giveup path =========================================================

@pytest.mark.integration
def test_giveup_callback_routes_to_reject_path(db_env):
    """giveup:<draft_id> walks the same path as reject — marks draft rejected
    and updates the Telegram message."""
    from linkedin_agent import db
    cfg = _make_cfg()
    pid, did = _seed_prospect_and_draft("connect_note", "body text here for the giveup path that is long enough")
    daemon, fake_tg = _make_daemon(cfg)

    try:
        daemon._dispatch({"callback_query": _callback_event("giveup", did)})
    finally:
        daemon.close()

    draft = db.get_draft(did)
    assert draft["status"] == "rejected"
    assert draft["reject_reason"] == "user_rejected"
    # The Telegram card should be marked rejected (strikethrough text)
    assert len(fake_tg.marked_rejected) == 1


# ===== idempotency for retry/giveup ========================================

@pytest.mark.integration
def test_retry_on_already_sent_is_noop(db_env):
    """Retrying a draft that already shipped should be a no-op, not a re-send."""
    from linkedin_agent import db
    cfg = _make_cfg()
    pid, did = _seed_prospect_and_draft("connect_note", "body text for the already-sent retry test that is long enough")
    db.set_draft_status(did, "sent")

    daemon, fake_tg = _make_daemon(cfg)
    try:
        daemon._dispatch({"callback_query": _callback_event("retry", did)})
    finally:
        daemon.close()

    # No additional send action happened
    answer = fake_tg.callback_answers[0].text or ""
    assert "Already sent" in answer
    # Draft still in 'sent' state — wasn't reverted or re-processed
    assert db.get_draft(did)["status"] == "sent"


# ===== reply kind =========================================================

@pytest.mark.integration
def test_send_via_adapter_reply_routes_to_dm_keeps_status_replied(db_env, monkeypatch):
    """Reply drafts go out as DMs but: status stays 'replied' (we're in active
    conversation, not the initial outreach funnel), dm_count is NOT bumped
    (replies don't trigger follow-up scheduling), and the action is logged
    under 'reply_sent' not 'dm'."""
    from linkedin_agent import db
    from linkedin_agent.bot_daemon import send_draft_via_adapter
    from linkedin_agent.adapters import get_adapter

    monkeypatch.setenv("LINKEDIN_FAKE_WINDOW", "open")
    cfg = _make_cfg(dry_run=True)

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/reply-test",
        full_name="Reply Test",
    )
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET status='replied', dm_count=1 WHERE id=?", (pid,))
    did = db.enqueue_draft(pid, "reply", "Glad you're open — here's a follow-up question.")
    draft = db.get_draft(did)

    adapter = get_adapter(cfg)
    try:
        send_draft_via_adapter(cfg, adapter, draft, source="test")
    finally:
        adapter.close()

    refreshed = db.get_prospect(pid)
    assert refreshed["status"] == "replied", "reply send must NOT change status from 'replied'"
    assert refreshed["dm_count"] == 1, "reply send must NOT bump dm_count (only sequence DMs bump)"
    assert db.get_draft(did)["status"] == "sent"

    # Outbound message recorded in thread for context on future replies
    with db.connect() as conn:
        cur = conn.execute(
            "SELECT direction, body FROM messages WHERE prospect_id=? ORDER BY sent_at",
            (pid,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    assert any(r["direction"] == "outbound" and "Glad you're open" in r["body"] for r in rows)


@pytest.mark.integration
def test_send_via_adapter_reply_respects_dm_cap(db_env, monkeypatch):
    """Replies count against the DM cap, not connect."""
    from linkedin_agent import db
    from linkedin_agent import safety
    from linkedin_agent.bot_daemon import send_draft_via_adapter
    from linkedin_agent.adapters import get_adapter

    monkeypatch.setenv("LINKEDIN_FAKE_WINDOW", "open")
    cfg = _make_cfg(daily_max_dms=0, dry_run=True)

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/reply-cap-test",
        full_name="Reply Cap Test",
    )
    did = db.enqueue_draft(pid, "reply", "Reply body that should hit the DM cap.")
    draft = db.get_draft(did)

    adapter = get_adapter(cfg)
    try:
        with pytest.raises(safety.RateLimitExceeded):
            send_draft_via_adapter(cfg, adapter, draft, source="test")
    finally:
        adapter.close()

    # Draft should NOT be marked sent — cap rejection happens before the
    # status transition.
    assert db.get_draft(did)["status"] == "pending"


# ===== mark_draft_error signature ==========================================

@pytest.mark.unit
def test_mark_draft_error_callbacks_carry_draft_id():
    """Buttons rendered by mark_draft_error must encode draft_id (not the
    Telegram message_id) in their callback_data so the daemon can route them."""
    fake_tg = FakeTelegramClient(_make_cfg())
    fake_tg.mark_draft_error(
        message_id=999,                # Telegram message id (NOT the right thing to route on)
        original_body="body here",
        error="something broke",
        draft_id=42,                    # this is what should be in callback_data
    )
    # FakeTelegramClient records edits via .edits — last edit should carry the right keyboard
    last_edit = fake_tg.edits[-1]
    keyboard = last_edit.reply_markup
    assert keyboard is not None
    buttons = keyboard["inline_keyboard"][0]
    cb_values = [b["callback_data"] for b in buttons]
    assert "retry:42" in cb_values
    assert "giveup:42" in cb_values
    # And NOT the message_id (which was 999)
    assert "retry:999" not in cb_values
