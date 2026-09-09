"""Tests for the connection-acceptance detection flow.

Retargeted when Unipile was removed. These previously mocked Unipile's
/users/{id} endpoint with respx; the assertions were always about pipeline
behaviour — 1st-degree flips a prospect to 'connected', anything else leaves
it alone — so they now run against a fake provider through the capability
router instead of a transport that no longer exists.


When someone accepts our connection invite, LinkedIn doesn't surface it via
the messages endpoint — so the cron has to actively poll profile-distance
for every prospect in `connection_sent` status. This file covers:

  • The transition rule (FIRST_DEGREE → flip to 'connected')
  • The no-op rule (still 2nd/3rd-degree → stay 'connection_sent')
  • The daily.py wire-in (an accept detected in the same run feeds DM1)
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeProvider, fake_router


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
def test_check_acceptances_flips_first_degree_to_connected(db_env):
    """A prospect whose API response now shows FIRST_DEGREE moves to 'connected'."""
    from linkedin_agent import db, enrichment
    pid = _seed_connection_sent(provider_id="ACoFIRSTDEG")

    provider = FakeProvider(accepted=True)

    result = enrichment.check_acceptances(_cfg(), router=fake_router(provider))

    assert result.detected == 1
    assert result.still_pending == 0
    assert db.get_prospect(pid)["status"] == "connected"


@pytest.mark.integration
def test_check_acceptances_leaves_still_pending_alone(db_env):
    """A prospect still at 2nd-degree stays in 'connection_sent'."""
    from linkedin_agent import db, enrichment
    pid = _seed_connection_sent(provider_id="ACoSTILLPND")

    provider = FakeProvider(accepted=False)

    result = enrichment.check_acceptances(_cfg(), router=fake_router(provider))

    assert result.detected == 0
    assert result.still_pending == 1
    assert db.get_prospect(pid)["status"] == "connection_sent"


@pytest.mark.integration
def test_check_acceptances_skips_prospects_without_provider_id(db_env):
    provider = FakeProvider(accepted=True)
    """No provider_id → can't look them up → skip silently (don't error)."""
    from linkedin_agent import db, enrichment
    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/no-provider",
        full_name="No Provider",
    )
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET status='connection_sent' WHERE id=?", (pid,))

    # No mocks set — if we tried to fetch, respx would fail.
    result = enrichment.check_acceptances(_cfg(), router=fake_router(provider))

    assert result.detected == 0
    assert result.still_pending == 0
    assert result.errors == 0


@pytest.mark.integration
def test_check_acceptances_logs_accept_detected_action(db_env):
    """Detected acceptance writes an 'accept_detected' row in the action log."""
    from linkedin_agent import db, enrichment
    pid = _seed_connection_sent(provider_id="ACoLOGTEST")

    provider = FakeProvider(accepted=True)

    enrichment.check_acceptances(_cfg(), router=fake_router(provider))

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT kind, result FROM actions WHERE prospect_id=? AND kind='accept_detected'",
            (pid,),
        ).fetchall()
    assert len(rows) == 1


# ===== daily.py integration ================================================

@pytest.mark.integration
def test_daily_runs_acceptance_check_and_drafts_dm1_same_cycle(db_env, fake_telegram, monkeypatch):
    """End-to-end: a prospect in connection_sent gets detected as accepted,
    flipped to 'connected', and the SAME daily run drafts a DM1 for them."""
    from linkedin_agent import daily as daily_mod, db
    from linkedin_agent.adapters import get_adapter
    pid = _seed_connection_sent(provider_id="ACoDAILYINT")

    # The fake backend serves the adapter; the router (built inside daily)
    # serves the acceptance check, which the fake provider answers.
    provider = FakeProvider(accepted=True)
    for target in ("linkedin_agent.providers.build_router",
                   "linkedin_agent.enrichment.build_router"):
        monkeypatch.setattr(target, lambda cfg, **kw: fake_router(provider))

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


# ===== per-cycle budget ====================================================

@pytest.mark.integration
def test_daily_bounds_how_many_invites_it_checks(db_env, fake_telegram, monkeypatch):
    """An hourly cron must be able to finish.

    Under Unipile an acceptance check was one ~30ms GET, so the call was made
    unbounded. PhantomBuster answers the same question with a full profile
    re-scrape — a container launch, a browser boot, wait() up to 300s, plus
    result-settle retries. Twenty pending invites then meant twenty Phantom
    launches inside one tick, and the run could not finish before the next one
    started.

    Nothing is lost, only deferred: a detected accept leaves the queue, so
    successive ticks work through the backlog.
    """
    from linkedin_agent import daily as daily_mod, db
    from linkedin_agent.adapters import get_adapter

    for n in range(10):
        pid = db.upsert_prospect(
            linkedin_url=f"https://www.linkedin.com/in/budget-{n}",
            full_name=f"Budget {n}",
            provider_id=f"ACoBUDGET{n:04d}",
        )
        with db.connect() as conn:
            conn.execute("UPDATE prospects SET status='connection_sent' WHERE id=?",
                         (pid,))

    checked: list[str] = []

    class CountingProvider(FakeProvider):
        def check_acceptance(self, identifier):
            checked.append(identifier)
            return False        # nobody accepts, so none leave the queue

    provider = CountingProvider()
    for target in ("linkedin_agent.providers.build_router",
                   "linkedin_agent.enrichment.build_router"):
        monkeypatch.setattr(target, lambda cfg, **kw: fake_router(provider))

    cfg = _cfg()
    adapter = get_adapter(cfg)
    try:
        daily_mod.run_daily(cfg, adapter=adapter, telegram=fake_telegram,
                            drafter=lambda *a, **k: "unused")
    finally:
        adapter.close()

    assert len(checked) == daily_mod._ACCEPTANCE_CHECK_BUDGET
    assert len(checked) < 10, "the whole queue was checked in one tick"


@pytest.mark.unit
def test_acceptance_budget_is_configurable_and_can_be_lifted(monkeypatch):
    """A faster provider should not be held to a slow provider's ceiling."""
    from linkedin_agent import daily as daily_mod

    monkeypatch.delenv("DAILY_MAX_ACCEPTANCE_CHECKS", raising=False)
    assert daily_mod._acceptance_check_budget() == daily_mod._ACCEPTANCE_CHECK_BUDGET

    monkeypatch.setenv("DAILY_MAX_ACCEPTANCE_CHECKS", "25")
    assert daily_mod._acceptance_check_budget() == 25

    monkeypatch.setenv("DAILY_MAX_ACCEPTANCE_CHECKS", "0")
    assert daily_mod._acceptance_check_budget() is None, "0 means unbounded"

    monkeypatch.setenv("DAILY_MAX_ACCEPTANCE_CHECKS", "not-a-number")
    assert daily_mod._acceptance_check_budget() == daily_mod._ACCEPTANCE_CHECK_BUDGET


