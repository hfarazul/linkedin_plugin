"""P1-3 — signals and evidence as first-class records.

Signals record WHY we contacted someone; evidence records HOW WE KNOW. Both
are append-only, because a message already sent must stay explainable by
exactly the facts that were true when it went out.

The migration tests matter more than they look: these tables land on a
production SQLite file that already holds live outreach state, so init_db()
has to stay idempotent and existing rows must keep their meaning.
"""

from __future__ import annotations

import sqlite3

import pytest


def _columns(db, table: str) -> set[str]:
    with db.connect() as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(db) -> set[str]:
    with db.connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}


def _prospect(db) -> int:
    return db.upsert_prospect(linkedin_url="https://www.linkedin.com/in/probe-1",
                              full_name="Probe One")


# ----------------------------- schema ----------------------------------------

@pytest.mark.integration
def test_signals_and_evidence_tables_exist(db_env) -> None:
    from linkedin_agent import db
    assert {"signals", "evidence"} <= _tables(db)


@pytest.mark.integration
def test_signals_has_expected_columns(db_env) -> None:
    from linkedin_agent import db
    assert {
        "id", "prospect_id", "kind", "status", "confidence",
        "detected_at", "occurred_at", "expires_at", "payload", "dedup_key",
    } <= _columns(db, "signals")


@pytest.mark.integration
def test_evidence_has_expected_columns(db_env) -> None:
    from linkedin_agent import db
    assert {
        "id", "signal_id", "claim", "source_type",
        "source_url", "raw_excerpt", "fetched_at", "checksum",
    } <= _columns(db, "evidence")


@pytest.mark.integration
def test_init_db_is_idempotent(db_env) -> None:
    """Runs against a live production DB — must survive repeated calls."""
    from linkedin_agent import db
    db.init_db()
    db.init_db()
    assert {"signals", "evidence"} <= _tables(db)


# ----------------------------- create_signal ---------------------------------

@pytest.mark.integration
def test_create_signal_returns_id(db_env) -> None:
    from linkedin_agent import db
    sid = db.create_signal(_prospect(db), "transition_to_founder", "high", "dk-1")
    assert isinstance(sid, int)
    assert db.get_signal(sid)["kind"] == "transition_to_founder"


@pytest.mark.integration
def test_duplicate_dedup_key_returns_none_not_exception(db_env) -> None:
    """The idempotency guarantee: re-scouting someone we already know about
    must be a quiet skip, not a crash and not a duplicate draft."""
    from linkedin_agent import db
    pid = _prospect(db)
    first = db.create_signal(pid, "transition_to_founder", "high", "dk-same")
    second = db.create_signal(pid, "transition_to_founder", "high", "dk-same")
    assert isinstance(first, int)
    assert second is None
    assert len(db.list_signals()) == 1


@pytest.mark.integration
def test_collision_can_be_traced_to_the_existing_signal(db_env) -> None:
    """After a collision the caller should be able to say WHICH signal already
    covers the event, so the CLI can report 'already detected as #12'."""
    from linkedin_agent import db
    pid = _prospect(db)
    sid = db.create_signal(pid, "transition_bigco_to_startup", "high", "dk-trace")
    assert db.create_signal(pid, "transition_bigco_to_startup", "high", "dk-trace") is None
    assert db.get_signal_by_dedup_key("dk-trace")["id"] == sid


@pytest.mark.integration
def test_create_signal_rejects_bad_confidence(db_env) -> None:
    from linkedin_agent import db
    with pytest.raises(ValueError, match="confidence"):
        db.create_signal(_prospect(db), "k", "very-sure", "dk-bad-conf")


@pytest.mark.integration
def test_create_signal_rejects_bad_status(db_env) -> None:
    from linkedin_agent import db
    with pytest.raises(ValueError, match="status"):
        db.create_signal(_prospect(db), "k", "high", "dk-bad-status", status="maybe")


@pytest.mark.integration
def test_signal_status_transitions(db_env) -> None:
    from linkedin_agent import db
    sid = db.create_signal(_prospect(db), "k", "high", "dk-status")
    assert db.get_signal(sid)["status"] == "new"
    db.set_signal_status(sid, "qualified")
    assert db.get_signal(sid)["status"] == "qualified"
    with pytest.raises(ValueError):
        db.set_signal_status(sid, "nonsense")


