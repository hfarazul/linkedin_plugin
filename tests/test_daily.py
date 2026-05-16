"""Tests for daily orchestration.

The daily run sequences ~7 sub-steps and respects rate caps. These integration
tests use the in-process API (not subprocess) so we can inject the fake
adapter, fake Telegram, and a stubbed drafter directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from linkedin_agent import daily as daily_mod


# Stable drafter stub — every test that needs a draft body uses this.
def _stub_drafter(kind, prospect_id, recent_posts=None):
    return f"stub-{kind}-{prospect_id}"


def _make_cfg(**overrides):
    """Minimal Config-like object. daily.py only reads cap fields + backend creds."""
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
        "telegram_bot_token": None,
        "telegram_chat_id": None,
        "playwright_state_path": None,
    }
    base.update(overrides)
    return type("CFG", (), base)()


def _seed_prospect(status, *, dm_count=0, last_dm_at=None, linkedin_url=None):
    """Insert a prospect at a specific stage. Returns id."""
    from linkedin_agent import db
    url = linkedin_url or f"https://www.linkedin.com/in/test-{status}-{dm_count}"
    pid = db.upsert_prospect(linkedin_url=url, full_name=f"Test {status}")
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET status=? WHERE id=?", (status, pid))
        if dm_count:
            conn.execute("UPDATE prospects SET dm_count=? WHERE id=?", (dm_count, pid))
        if last_dm_at:
            conn.execute("UPDATE prospects SET last_dm_at=? WHERE id=?", (last_dm_at, pid))
    return pid


# ===== tests =================================================================

@pytest.mark.integration
def test_daily_empty_db(db_env, fake_telegram):
    """No prospects → everything is zero, no errors, no Telegram drafts."""
    from linkedin_agent.adapters import get_adapter
    cfg = _make_cfg()
    adapter = get_adapter(cfg)
    try:
        result = daily_mod.run_daily(
            cfg, adapter=adapter, telegram=fake_telegram, drafter=_stub_drafter,
        )
    finally:
        adapter.close()
    assert result.reactions_sent == 0
    assert result.connect_drafts == 0
    assert result.dm1_drafts == 0
    assert result.errors == []
    assert fake_telegram.drafts_pushed == []


@pytest.mark.integration
def test_daily_reacts_to_targeted_prospects(db_env, fake_telegram, monkeypatch):
    """targeted prospects with posts get reacted to → status flips to 'reacted'."""
    from linkedin_agent import db
    from linkedin_agent.adapters import get_adapter
    pid = _seed_prospect("targeted")
    cfg = _make_cfg()

    # Force send window open so the optional step-7 flush logic is exercised
    monkeypatch.setenv("LINKEDIN_FAKE_WINDOW", "open")

    adapter = get_adapter(cfg)
    try:
        result = daily_mod.run_daily(
            cfg, adapter=adapter, telegram=fake_telegram, drafter=_stub_drafter,
        )
    finally:
        adapter.close()

    assert result.reactions_sent == 1
    refreshed = db.get_prospect(pid)
    assert refreshed["status"] == "reacted"


@pytest.mark.integration
def test_daily_drafts_connect_for_reacted(db_env, fake_telegram):
    """reacted prospects get a connect_note draft → enqueued, pushed to fake Telegram."""
    from linkedin_agent import db
    from linkedin_agent.adapters import get_adapter
    pid = _seed_prospect("reacted")
    cfg = _make_cfg()
    adapter = get_adapter(cfg)
    try:
        result = daily_mod.run_daily(
            cfg, adapter=adapter, telegram=fake_telegram, drafter=_stub_drafter,
        )
    finally:
        adapter.close()

    assert result.connect_drafts == 1
    drafts = db.list_pending_drafts()
    assert any(d["kind"] == "connect_note" for d in drafts)
    assert any(d.kind == "connect_note" for d in fake_telegram.drafts_pushed)


@pytest.mark.integration
def test_daily_drafts_dm1_for_connected(db_env, fake_telegram):
    """connected prospects with dm_count==0 get a dm1 draft."""
    from linkedin_agent import db
    from linkedin_agent.adapters import get_adapter
    pid = _seed_prospect("connected")
    cfg = _make_cfg()
    adapter = get_adapter(cfg)
    try:
        result = daily_mod.run_daily(
            cfg, adapter=adapter, telegram=fake_telegram, drafter=_stub_drafter,
        )
    finally:
        adapter.close()

    assert result.dm1_drafts == 1
    drafts = db.list_pending_drafts()
    assert any(d["kind"] == "dm1" for d in drafts)


@pytest.mark.integration
def test_daily_respects_react_cap(db_env, fake_telegram):
    """With react cap=1, only 1 of 3 targeted prospects gets reacted."""
    from linkedin_agent import db
    from linkedin_agent.adapters import get_adapter

    _seed_prospect("targeted", linkedin_url="https://www.linkedin.com/in/test-a")
    _seed_prospect("targeted", linkedin_url="https://www.linkedin.com/in/test-b")
    _seed_prospect("targeted", linkedin_url="https://www.linkedin.com/in/test-c")

    cfg = _make_cfg(daily_max_reactions=1)
    adapter = get_adapter(cfg)
    try:
        result = daily_mod.run_daily(
            cfg, adapter=adapter, telegram=fake_telegram, drafter=_stub_drafter,
        )
    finally:
        adapter.close()

    assert result.reactions_sent == 1
    assert "react" in result.skipped_cap_hit
    reacted = db.list_prospects(status="reacted")
    assert len(reacted) == 1


@pytest.mark.integration
def test_daily_idempotent_within_caps(db_env, fake_telegram):
    """Running daily twice in a row doesn't duplicate drafts for the same prospect."""
    from linkedin_agent import db
    from linkedin_agent.adapters import get_adapter
    _seed_prospect("connected")
    cfg = _make_cfg()

    adapter = get_adapter(cfg)
    try:
        result1 = daily_mod.run_daily(cfg, adapter=adapter, telegram=fake_telegram, drafter=_stub_drafter)
        result2 = daily_mod.run_daily(cfg, adapter=adapter, telegram=fake_telegram, drafter=_stub_drafter)
    finally:
        adapter.close()

    assert result1.dm1_drafts == 1
    assert result2.dm1_drafts == 0   # already drafted, skip
    assert len(db.list_pending_drafts()) == 1


