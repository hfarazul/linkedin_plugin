"""PB-7 / P1-2 — enrichment routed through the capability layer.

Two behaviours here are new and worth pinning:

1. Work history is persisted to `positions`, which is what the transition
   detectors will read. Detection then becomes a pure function over stored
   rows, so re-running it after a rule change costs no scraping budget.

2. Enrichment writes only the fields a provider actually returned. The old
   single-provider code overwrote every column unconditionally, which was
   harmless with one provider and destructive with two: PhantomBuster does not
   return `pronoun` or `is_creator`, so a blanket write would erase whatever
   Unipile had previously stored for the same prospect.
"""

from __future__ import annotations

import pytest

from linkedin_agent.providers.capabilities import (
    Capability,
    Position,
    ProfileFacts,
)


class FakeProvider:
    """Stands in for either vendor. Records what was asked of it."""

    def __init__(self, name: str, facts: ProfileFacts | None,
                 *, accepted: bool | None = None) -> None:
        self.name = name
        self._facts = facts
        self._accepted = accepted
        self.calls: list[tuple] = []

    def supports(self, capability: Capability) -> bool:
        return True

    def fetch_profile(self, identifier, *, with_experience=False):
        self.calls.append(("fetch_profile", identifier, with_experience))
        return self._facts

    def check_acceptance(self, identifier):
        self.calls.append(("check_acceptance", identifier))
        return self._accepted

    def close(self) -> None:
        self.calls.append(("close",))


def _router(provider):
    from linkedin_agent.providers.router import CapabilityRouter
    return CapabilityRouter(provider, None)


def _cfg():
    return type("CFG", (), {
        "unipile_api_key": None, "unipile_account_id": None,
        "unipile_dsn": None, "dry_run": False,
    })()


def _seed(db, provider_id="ACoENRICH1"):
    return db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/enrich-test",
        full_name="Enrich Test", provider_id=provider_id)


FACTS = ProfileFacts(
    provider_id="ACoENRICH1",
    headline="Software Engineer",
    location="Bengaluru",
    network_distance="2nd",
    follower_count=512,
    company_employee_count=42,
    positions=[
        Position(company="TalkingLands", title="Software Engineer",
                 start_date="2026-02-01", is_current=True,
                 date_precision="month", source="phantombuster"),
        Position(company="dan Lab", title="Co-Founder",
                 start_date="2025-08-01", is_current=True,
                 date_precision="month", source="phantombuster"),
    ],
    source="phantombuster",
)


# ----------------------------- positions persistence -------------------------

@pytest.mark.integration
def test_enrich_persists_positions(db_env) -> None:
    from linkedin_agent import db, enrichment
    pid = _seed(db)
    assert enrichment.enrich(_cfg(), pid, router=_router(FakeProvider("pb", FACTS)))

    rows = db.list_positions(pid)
    assert len(rows) == 2
    assert rows[0]["company"] == "TalkingLands"      # newest first
    assert rows[0]["started_at"] == "2026-02-01"
    assert rows[0]["date_precision"] == "month"
    assert rows[0]["is_current"] == 1
    assert rows[0]["source"] == "phantombuster"


@pytest.mark.integration
def test_enrich_requests_experience(db_env) -> None:
    """Both providers return history on the same call as the profile, so
    asking for it separately would spend scraping budget for nothing."""
    from linkedin_agent import db, enrichment
    provider = FakeProvider("pb", FACTS)
    enrichment.enrich(_cfg(), _seed(db), router=_router(provider))
    assert provider.calls[0] == ("fetch_profile", "ACoENRICH1", True)


@pytest.mark.integration
def test_re_enriching_replaces_rather_than_duplicates(db_env) -> None:
    """A profile is a snapshot. A removed role must not linger for a detector
    to reason from."""
    from linkedin_agent import db, enrichment
    pid = _seed(db)
    router = _router(FakeProvider("pb", FACTS))
    enrichment.enrich(_cfg(), pid, router=router)
    enrichment.enrich(_cfg(), pid, router=router)
    assert len(db.list_positions(pid)) == 2

    fewer = ProfileFacts(provider_id="ACoENRICH1",
                         positions=[FACTS.positions[0]], source="pb")
    enrichment.enrich(_cfg(), pid, router=_router(FakeProvider("pb", fewer)))
    remaining = db.list_positions(pid)
    assert len(remaining) == 1
    assert remaining[0]["company"] == "TalkingLands"


