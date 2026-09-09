"""The evidence engine reaching the live drafter path.

Before this branch, `build_evidence` had exactly one caller —
`scripts/smoke_e2e.py`. Every production path (`daily`, `followup`, `poll`)
called the drafter with `evidence=None`, and with no evidence
`drafter.draft()` sets `enforce_pain_gate = kind.startswith("email")` and
`thin_evidence = False`. So on the live DM path the pain-claim gate and the
inferred-relevance gate were both inactive: the two checks that stop us
asserting a stranger has a problem we invented.

What matters here is not that `build_evidence` gets called. It is that the
same object reaches `drafter.draft`, that the gates it governs actually fire,
and that when evidence is thin the system gets MORE careful rather than
finding another reason to write.
"""

from __future__ import annotations

import pytest

from linkedin_agent import drafter as d
from linkedin_agent.providers.capabilities import Position
from tests.fakes import FakeProvider, FakeTelegramClient, fake_router


HIRING_POST = {"text": "We're hiring a senior engineer to help us ship faster.",
               "posted_at": "2026-09-01T10:00:00Z"}
PLAIN_POST = {"text": "Enjoyed the conference in Lisbon last week. Good crowd.",
              "posted_at": "2026-09-01T10:00:00Z"}


def _cfg(**overrides):
    base = {
        "backend": "fake", "daily_max_reactions": 30,
        "daily_max_connections": 20, "daily_max_dms": 10,
        "daily_max_searches": 50, "action_delay_min": 0, "action_delay_max": 0,
        "dry_run": False, "unipile_api_key": None, "unipile_account_id": None,
        "unipile_dsn": None, "telegram_bot_token": "t", "telegram_chat_id": 1,
        "playwright_state_path": None,
    }
    base.update(overrides)
    return type("CFG", (), base)()


def _seed(db, *, status="reacted", positions=True, **cols):
    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/evidence-wiring",
        full_name="Dana Reed", company="NowCorp", headline="Founder at NowCorp",
        provider_id="ACoEVIDENCE0001",
    )
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET status=? WHERE id=?", (status, pid))
        for col, val in cols.items():
            conn.execute(f"UPDATE prospects SET {col}=? WHERE id=?", (val, pid))
    if positions:
        db.replace_positions(pid, [
            Position(company="NowCorp", title="Founder", start_date="2025-06-01",
                     end_date=None, is_current=True, date_precision="month"),
        ])
    return pid


# ===================== the payload reaches the drafter =====================

@pytest.mark.integration
def test_daily_passes_evidence_into_the_drafter(db_env, fake_telegram):
    """daily -> evidence -> drafter, for both kinds it drafts."""
    from linkedin_agent import daily as daily_mod, db
    from linkedin_agent.adapters import get_adapter

    _seed(db, status="reacted")
    seen = {}

    def capturing(kind, prospect_id, recent_posts=None, evidence=None):
        seen[kind] = evidence
        return "x" * 400

    cfg = _cfg()
    adapter = get_adapter(cfg)
    try:
        daily_mod.run_daily(cfg, adapter=adapter, telegram=fake_telegram,
                            drafter=capturing)
    finally:
        adapter.close()

    assert "connect_note" in seen
    payload = seen["connect_note"]
    assert payload is not None, "daily drafted with no evidence at all"
    assert "tier" in payload and "pain_claim_licensed" in payload
    assert "unknowns" in payload


@pytest.mark.integration
def test_followup_passes_evidence_into_the_drafter(db_env, fake_telegram):
    """followup -> evidence -> drafter, for dm2 and dm3."""
    from datetime import datetime, timedelta, timezone
    from linkedin_agent import db, followup

    now = datetime.now(timezone.utc)
    pid = _seed(db, status="dm_sent", dm_count=1,
                last_dm_at=(now - timedelta(days=5)).isoformat())
    seen = {}

    def capturing(kind, prospect_id, evidence=None):
        seen[kind] = evidence
        return f"{kind} body long enough to pass"

    result = followup.run_followup_cycle(_cfg(), drafter=capturing,
                                         telegram=fake_telegram, now=now)

    assert result.dm2_enqueued == 1
    assert seen["dm2"] is not None
    assert seen["dm2"]["tier"] in ("strong", "moderate", "weak", "none")


