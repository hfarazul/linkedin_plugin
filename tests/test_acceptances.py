"""Tests for the connection-acceptance detection flow.

When someone accepts our connection invite, LinkedIn doesn't surface it via
the messages endpoint — so the cron has to actively poll profile-distance
for every prospect in `connection_sent` status. This file covers:

  • The transition rule (FIRST_DEGREE → flip to 'connected')
  • The no-op rule (still 2nd/3rd-degree → stay 'connection_sent')
  • The daily.py wire-in (an accept detected in the same run feeds DM1)
"""

from __future__ import annotations

import httpx
import pytest
import respx


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
        "telegram_bot_token": None,
        "telegram_chat_id": None,
        "playwright_state_path": None,
    }
    base.update(overrides)
    return type("CFG", (), base)()


def _seed_connection_sent(provider_id="ACoTEST123"):
    from linkedin_agent import db
    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/accept-test",
        full_name="Accept Test",
        provider_id=provider_id,
    )
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET status='connection_sent' WHERE id=?", (pid,))
    return pid


# ===== detection rule ======================================================

@pytest.mark.integration
@respx.mock
def test_check_acceptances_flips_first_degree_to_connected(db_env):
    """A prospect whose API response now shows FIRST_DEGREE moves to 'connected'."""
    from linkedin_agent import db, enrichment
    pid = _seed_connection_sent(provider_id="ACoFIRSTDEG")

    respx.get("https://api21.unipile.com:15165/api/v1/users/ACoFIRSTDEG").mock(
        return_value=httpx.Response(200, json={
            "network_distance": "FIRST_DEGREE",
            "headline": "Test", "follower_count": 100,
        })
    )

    result = enrichment.check_acceptances(_cfg())

    assert result.detected == 1
    assert result.still_pending == 0
    assert db.get_prospect(pid)["status"] == "connected"


@pytest.mark.integration
@respx.mock
def test_check_acceptances_leaves_still_pending_alone(db_env):
    """A prospect still at 2nd-degree stays in 'connection_sent'."""
    from linkedin_agent import db, enrichment
    pid = _seed_connection_sent(provider_id="ACoSTILLPND")

    respx.get("https://api21.unipile.com:15165/api/v1/users/ACoSTILLPND").mock(
        return_value=httpx.Response(200, json={
            "network_distance": "SECOND_DEGREE",
            "headline": "Test",
        })
    )

    result = enrichment.check_acceptances(_cfg())

    assert result.detected == 0
    assert result.still_pending == 1
    assert db.get_prospect(pid)["status"] == "connection_sent"


@pytest.mark.integration
@respx.mock
def test_check_acceptances_skips_prospects_without_provider_id(db_env):
    """No provider_id → can't look them up → skip silently (don't error)."""
    from linkedin_agent import db, enrichment
    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/no-provider",
        full_name="No Provider",
    )
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET status='connection_sent' WHERE id=?", (pid,))

    # No mocks set — if we tried to fetch, respx would fail.
    result = enrichment.check_acceptances(_cfg())

    assert result.detected == 0
    assert result.still_pending == 0
    assert result.errors == 0


@pytest.mark.integration
@respx.mock
def test_check_acceptances_logs_accept_detected_action(db_env):
    """Detected acceptance writes an 'accept_detected' row in the action log."""
    from linkedin_agent import db, enrichment
    pid = _seed_connection_sent(provider_id="ACoLOGTEST")

    respx.get("https://api21.unipile.com:15165/api/v1/users/ACoLOGTEST").mock(
        return_value=httpx.Response(200, json={"network_distance": "FIRST_DEGREE"})
    )

    enrichment.check_acceptances(_cfg())

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT kind, result FROM actions WHERE prospect_id=? AND kind='accept_detected'",
            (pid,),
        ).fetchall()
    assert len(rows) == 1


# ===== daily.py integration ================================================

@pytest.mark.integration
@respx.mock
def test_daily_runs_acceptance_check_and_drafts_dm1_same_cycle(db_env, fake_telegram):
    """End-to-end: a prospect in connection_sent gets detected as accepted,
    flipped to 'connected', and the SAME daily run drafts a DM1 for them."""
    from linkedin_agent import daily as daily_mod, db
    from linkedin_agent.adapters import get_adapter
    pid = _seed_connection_sent(provider_id="ACoDAILYINT")

    respx.get("https://api21.unipile.com:15165/api/v1/users/ACoDAILYINT").mock(
        return_value=httpx.Response(200, json={"network_distance": "FIRST_DEGREE"})
    )
    respx.get("https://api21.unipile.com:15165/api/v1/messages").mock(
        return_value=httpx.Response(200, json={"items": [], "cursor": None})
    )

    def stub_drafter(kind, prospect_id, recent_posts=None):
        return f"stub-{kind} body that meets the minimum length for a draft, padded with extra words to clear the 350-char DM1 minimum. " * 4

    cfg = _cfg()
    adapter = get_adapter(cfg)
    try:
        result = daily_mod.run_daily(
            cfg, adapter=adapter, telegram=fake_telegram, drafter=stub_drafter,
        )
    finally:
        adapter.close()

    assert result.accepts_detected == 1
    refreshed = db.get_prospect(pid)
    assert refreshed["status"] == "connected"
    # AND the dm1 step ran in the same cycle (no waiting for next cron)
    assert result.dm1_drafts == 1