@pytest.mark.integration
def test_positions_without_dates_sort_last(db_env) -> None:
    from linkedin_agent import db, enrichment
    pid = _seed(db)
    facts = ProfileFacts(provider_id="ACoENRICH1", positions=[
        Position(company="Undated", title="Advisor"),
        Position(company="Dated", title="CEO", start_date="2024-01-01"),
    ], source="pb")
    enrichment.enrich(_cfg(), pid, router=_router(FakeProvider("pb", facts)))
    assert [r["company"] for r in db.list_positions(pid)] == ["Dated", "Undated"]


# ----------------------------- partial field updates -------------------------

@pytest.mark.integration
def test_absent_fields_are_not_overwritten(db_env) -> None:
    """The multi-provider hazard. Unipile stores `pronoun`; PhantomBuster does
    not return it. Enriching via PhantomBuster must not erase it."""
    from linkedin_agent import db, enrichment
    pid = _seed(db)
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET pronoun = 'She/Her' WHERE id = ?", (pid,))

    # FACTS has pronoun=None, as PhantomBuster would.
    enrichment.enrich(_cfg(), pid, router=_router(FakeProvider("pb", FACTS)))

    row = db.get_prospect(pid)
    assert row["pronoun"] == "She/Her", "a provider gap must not erase known data"
    assert row["headline"] == "Software Engineer", "returned fields still update"


@pytest.mark.integration
def test_enrich_records_the_provider_in_the_audit_log(db_env) -> None:
    """Which provider produced a row has to be recoverable later."""
    import json
    from linkedin_agent import db, enrichment
    pid = _seed(db)
    enrichment.enrich(_cfg(), pid, router=_router(FakeProvider("pb", FACTS)))
    with db.connect() as conn:
        row = conn.execute(
            "SELECT payload FROM actions WHERE kind='enrich' AND prospect_id=?",
            (pid,)).fetchone()
    payload = json.loads(row["payload"])
    assert payload["provider"] == "phantombuster"
    assert payload["positions"] == 2


# ----------------------------- failure paths ---------------------------------

@pytest.mark.integration
def test_profile_not_found_returns_false(db_env) -> None:
    from linkedin_agent import db, enrichment
    pid = _seed(db)
    assert enrichment.enrich(_cfg(), pid, router=_router(FakeProvider("pb", None))) \
        is False
    assert db.list_positions(pid) == []


@pytest.mark.integration
def test_prospect_without_provider_id_is_skipped(db_env) -> None:
    from linkedin_agent import db, enrichment
    pid = db.upsert_prospect(linkedin_url="https://www.linkedin.com/in/no-id")
    provider = FakeProvider("pb", FACTS)
    assert enrichment.enrich(_cfg(), pid, router=_router(provider)) is False
    assert provider.calls == [], "must not spend a provider call"


@pytest.mark.integration
def test_missing_prospect_raises(db_env) -> None:
    from linkedin_agent import enrichment
    with pytest.raises(ValueError):
        enrichment.enrich(_cfg(), 9999, router=_router(FakeProvider("pb", FACTS)))


# ----------------------------- acceptance check ------------------------------

@pytest.mark.integration
def test_acceptance_check_routes_through_capability(db_env) -> None:
    """Unipile reads network_distance, PhantomBuster reads connectionDegree.
    This function must not know which."""
    from linkedin_agent import db, enrichment
    pid = _seed(db, provider_id="ACoACCEPT1")
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET status='connection_sent' WHERE id=?", (pid,))

    provider = FakeProvider("pb", None, accepted=True)
    result = enrichment.check_acceptances(_cfg(), router=_router(provider))

    assert result.detected == 1
    assert db.get_prospect(pid)["status"] == "connected"


@pytest.mark.integration
def test_acceptance_check_leaves_pending_alone(db_env) -> None:
    from linkedin_agent import db, enrichment
    pid = _seed(db, provider_id="ACoPENDING1")
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET status='connection_sent' WHERE id=?", (pid,))

    result = enrichment.check_acceptances(
        _cfg(), router=_router(FakeProvider("pb", None, accepted=False)))

    assert result.detected == 0
    assert result.still_pending == 1
    assert db.get_prospect(pid)["status"] == "connection_sent"


@pytest.mark.integration
def test_acceptance_unknown_counts_as_error_not_rejection(db_env) -> None:
    """None means "could not determine" — treating it as "not accepted" would
    silently strand prospects in connection_sent forever."""
    from linkedin_agent import db, enrichment
    pid = _seed(db, provider_id="ACoUNKNOWN1")
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET status='connection_sent' WHERE id=?", (pid,))

    result = enrichment.check_acceptances(
        _cfg(), router=_router(FakeProvider("pb", None, accepted=None)))

    assert result.errors == 1
    assert result.still_pending == 0