@pytest.mark.integration
def test_poll_passes_evidence_into_the_reply_drafter(db_env, monkeypatch):
    """poll -> evidence -> reply drafter."""
    from linkedin_agent import db, poll as poll_mod
    from linkedin_agent.providers.capabilities import InboundMessage

    pid = _seed(db, status="dm_sent")
    db.record_message(pid, "outbound", "our dm1")
    seen = {}

    def capturing(kind, prospect_id, evidence=None):
        seen[kind] = evidence
        return "a reply body that is long enough to clear the floor"

    provider = FakeProvider(inbox=[InboundMessage(
        external_id="m1", prospect_provider_id="ACoEVIDENCE0001",
        body="Thanks — we're rebuilding our reporting by hand right now.",
        sent_at=None, thread_id="t1", is_from_me=False, source="phantombuster")])
    monkeypatch.setattr(poll_mod, "TelegramClient",
                        lambda c: FakeTelegramClient(_cfg()))

    poll_mod.poll_once(_cfg(), router=fake_router(provider), notify=True,
                       drafter=capturing)

    assert seen.get("reply") is not None, "poll drafted a reply with no evidence"


# ===================== the gates it governs actually fire ==================

def _drive_real_drafter(monkeypatch, db, pid, kind, body, evidence):
    """Run the REAL drafter against a scripted model output."""
    monkeypatch.setattr(d, "_invoke_claude", lambda p, timeout=90: body)
    tries: list = []
    return d.draft(kind, pid, evidence=evidence, attempts_out=tries), tries


# Clean on every other gate, but asserts a problem nothing supports.
INVENTS_PAIN = (
    "Dana,\n\nYou're probably wrestling with the same reporting problem every "
    "founder at this stage runs into, and it tends to eat a whole quarter "
    "before anyone notices.\n\nI'm Haque, co-founder of Cortivo. We pair a "
    "senior engineer with AI tooling so a founder can ship without hiring in "
    "house first.\n\nWould that be useful on your side, or is it not where "
    "your head is at right now?\n\nBest,\nHaque\nCortivo")


@pytest.mark.integration
def test_the_pain_gate_is_inactive_on_a_dm_without_evidence(db_env, monkeypatch):
    """The behaviour this branch exists to change — pinned so the change is
    visible rather than asserted."""
    from linkedin_agent import db
    pid = _seed(db)

    body, tries = _drive_real_drafter(monkeypatch, db, pid, "dm1",
                                      INVENTS_PAIN, None)

    assert body == INVENTS_PAIN, "expected the ungated pre-wiring behaviour"
    assert tries[-1].outcome == "accepted"


@pytest.mark.integration
def test_the_pain_gate_fires_on_a_dm_once_evidence_is_wired(db_env, monkeypatch):
    """Same draft, same prospect, evidence attached: now rejected."""
    from linkedin_agent import db, evidence_context
    pid = _seed(db)

    evidence = evidence_context.build_for("dm1", pid, recent_posts=[])
    assert evidence["pain_claim_licensed"] is False

    with pytest.raises(d.DrafterError):
        _drive_real_drafter(monkeypatch, db, pid, "dm1", INVENTS_PAIN, evidence)


@pytest.mark.integration
def test_the_inferred_relevance_gate_fires_at_the_thin_tier(db_env, monkeypatch):
    """Reasoning a problem from a job title, which is the softer version of the
    same invention and was equally unguarded on the DM path."""
    from linkedin_agent import db, evidence_context
    pid = _seed(db)

    infers = (
        "Dana,\n\nRunning a company at this size is a setting where that kind "
        "of work tends to matter, so I thought it worth a note.\n\nI'm Haque, "
        "co-founder of Cortivo. We pair a senior engineer with AI tooling so a "
        "founder can ship without hiring a full team first.\n\nWould that be "
        "relevant on your side?\n\nBest,\nHaque\nCortivo")

    evidence = evidence_context.build_for("dm1", pid, recent_posts=[])
    assert evidence["tier"] == "weak"

    with pytest.raises(d.DrafterError):
        _drive_real_drafter(monkeypatch, db, pid, "dm1", infers, evidence)


# ===================== tier behaviour =====================================

@pytest.mark.integration
def test_a_published_signal_licenses_a_claim(db_env):
    """strong: they said something implying a problem, in their own words."""
    from linkedin_agent import db, evidence_context
    pid = _seed(db)

    payload = evidence_context.build_for("dm1", pid, recent_posts=[HIRING_POST])

    assert payload["tier"] == "strong"
    assert payload["pain_claim_licensed"] is True
    assert payload["licensed_claims"]


@pytest.mark.integration
def test_a_post_without_a_signal_licenses_nothing(db_env):
    """moderate: reference what they wrote, diagnose nothing."""
    from linkedin_agent import db, evidence_context
    pid = _seed(db)

    payload = evidence_context.build_for("dm1", pid, recent_posts=[PLAIN_POST])

    assert payload["tier"] == "moderate"
    assert payload["pain_claim_licensed"] is False
    assert payload["observations"]


