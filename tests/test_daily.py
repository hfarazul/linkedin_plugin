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
def test_daily_reacts_to_targeted_prospects(db_env, fake_telegram):
    """targeted prospects with posts get reacted to → status flips to 'reacted'."""
    from linkedin_agent import db
    from linkedin_agent.adapters import get_adapter
    pid = _seed_prospect("targeted")
    cfg = _make_cfg()

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
def test_daily_passes_recent_posts_to_drafter(db_env, fake_telegram):
    """daily must fetch the prospect's recent posts and feed them to the drafter,
    or the drafter has nothing specific to reference and returns INSUFFICIENT_CONTEXT."""
    from linkedin_agent import db
    from linkedin_agent.adapters import get_adapter
    pid = _seed_prospect("reacted")
    cfg = _make_cfg()

    captured = []
    def asserting_drafter(kind, prospect_id, recent_posts=None):
        captured.append({"kind": kind, "prospect_id": prospect_id, "posts": recent_posts})
        return f"stub-{kind}"

    adapter = get_adapter(cfg)
    try:
        daily_mod.run_daily(cfg, adapter=adapter, telegram=fake_telegram, drafter=asserting_drafter)
    finally:
        adapter.close()

    # Find the connect_note draft call and verify posts came along
    connect_calls = [c for c in captured if c["kind"] == "connect_note"]
    assert len(connect_calls) == 1
    assert connect_calls[0]["posts"], "drafter called without recent_posts — daily.py regression"


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
def test_daily_react_respects_dry_run(db_env, fake_telegram):
    """DRY_RUN must propagate to the daily react step — state advances but
    no LinkedIn write happens and the action log marks dry_run=True."""
    import sqlite3
    from linkedin_agent import db
    from linkedin_agent.adapters import get_adapter

    pid = _seed_prospect("targeted", linkedin_url="https://www.linkedin.com/in/test-dry")
    cfg = _make_cfg(dry_run=True)

    react_calls = []
    adapter = get_adapter(cfg)
    # Wrap adapter.react to detect any real call (there should be none).
    real_react = adapter.react
    def counting_react(*args, **kwargs):
        react_calls.append((args, kwargs))
        return real_react(*args, **kwargs)
    adapter.react = counting_react

    try:
        result = daily_mod.run_daily(
            cfg, adapter=adapter, telegram=fake_telegram, drafter=_stub_drafter,
        )
    finally:
        adapter.close()

    assert result.reactions_sent == 1
    assert react_calls == []        # the only assertion that matters
    refreshed = db.get_prospect(pid)
    assert refreshed["status"] == "reacted"   # state still advanced
    # action log records dry_run=True
    conn = sqlite3.connect(db_env["LINKEDIN_DB_PATH"])
    rows = conn.execute("SELECT result, dry_run FROM actions WHERE kind='react'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "dry_run"
    assert rows[0][1] == 1


@pytest.mark.integration
def test_send_draft_via_adapter_respects_dry_run(db_env):
    """DRY_RUN must propagate to send_draft_via_adapter — used by both bot
    daemon (approval) and send-approved CLI."""
    import sqlite3
    from linkedin_agent import db
    from linkedin_agent.adapters import get_adapter
    from linkedin_agent.bot_daemon import send_draft_via_adapter

    pid = _seed_prospect("connected", linkedin_url="https://www.linkedin.com/in/test-send-dry")
    draft_id = db.enqueue_draft(pid, "dm1", "test body content for dry run path")
    db.set_draft_status(draft_id, "approved")

    cfg = _make_cfg(dry_run=True)
    adapter = get_adapter(cfg)
    dm_calls = []
    real_send_dm = adapter.send_dm
    def counting_send_dm(*args, **kwargs):
        dm_calls.append((args, kwargs))
        return real_send_dm(*args, **kwargs)
    adapter.send_dm = counting_send_dm

    try:
        draft = db.get_draft(draft_id)
        send_draft_via_adapter(cfg, adapter, draft, source="test")
    finally:
        adapter.close()

    assert dm_calls == []                       # no LinkedIn write
    refreshed = db.get_prospect(pid)
    assert refreshed["status"] == "dm_sent"     # state advanced
    assert refreshed["dm_count"] == 1           # follow-up tracking still ticked
    draft = db.get_draft(draft_id)
    assert draft["status"] == "sent"
    # log marks dry_run=True
    conn = sqlite3.connect(db_env["LINKEDIN_DB_PATH"])
    row = conn.execute(
        "SELECT result, dry_run FROM actions WHERE kind='dm' AND prospect_id=?",
        (pid,),
    ).fetchone()
    conn.close()
    assert row[0] == "dry_run"
    assert row[1] == 1


@pytest.mark.integration
def test_daily_reacts_regardless_of_window(db_env, fake_telegram, monkeypatch):
    """Reactions are intentionally NOT window-gated — they're low-stakes
    LIKEs and the daily cron's own schedule is the practical envelope.
    Verify reactions fire even when the send window is closed."""
    from linkedin_agent import db
    from linkedin_agent.adapters import get_adapter

    pid = _seed_prospect("targeted", linkedin_url="https://www.linkedin.com/in/test-weekend")
    monkeypatch.setenv("LINKEDIN_FAKE_WINDOW", "closed")
    cfg = _make_cfg()

    adapter = get_adapter(cfg)
    try:
        result = daily_mod.run_daily(
            cfg, adapter=adapter, telegram=fake_telegram, drafter=_stub_drafter,
        )
    finally:
        adapter.close()

    assert result.reactions_sent == 1
    assert "react" not in result.skipped_window_steps
    refreshed = db.get_prospect(pid)
    assert refreshed["status"] == "reacted"   # advanced even though window=closed


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