@pytest.mark.integration
def test_signal_cascades_on_prospect_delete(db_env) -> None:
    from linkedin_agent import db
    pid = _prospect(db)
    db.create_signal(pid, "k", "high", "dk-cascade")
    with db.connect() as conn:
        conn.execute("DELETE FROM prospects WHERE id = ?", (pid,))
    assert db.list_signals() == []


# ----------------------------- evidence --------------------------------------

@pytest.mark.integration
def test_add_evidence_and_read_back(db_env) -> None:
    from linkedin_agent import db
    sid = db.create_signal(_prospect(db), "transition_to_founder", "high", "dk-ev")
    db.add_evidence(
        sid, claim="Started as Founder at Acme in March 2026",
        source_type="linkedin_position",
        source_url="https://www.linkedin.com/in/probe-1",
        raw_excerpt="Founder, Acme - Mar 2026 to Present",
    )
    rows = db.list_evidence(sid)
    assert len(rows) == 1
    assert rows[0]["claim"].startswith("Started as Founder")
    assert rows[0]["source_type"] == "linkedin_position"
    assert rows[0]["fetched_at"]


@pytest.mark.integration
def test_evidence_checksum_tracks_the_excerpt(db_env) -> None:
    """Same text -> same checksum; changed text -> different. That is what
    lets us notice later that a source moved under a claim we already sent."""
    from linkedin_agent import db
    sid = db.create_signal(_prospect(db), "k", "high", "dk-sum")
    a = db.add_evidence(sid, claim="c", source_type="t", raw_excerpt="identical text")
    b = db.add_evidence(sid, claim="c", source_type="t", raw_excerpt="identical text")
    c = db.add_evidence(sid, claim="c", source_type="t", raw_excerpt="different text")
    by_id = {r["id"]: r for r in db.list_evidence(sid)}
    assert by_id[a]["checksum"] == by_id[b]["checksum"]
    assert by_id[a]["checksum"] != by_id[c]["checksum"]


@pytest.mark.integration
def test_evidence_without_excerpt_has_no_checksum(db_env) -> None:
    from linkedin_agent import db
    sid = db.create_signal(_prospect(db), "k", "high", "dk-nosum")
    eid = db.add_evidence(sid, claim="c", source_type="t")
    assert db.list_evidence(sid)[0]["checksum"] is None
    assert isinstance(eid, int)


@pytest.mark.integration
def test_evidence_is_append_only(db_env) -> None:
    """Guard against a future 'convenience' edit helper: evidence must not be
    mutable, or a sent message stops being explainable by its own record."""
    from linkedin_agent import db
    assert not hasattr(db, "update_evidence")
    assert not hasattr(db, "delete_evidence")


@pytest.mark.integration
def test_get_signal_with_evidence(db_env) -> None:
    from linkedin_agent import db
    sid = db.create_signal(_prospect(db), "k", "medium", "dk-bundle")
    db.add_evidence(sid, claim="one", source_type="t")
    db.add_evidence(sid, claim="two", source_type="t")
    signal, evidence = db.get_signal_with_evidence(sid)
    assert signal["id"] == sid
    assert [e["claim"] for e in evidence] == ["one", "two"]


@pytest.mark.integration
def test_get_signal_with_evidence_missing_returns_none(db_env) -> None:
    from linkedin_agent import db
    assert db.get_signal_with_evidence(9999) is None


# ----------------------------- list_signals ----------------------------------

@pytest.mark.integration
def test_list_signals_filters(db_env) -> None:
    from linkedin_agent import db
    pid = _prospect(db)
    other = db.upsert_prospect(linkedin_url="https://www.linkedin.com/in/probe-2")
    db.create_signal(pid, "transition_to_founder", "high", "dk-a")
    db.create_signal(pid, "transition_bigco_to_startup", "medium", "dk-b", status="qualified")
    db.create_signal(other, "transition_to_founder", "high", "dk-c")

    assert len(db.list_signals()) == 3
    assert len(db.list_signals(kind="transition_to_founder")) == 2
    assert len(db.list_signals(status="qualified")) == 1
    assert len(db.list_signals(prospect_id=other)) == 1
    assert len(db.list_signals(kind="transition_to_founder", prospect_id=pid)) == 1