@pytest.mark.integration
def test_role_and_company_alone_are_the_weak_tier(db_env):
    """weak: one verified fact, a plain introduction, an ask. Correct, not lazy."""
    from linkedin_agent import db, evidence_context
    pid = _seed(db)

    payload = evidence_context.build_for("dm1", pid, recent_posts=[])

    assert payload["tier"] == "weak"
    assert payload["pain_claim_licensed"] is False
    assert any("no posts retrieved" in u for u in payload["unknowns"])


@pytest.mark.integration
def test_nothing_at_all_is_the_none_tier(db_env):
    """none: the drafter is expected to return INSUFFICIENT_CONTEXT."""
    from linkedin_agent import db, evidence_context
    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/blank-prospect")

    payload = evidence_context.build_for("dm1", pid, recent_posts=[])

    assert payload["tier"] == "none"
    assert payload["pain_claim_licensed"] is False


# ===================== absence must not loosen anything ====================

@pytest.mark.integration
def test_a_missing_prospect_yields_no_evidence_rather_than_a_guess(db_env):
    from linkedin_agent import evidence_context
    assert evidence_context.build_for("dm1", 99999) is None


@pytest.mark.integration
def test_a_failure_to_build_evidence_never_kills_the_run(db_env, monkeypatch):
    """The callers are cron steps iterating prospects. Degrading to no evidence
    is the conservative path — draft() fails closed for email kinds — but the
    step must survive."""
    from linkedin_agent import db, evidence_context
    pid = _seed(db)

    monkeypatch.setattr(evidence_context, "facts_for",
                        lambda _pid: (_ for _ in ()).throw(RuntimeError("boom")))
    assert evidence_context.build_safely("dm1", pid) is None


@pytest.mark.integration
def test_email_still_fails_closed_when_evidence_is_absent(db_env, monkeypatch):
    """The pre-existing fail-closed rule for email kinds is untouched."""
    from linkedin_agent import db
    pid = _seed(db)

    with pytest.raises(d.DrafterError):
        _drive_real_drafter(monkeypatch, db, pid, "email1", INVENTS_PAIN, None)


# ===================== per-kind presentation choices =======================

@pytest.mark.integration
def test_dm3_gets_no_shape_because_it_is_specified_to_have_no_question(db_env):
    """An email shape would fail dm3 on every attempt: missing_shape_elements
    requires a "?" and dm3 is a breakup line with no ask."""
    from linkedin_agent import db, evidence_context
    pid = _seed(db)

    payload = evidence_context.build_for("dm3", pid, recent_posts=[HIRING_POST])

    assert "shape" not in payload
    assert "closing" not in payload
    # But the part that governs what may be SAID is still there.
    assert payload["tier"] == "strong"
    assert "pain_claim_licensed" in payload


@pytest.mark.integration
def test_email_gets_the_full_presentation_set(db_env):
    from linkedin_agent import db, evidence_context
    pid = _seed(db)

    payload = evidence_context.build_for("email1", pid, recent_posts=[HIRING_POST])

    assert payload["shape"]["name"]
    assert payload["closing"]["name"]
    assert payload["positioning"]["name"]


@pytest.mark.integration
def test_a_first_touch_gets_positioning_but_a_follow_up_does_not(db_env):
    """By dm2 they have already been introduced to us."""
    from linkedin_agent import db, evidence_context
    pid = _seed(db)

    assert "positioning" in evidence_context.build_for("dm1", pid, [HIRING_POST])
    assert "positioning" not in evidence_context.build_for("dm2", pid, [HIRING_POST])


# ===================== reply evidence comes from their words ===============

@pytest.mark.integration
def test_a_reply_may_engage_with_a_problem_the_prospect_stated(db_env):
    """The gate exists to stop us diagnosing strangers cold, not to stop us
    listening. Without their inbound counted as evidence, a reply answering
    "we're rebuilding by hand" would be rejected for claiming they have a
    build problem — which they had just told us they have."""
    from linkedin_agent import db, evidence_context
    pid = _seed(db, status="replied")
    db.record_message(pid, "inbound",
                      "Honestly we're rebuilding our reporting by hand and it's painful.")

    payload = evidence_context.build_for("reply", pid)

    assert payload["pain_claim_licensed"] is True
    assert any("message to us" in s["source"] for s in payload["signals"])