@pytest.mark.integration
def test_the_budget_rotates_instead_of_re_checking_one_fixed_head(db_env, monkeypatch):
    """A bound that always checks the same prospects is a cut, not a defer.

    The first version sliced db.list_prospects, which orders by last_action_at
    DESC NULLS LAST. A still-pending check writes no action, so the ordering
    was identical every tick: the same head was re-checked hourly and the tail
    was never reached. Worse, DESC put the NEWEST invites first — the ones
    least likely to have been accepted yet.
    """
    from linkedin_agent import db, enrichment

    for n in range(9):
        pid = db.upsert_prospect(
            linkedin_url=f"https://www.linkedin.com/in/rotate-{n}",
            full_name=f"Rotate {n}",
            provider_id=f"ACoROTATE{n:04d}",
        )
        with db.connect() as conn:
            conn.execute("UPDATE prospects SET status='connection_sent' WHERE id=?",
                         (pid,))

    seen: list[list[str]] = []

    class CountingProvider(FakeProvider):
        def check_acceptance(self, identifier):
            seen[-1].append(identifier)
            return False        # nobody accepts, so nobody leaves the queue

    for _ in range(3):
        seen.append([])
        enrichment.check_acceptances(_cfg(), limit=3,
                                     router=fake_router(CountingProvider()))

    assert [len(batch) for batch in seen] == [3, 3, 3]
    assert not (set(seen[0]) & set(seen[1])), "tick 2 re-checked tick 1's batch"
    assert not (set(seen[1]) & set(seen[2])), "tick 3 re-checked tick 2's batch"
    # Three ticks of three covers all nine — nothing is starved.
    assert len(set(seen[0]) | set(seen[1]) | set(seen[2])) == 9


@pytest.mark.integration
def test_a_pending_check_is_stamped_so_it_moves_to_the_tail(db_env):
    """The rotation rests on stamping every check, not just the ones that
    detect an accept. Stamping only on success recreates the starvation."""
    from linkedin_agent import db, enrichment

    pid = _seed_connection_sent(provider_id="ACoSTAMPED")
    assert db.get_prospect(pid)["acceptance_checked_at"] is None

    enrichment.check_acceptances(_cfg(), router=fake_router(FakeProvider(accepted=False)))

    row = db.get_prospect(pid)
    assert row["status"] == "connection_sent", "still pending, correctly"
    assert row["acceptance_checked_at"] is not None, "a pending check must stamp"
