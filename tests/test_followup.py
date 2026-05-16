"""Tests for the follow-up scheduler.

Unit tests verify the pure-logic candidate predicates against frozen time.
Integration tests exercise run_followup_cycle against a real SQLite (temp)
with stubbed drafter + FakeTelegramClient.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time

from linkedin_agent import followup


# ----- helpers --------------------------------------------------------------

def _make_row(**overrides):
    """Build a dict that quacks like a sqlite3.Row for the predicate functions."""
    base = {
        "id": 1,
        "status": "dm_sent",
        "dm_count": 0,
        "last_dm_at": None,
        "disposition": None,
    }
    base.update(overrides)
    return base


# ===== unit: predicates =====================================================

NOW = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.unit
@pytest.mark.parametrize("days_ago,expected", [
    (0, False),     # just sent
    (3, False),     # 1 day shy
    (4, True),      # at the threshold
    (10, True),
])
def test_is_dm2_due_at_4d_boundary(days_ago, expected):
    last = (NOW - timedelta(days=days_ago)).isoformat()
    row = _make_row(dm_count=1, last_dm_at=last)
    assert followup.is_dm2_due(row, NOW) is expected


@pytest.mark.unit
@pytest.mark.parametrize("days_ago,expected", [
    (4, False),     # DM2 timing, not DM3
    (10, False),    # 1 day shy
    (11, True),     # at threshold
    (30, True),
])
def test_is_dm3_due_at_11d_boundary(days_ago, expected):
    last = (NOW - timedelta(days=days_ago)).isoformat()
    row = _make_row(dm_count=2, last_dm_at=last)
    assert followup.is_dm3_due(row, NOW) is expected


@pytest.mark.unit
@pytest.mark.parametrize("days_ago,expected", [
    (10, False),
    (13, False),
    (14, True),
    (30, True),
])
def test_is_ghost_candidate_at_14d(days_ago, expected):
    last = (NOW - timedelta(days=days_ago)).isoformat()
    row = _make_row(dm_count=3, last_dm_at=last)
    assert followup.is_ghost_candidate(row, NOW) is expected


@pytest.mark.unit
def test_reply_halts_followup_at_dm2_timing():
    last = (NOW - timedelta(days=10)).isoformat()
    row = _make_row(dm_count=1, last_dm_at=last, status="replied")
    assert followup.is_dm2_due(row, NOW) is False
    assert followup.is_dm3_due(row, NOW) is False


@pytest.mark.unit
def test_ghost_skipped_if_already_dispositioned():
    last = (NOW - timedelta(days=30)).isoformat()
    row = _make_row(dm_count=3, last_dm_at=last, disposition="not_fit")
    assert followup.is_ghost_candidate(row, NOW) is False


@pytest.mark.unit
def test_no_last_dm_at_means_not_a_candidate():
    row = _make_row(dm_count=1, last_dm_at=None)
    assert followup.is_dm2_due(row, NOW) is False


# ===== integration: run_followup_cycle ======================================


def _seed_prospect(db, **kwargs):
    """Insert a prospect and set fields the predicates depend on."""
    import sqlite3
    defaults = {
        "linkedin_url": kwargs.pop("linkedin_url", f"https://www.linkedin.com/in/test-{kwargs.get('dm_count', 0)}"),
        "full_name": kwargs.pop("full_name", "Test Prospect"),
    }
    pid = db.upsert_prospect(**defaults)
    # Directly set fields not supported by upsert_prospect
    with db.connect() as conn:
        if "status" in kwargs:
            conn.execute("UPDATE prospects SET status=? WHERE id=?", (kwargs["status"], pid))
        if "dm_count" in kwargs:
            conn.execute("UPDATE prospects SET dm_count=? WHERE id=?", (kwargs["dm_count"], pid))
        if "last_dm_at" in kwargs:
            conn.execute("UPDATE prospects SET last_dm_at=? WHERE id=?", (kwargs["last_dm_at"], pid))
        if "disposition" in kwargs:
            conn.execute("UPDATE prospects SET disposition=? WHERE id=?", (kwargs["disposition"], pid))
    return pid


@pytest.mark.integration
def test_run_cycle_drafts_and_enqueues_dm2(db_env, fake_telegram):
    from linkedin_agent import db
    pid = _seed_prospect(
        db,
        status="dm_sent",
        dm_count=1,
        last_dm_at=(NOW - timedelta(days=5)).isoformat(),
    )
    cfg = type("C", (), {})()   # minimal stand-in; run_followup_cycle doesn't read cfg fields
    stub_drafter = lambda kind, prospect_id: f"stubbed {kind} body"

    result = followup.run_followup_cycle(cfg, drafter=stub_drafter, telegram=fake_telegram, now=NOW)

    assert result.dm2_enqueued == 1
    assert result.dm3_enqueued == 0
    drafts = db.list_pending_drafts()
    assert len(drafts) == 1
    assert drafts[0]["kind"] == "dm2"
    assert drafts[0]["body"] == "stubbed dm2 body"
    assert len(fake_telegram.drafts_pushed) == 1
    assert fake_telegram.drafts_pushed[0].kind == "dm2"


@pytest.mark.integration
def test_run_cycle_auto_ghosts_stale(db_env, fake_telegram):
    from linkedin_agent import db
    pid = _seed_prospect(
        db,
        status="dm_sent",
        dm_count=3,
        last_dm_at=(NOW - timedelta(days=15)).isoformat(),
    )
    cfg = type("C", (), {})()
    stub_drafter = lambda kind, prospect_id: "should not be called"

    result = followup.run_followup_cycle(cfg, drafter=stub_drafter, telegram=fake_telegram, now=NOW)

    assert result.ghosted == 1
    assert result.dm2_enqueued == 0
    assert result.dm3_enqueued == 0
    refreshed = db.get_prospect(pid)
    assert refreshed["disposition"] == "ghosted"


@pytest.mark.integration
def test_run_cycle_skips_already_drafted(db_env, fake_telegram):
    from linkedin_agent import db
    pid = _seed_prospect(
        db,
        status="dm_sent",
        dm_count=1,
        last_dm_at=(NOW - timedelta(days=5)).isoformat(),
    )
    # Existing pending draft for the same prospect+kind — must not be duplicated.
    db.enqueue_draft(pid, "dm2", "existing draft")

    cfg = type("C", (), {})()
    drafter_calls: list[str] = []
    def stub_drafter(kind, prospect_id):
        drafter_calls.append(kind)
        return "fresh draft"

    result = followup.run_followup_cycle(cfg, drafter=stub_drafter, telegram=fake_telegram, now=NOW)

    assert result.drafts_skipped_existing == 1
    assert result.dm2_enqueued == 0
    assert drafter_calls == []   # never invoked because we skipped early
    assert len(db.list_pending_drafts()) == 1   # the original one


@pytest.mark.integration
def test_run_cycle_dm3_preferred_when_both_could_apply(db_env, fake_telegram):
    """If timing somehow matched both DM2 and DM3 (dm_count=2 and old enough for
    DM3), the cycle should pick DM3."""
    from linkedin_agent import db
    pid = _seed_prospect(
        db,
        status="dm_sent",
        dm_count=2,
        last_dm_at=(NOW - timedelta(days=12)).isoformat(),
    )
    cfg = type("C", (), {})()
    result = followup.run_followup_cycle(
        cfg, drafter=lambda k, p: f"{k} body", telegram=fake_telegram, now=NOW,
    )
    assert result.dm3_enqueued == 1
    assert result.dm2_enqueued == 0


@pytest.mark.integration
def test_run_cycle_drafter_failure_is_recorded(db_env, fake_telegram):
    from linkedin_agent import db
    pid = _seed_prospect(
        db,
        status="dm_sent",
        dm_count=1,
        last_dm_at=(NOW - timedelta(days=5)).isoformat(),
    )
    cfg = type("C", (), {})()
    def boom(kind, prospect_id):
        raise RuntimeError("drafter exploded")

    result = followup.run_followup_cycle(cfg, drafter=boom, telegram=fake_telegram, now=NOW)

    assert result.drafts_failed == 1
    assert result.dm2_enqueued == 0
    assert len(db.list_pending_drafts()) == 0
    assert len(fake_telegram.drafts_pushed) == 0