@pytest.mark.integration
def test_a_private_message_is_never_described_as_a_post(db_env):
    """The source label reaches the drafter. Calling a DM a post invites
    "as you posted...", a confident falsehood about a real person."""
    from linkedin_agent import db, evidence_context
    pid = _seed(db, status="replied")
    db.record_message(pid, "inbound", "We're rebuilding our reporting by hand.")

    payload = evidence_context.build_for("reply", pid)

    sources = [o["source"] for o in payload["observations"]] + \
              [s["source"] for s in payload["signals"]]
    assert sources, "the inbound was not counted as evidence at all"
    assert not any("own post" in s for s in sources)


@pytest.mark.integration
def test_first_touch_kinds_do_not_treat_our_thread_as_their_evidence(db_env):
    """Only `reply` reads inbound messages. On a first touch there are none,
    and on a follow-up the thread is ours."""
    from linkedin_agent import db, evidence_context
    pid = _seed(db)
    db.record_message(pid, "inbound", "We're rebuilding our reporting by hand.")

    payload = evidence_context.build_for("dm2", pid, recent_posts=[])
    assert payload["pain_claim_licensed"] is False


# ===================== nothing else moved ==================================

@pytest.mark.integration
def test_the_approval_flow_is_unchanged(db_env, fake_telegram):
    """Drafts still land as pending_drafts and still get pushed to Telegram."""
    from linkedin_agent import daily as daily_mod, db
    from linkedin_agent.adapters import get_adapter

    pid = _seed(db, status="reacted")
    cfg = _cfg()
    adapter = get_adapter(cfg)
    try:
        daily_mod.run_daily(
            cfg, adapter=adapter, telegram=fake_telegram,
            drafter=lambda k, p, recent_posts=None, evidence=None: "x" * 400)
    finally:
        adapter.close()

    drafts = [x for x in db.list_pending_drafts() if x["prospect_id"] == pid]
    assert len(drafts) == 1
    assert drafts[0]["status"] == "pending"
    assert len(fake_telegram.drafts_pushed) == 1


@pytest.mark.integration
def test_dm_count_semantics_are_unchanged(db_env):
    from linkedin_agent import db
    from linkedin_agent.adapters import get_adapter
    from linkedin_agent.bot_daemon import send_draft_via_adapter

    pid = _seed(db, status="connected")
    did = db.enqueue_draft(pid, "dm1", "a body long enough to be real")
    cfg = _cfg()
    adapter = get_adapter(cfg)
    try:
        send_draft_via_adapter(cfg, adapter, db.get_draft(did))
    finally:
        adapter.close()

    row = db.get_prospect(pid)
    assert row["dm_count"] == 1
    assert row["last_dm_at"] is not None
    assert row["status"] == "dm_sent"


@pytest.mark.integration
def test_connection_note_handling_is_unchanged(db_env):
    """Still advances the pipeline and still records the note as an outbound
    message — the property added in bb47765."""
    from linkedin_agent import db
    from linkedin_agent.adapters import get_adapter
    from linkedin_agent.bot_daemon import send_draft_via_adapter

    pid = _seed(db, status="reacted")
    did = db.enqueue_draft(pid, "connect_note", "a note about their work")
    cfg = _cfg()
    adapter = get_adapter(cfg)
    try:
        send_draft_via_adapter(cfg, adapter, db.get_draft(did))
    finally:
        adapter.close()

    assert db.get_prospect(pid)["status"] == "connection_sent"
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT body FROM messages WHERE prospect_id=? AND direction='outbound'",
            (pid,)).fetchall()
    assert [r["body"] for r in rows] == ["a note about their work"]


# ===================== the ordering the wiring depends on ==================

@pytest.mark.integration
def test_the_stored_position_order_matches_the_provider_order(db_env):
    """positions[0] is "where they work now" to build_evidence, whether the
    data came from a live scrape or from the DB an hour later.

    The provider layer was corrected for this; the DB read path was not, so
    reconstructing a profile from storage put a former employer at position
    zero — and build_evidence would have stated it as a VERIFIED_FACT.
    """
    from linkedin_agent import db, evidence_context

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/order-check", full_name="Ora")
    db.replace_positions(pid, [
        Position(company="NowCorp", title="CTO", start_date=None, end_date=None,
                 is_current=True, date_precision="unknown"),
        Position(company="OldCorp", title="Engineer", start_date="2019-01-01",
                 end_date="2022-01-01", is_current=False, date_precision="month"),
    ])

    facts = evidence_context.facts_for(pid)
    assert facts.positions[0].company == "NowCorp"

    payload = evidence_context.build_for("dm1", pid, recent_posts=[])
    stated = " ".join(f["statement"] for f in payload["verified_facts"])
    assert "NowCorp" in stated
    assert "OldCorp" not in stated, "stated a former employer as their role"
