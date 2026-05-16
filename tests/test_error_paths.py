"""Error-path tests with respx — mocks Unipile / Telegram HTTP failures
and asserts the system degrades gracefully.

These catch the kind of bugs you only see in production: Unipile returns
500 mid-cycle, Telegram is rate-limited, network resets at the wrong
moment. Running these continuously means we know the failure modes work
before they show up in a real cron run.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from linkedin_agent.config import Config


def _cfg(**overrides) -> Config:
    base = {
        "backend": "unipile",
        "unipile_api_key": "fake-key",
        "unipile_account_id": "FVEbOtmdTUy0Sh-e6PSkew",
        "unipile_dsn": "api21.unipile.com:15165",
        "telegram_bot_token": "test-token",
        "telegram_chat_id": 12345,
        "daily_max_reactions": 30,
        "daily_max_connections": 20,
        "daily_max_dms": 10,
        "daily_max_searches": 50,
        "action_delay_min": 0,
        "action_delay_max": 0,
        "dry_run": False,
        "playwright_state_path": None,
    }
    base.update(overrides)
    return type("CFG", (), base)()


# ===== Unipile adapter errors ===============================================

@pytest.mark.integration
@respx.mock
def test_unipile_search_handles_500():
    """A 500 from Unipile during search must surface as an exception (callers
    catch + log). Critically: doesn't crash the adapter or leave it in a
    broken state for the next call."""
    from linkedin_agent.adapters.unipile_adapter import UnipileAdapter
    cfg = _cfg()
    respx.post("https://api21.unipile.com:15165/api/v1/linkedin/search").mock(
        return_value=httpx.Response(500, json={"error": "server fire"})
    )
    adapter = UnipileAdapter(cfg)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            adapter.search("fintech founder", limit=3)
    finally:
        adapter.close()


@pytest.mark.integration
@respx.mock
def test_unipile_search_handles_429():
    """Rate limit response — same handling as 5xx, exception bubbles."""
    from linkedin_agent.adapters.unipile_adapter import UnipileAdapter
    cfg = _cfg()
    respx.post("https://api21.unipile.com:15165/api/v1/linkedin/search").mock(
        return_value=httpx.Response(429, json={"error": "rate limited", "retry_after": 60})
    )
    adapter = UnipileAdapter(cfg)
    try:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            adapter.search("test", limit=3)
        assert exc_info.value.response.status_code == 429
    finally:
        adapter.close()


@pytest.mark.integration
@respx.mock
def test_unipile_search_handles_connect_timeout():
    """Network failure (connect timeout) raises httpx.ConnectError — caller
    should catch generically."""
    from linkedin_agent.adapters.unipile_adapter import UnipileAdapter
    cfg = _cfg()
    respx.post("https://api21.unipile.com:15165/api/v1/linkedin/search").mock(
        side_effect=httpx.ConnectTimeout("connect timed out")
    )
    adapter = UnipileAdapter(cfg)
    try:
        with pytest.raises(httpx.ConnectTimeout):
            adapter.search("test", limit=3)
    finally:
        adapter.close()


@pytest.mark.integration
@respx.mock
def test_unipile_send_connection_handles_422():
    """422 from invite (e.g., target profile locked) raises — daemon's
    _approve catches it and shows mark_draft_error in Telegram."""
    from linkedin_agent.adapters.unipile_adapter import UnipileAdapter
    cfg = _cfg()
    respx.get("https://api21.unipile.com:15165/api/v1/users/test-slug").mock(
        return_value=httpx.Response(
            200,
            json={"provider_id": "ACoTEST", "public_identifier": "test-slug"},
        )
    )
    respx.post("https://api21.unipile.com:15165/api/v1/users/invite").mock(
        return_value=httpx.Response(
            422,
            json={"error": "errors/invalid_recipient",
                  "detail": "profile is locked"},
        )
    )
    adapter = UnipileAdapter(cfg)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            adapter.send_connection("https://www.linkedin.com/in/test-slug",
                                    note="hello there")
    finally:
        adapter.close()


# ===== daily.py resilience under Unipile failure ============================

@pytest.mark.integration
@respx.mock
def test_daily_continues_after_unipile_search_500(db_env, fake_telegram):
    """daily.poll catches errors and records them — does NOT crash the
    rest of the daily cycle (react/connect/dm steps for OTHER prospects
    should still run)."""
    import sqlite3
    from linkedin_agent import db, daily as daily_mod

    # Mock Unipile /messages to always 500 (poll will fail).
    respx.get("https://api21.unipile.com:15165/api/v1/messages").mock(
        return_value=httpx.Response(500, json={"error": "down"})
    )

    cfg = _cfg()
    # Use the fake adapter for the LinkedIn operations (search/react/connect/dm)
    # — only poll uses the raw httpx client which respx intercepts.
    from linkedin_agent.adapters import get_adapter
    cfg.backend = "fake"
    adapter = get_adapter(cfg)

    try:
        result = daily_mod.run_daily(
            cfg, adapter=adapter, telegram=fake_telegram,
            drafter=lambda k, p, recent_posts=None: f"stub {k}",
        )
    finally:
        adapter.close()

    # The poll step recorded an error but daily completed
    assert any("poll" in err for err in result.errors)
    # daily_completed still got logged
    conn = sqlite3.connect(db_env["LINKEDIN_DB_PATH"])
    row = conn.execute("SELECT result FROM actions WHERE kind='daily_completed'").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "ok"


# ===== Telegram failures ====================================================

@pytest.mark.integration
@respx.mock
def test_telegram_send_message_500_raises():
    """Telegram returning 500 during sendMessage raises TelegramError."""
    from linkedin_agent.telegram import TelegramClient, TelegramError
    cfg = _cfg()
    respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "error_code": 500,
                                               "description": "telegram is down"})
    )
    tg = TelegramClient(cfg)
    try:
        with pytest.raises(TelegramError, match="telegram is down"):
            tg.send_message("hello")
    finally:
        tg.close()


@pytest.mark.integration
@respx.mock
def test_telegram_send_message_network_error_raises():
    """Network-level failure during Telegram send."""
    from linkedin_agent.telegram import TelegramClient, TelegramError
    cfg = _cfg()
    respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
        side_effect=httpx.ConnectError("network down")
    )
    tg = TelegramClient(cfg)
    try:
        with pytest.raises(httpx.ConnectError):
            tg.send_message("hello")
    finally:
        tg.close()


@pytest.mark.integration
@respx.mock
def test_poll_handles_telegram_notification_failure_gracefully(db_env):
    """If polling found new inbound but Telegram is down, the inbound is
    still recorded in DB — Telegram failure doesn't roll back the work."""
    import sqlite3
    from linkedin_agent import db, poll as poll_mod

    # Seed a prospect we'll receive an inbound from
    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/notify-test",
        full_name="Notify Test",
        provider_id="ACoTESTNOTIFY",
    )

    # Unipile returns one new inbound message
    respx.get("https://api21.unipile.com:15165/api/v1/messages").mock(
        return_value=httpx.Response(200, json={
            "items": [{
                "id": "msg-external-1",
                "is_sender": False,
                "sender_id": "ACoTESTNOTIFY",
                "text": "Hi, thanks for the connect.",
                "chat_id": "chat-1",
            }],
            "cursor": None,
        })
    )
    # Telegram is down
    respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
        return_value=httpx.Response(503, json={"ok": False, "description": "telegram down"})
    )

    cfg = _cfg()
    result = poll_mod.poll_once(cfg, notify=True)

    # Inbound was recorded in DB despite Telegram failure
    assert result.new_inbound == 1
    conn = sqlite3.connect(db_env["LINKEDIN_DB_PATH"])
    rows = conn.execute(
        "SELECT direction, body FROM messages WHERE prospect_id=?", (pid,)
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "inbound"
    assert rows[0][1] == "Hi, thanks for the connect."
    # Prospect status was updated
    refreshed = db.get_prospect(pid)
    assert refreshed["status"] == "replied"


# ===== adapter clean state after failure ====================================

@pytest.mark.integration
@respx.mock
def test_adapter_usable_after_failure():
    """After a failed call, the adapter must still work for subsequent
    successful calls — no broken-state cascade."""
    from linkedin_agent.adapters.unipile_adapter import UnipileAdapter
    cfg = _cfg()

    # First call: 500 (will raise)
    route1 = respx.post("https://api21.unipile.com:15165/api/v1/linkedin/search").mock(
        return_value=httpx.Response(500)
    )
    adapter = UnipileAdapter(cfg)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            adapter.search("test", limit=1)

        # Switch the mock to return success
        route1.mock(return_value=httpx.Response(200, json={
            "items": [{
                "id": "ACoFRESH",
                "public_identifier": "fresh-test",
                "name": "Fresh Test",
                "headline": "Founder",
                "location": "Test City",
            }],
        }))

        # Second call: succeeds, adapter is still usable
        hits = adapter.search("test", limit=1)
        assert len(hits) == 1
        assert hits[0].full_name == "Fresh Test"
    finally:
        adapter.close()