@pytest.mark.integration
def test_daily_full_chain(db_env, fake_telegram):
    """Mixed-stage DB: every step does its job, no cross-contamination."""
    from linkedin_agent import db
    from linkedin_agent.adapters import get_adapter
    from datetime import datetime, timedelta, timezone

    five_days_ago = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()

    _seed_prospect("targeted",  linkedin_url="https://www.linkedin.com/in/p1")
    _seed_prospect("reacted",   linkedin_url="https://www.linkedin.com/in/p2")
    _seed_prospect("connected", linkedin_url="https://www.linkedin.com/in/p3")
    _seed_prospect("dm_sent",   linkedin_url="https://www.linkedin.com/in/p4",
                   dm_count=1, last_dm_at=five_days_ago)

    cfg = _make_cfg()
    adapter = get_adapter(cfg)
    try:
        result = daily_mod.run_daily(cfg, adapter=adapter, telegram=fake_telegram, drafter=_stub_drafter)
    finally:
        adapter.close()

    # p1 reacts in step 3; daily cascades — once reacted, p1 also qualifies
    # for connect drafting in step 4 alongside the originally-reacted p2.
    assert result.reactions_sent == 1
    assert result.connect_drafts == 2     # p1 (just-reacted) + p2 (was reacted)
    assert result.dm1_drafts == 1         # p3
    assert result.dm2_drafts == 1         # p4
    assert len(db.list_pending_drafts()) == 4
    assert len(fake_telegram.drafts_pushed) == 4
