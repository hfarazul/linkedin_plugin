"""PB-8 — the PhantomBuster inbox path end to end, from raw scraper rows.

tests/test_poll_router.py exercises poll_once against hand-built
InboundMessage objects, and tests/test_provider_phantombuster.py exercises the
row mapper in isolation. Neither covers the seam between them: a raw Inbox
Scraper row travelling all the way through normalization, prospect matching,
insertion, sequence-halting, drafting and Telegram.

That seam is where a field-name mistake would survive both existing suites, so
every fixture here is a verbatim row captured from agent 1327575646342095 —
mojibake and CSV-style "TRUE"/"FALSE" strings included.
"""

from __future__ import annotations

import pytest

from linkedin_agent.providers.capabilities import Capability
from linkedin_agent.providers.phantombuster import inbound_from_row
from tests.fakes import FakeTelegramClient


# --- verbatim rows from the live Inbox Scraper run ---------------------------
PB_REPLY_ROW = {
    "threadUrl": "https://www.linkedin.com/messaging/thread/2-ZGEyNmI5MmIt.../",
    "linkedInUrls": "https://www.linkedin.com/in/ACoAADEv3hgBnfiUSJ1apzGtyJCtRSFQni_zwMU",
    "message": "Thanks, Zakaur",
    "lastMessageFromUrl": "https://www.linkedin.com/in/ACoAADEv3hgBnfiUSJ1apzGtyJCtRSFQni_zwMU",
    "lastMessageFromEntityUrn":
        "urn:li:msg_messagingParticipant:urn:li:fsd_profile:ACoAADEv3hgBnfiUSJ1apzGtyJCtRSFQni_zwMU",
    "firstnameFrom": "Arif", "lastnameFrom": "Hassan",
    "occupationFrom": "Planning Engineer (E&I) | Oil & Gas EPC Projects",
    "isLastMessageFromMe": "FALSE", "readStatus": "TRUE",
    "lastMessageDate": "2025-05-31T09:57:02.966Z",
    "timestamp": "2026-09-02T14:26:30.077Z",
}

PB_OURS_ROW = {
    "threadUrl": "https://www.linkedin.com/messaging/thread/2-ZmQzM2QwNzIt.../",
    "linkedInUrls": "https://www.linkedin.com/in/ACoAADdAiegBgVhsC0ond2h1_meyUCrSz9vkIn0",
    "message": "Thank you",
    "lastMessageFromEntityUrn":
        "urn:li:msg_messagingParticipant:urn:li:fsd_profile:ACoAADl5vRMBD-omfcGDSbcx3Y4HO11QLxLXcEk",
    # These are OUR details — the account owner sent the last message.
    "firstnameFrom": "Zakaur", "lastnameFrom": "Rahman",
    "occupationFrom": "Full Stack Engineer | Next.js â€¢ React â€¢ Node.js",
    "isLastMessageFromMe": "TRUE", "readStatus": "TRUE",
    "lastMessageDate": "2025-03-24T06:07:33.364Z",
}

# The captured row is 15 months old — realistic for a first backlog scrape,
# but the staleness guard suppresses drafting for it. Workflow tests use a
# recent copy of the same row so they exercise the live path.
PB_RECENT_ROW = {**PB_REPLY_ROW,
                 "lastMessageDate": "2026-09-02T09:57:02.966Z"}

PROSPECT_ID = "ACoAADEv3hgBnfiUSJ1apzGtyJCtRSFQni_zwMU"
OUR_ACCOUNT_ID = "ACoAADl5vRMBD-omfcGDSbcx3Y4HO11QLxLXcEk"


class PhantomBusterLikeProvider:
    """Returns exactly what PhantomBusterProvider.fetch_inbox() would, by
    running the real mapper over the real rows. Only the HTTP/container layer
    is faked, so a change to the mapper breaks these tests."""

    name = "phantombuster"
    is_async = True

    def __init__(self, rows):
        self._rows = rows
        self.capabilities_asked: list = []

    def supports(self, capability):
        self.capabilities_asked.append(capability)
        return capability == Capability.INBOX_READ

    def fetch_inbox(self, limit=50):
        out = [inbound_from_row(r) for r in self._rows]
        return [m for m in out if m is not None]

    def close(self):
        pass


def _router(provider):
    from linkedin_agent.providers.router import CapabilityRouter
    return CapabilityRouter(provider, None)


def _cfg():
    return type("CFG", (), {
        "unipile_api_key": None, "unipile_account_id": None, "unipile_dsn": None,
        "telegram_bot_token": "t", "telegram_chat_id": 1, "dry_run": False,
    })()


def _seed(db, provider_id=PROSPECT_ID, name="Arif Hassan"):
    return db.upsert_prospect(
        linkedin_url=f"https://www.linkedin.com/in/{provider_id}",
        full_name=name, company="Acme", provider_id=provider_id)


def _poll(cfg, provider, **kw):
    from linkedin_agent import poll as poll_mod
    kw.setdefault("notify", False)
    kw.setdefault("draft_replies", False)
    kw.setdefault("drafter", lambda *a, **k: "unused")
    return poll_mod.poll_once(cfg, router=_router(provider), **kw)


# ============ 1. router is asked for INBOX_READ, and nothing else ============

@pytest.mark.integration
def test_poll_requests_the_inbox_read_capability(db_env) -> None:
    from linkedin_agent import db
    _seed(db)
    provider = PhantomBusterLikeProvider([PB_REPLY_ROW])
    _poll(_cfg(), provider)
    assert Capability.INBOX_READ in provider.capabilities_asked
    assert set(provider.capabilities_asked) == {Capability.INBOX_READ}, \
        "poll must not reach for any other capability"


# ============ 2. raw PB row -> messages row, field by field ==================

@pytest.mark.integration
def test_raw_phantombuster_row_maps_into_messages(db_env) -> None:
    """The full field mapping the review asked for, asserted end to end rather
    than at the mapper boundary."""
    from linkedin_agent import db
    pid = _seed(db)
    _poll(_cfg(), PhantomBusterLikeProvider([PB_REPLY_ROW]))

    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE prospect_id = ?", (pid,)).fetchone()

    assert row["body"] == "Thanks, Zakaur"                       # message
    assert row["thread_id"] == PB_REPLY_ROW["threadUrl"]          # threadUrl
    assert row["sent_at"] == "2025-05-31T09:57:02.966Z"           # lastMessageDate
    assert row["direction"] == "inbound"                          # isLastMessageFromMe
    assert row["channel"] == "linkedin"
    assert row["external_id"] == inbound_from_row(PB_REPLY_ROW).external_id
    assert len(row["external_id"]) == 32, "deterministic synthetic id"


# ============ 3. prospect matching goes through the existing lookup ==========

@pytest.mark.integration
def test_matching_uses_the_existing_provider_id_lookup(db_env, monkeypatch) -> None:
    """No second matching mechanism. If someone later adds a URL-based
    fallback, this spy fails."""
    from linkedin_agent import db, poll as poll_mod
    pid = _seed(db)

    seen: list[str] = []
    original = db.get_prospect_by_provider_id

    def spy(provider_id):
        seen.append(provider_id)
        return original(provider_id)

    monkeypatch.setattr(poll_mod.db, "get_prospect_by_provider_id", spy)
    monkeypatch.setattr(
        poll_mod.db, "get_prospect_by_linkedin_url",
        lambda *a, **k: pytest.fail("poll must not match prospects by URL"),
        raising=False)

    _poll(_cfg(), PhantomBusterLikeProvider([PB_REPLY_ROW]))
    assert seen == [PROSPECT_ID], "matched on the ACoAA id from linkedInUrls"


# ============ 5. sender fields never touch the prospect ======================

@pytest.mark.integration
def test_our_own_last_message_does_not_corrupt_the_prospect(db_env) -> None:
    """The data-corruption trap, asserted at the poll level.

    In PB_OURS_ROW the *From fields hold OUR name and occupation. If any of it
    reached the prospect record, that prospect would end up named after the
    account owner.
    """
    from linkedin_agent import db
    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/ACoAADdAiegBgVhsC0ond2h1_meyUCrSz9vkIn0",
        full_name="Real Prospect", headline="Head of Ops",
        provider_id="ACoAADdAiegBgVhsC0ond2h1_meyUCrSz9vkIn0")

    result = _poll(_cfg(), PhantomBusterLikeProvider([PB_OURS_ROW]))

    prospect = db.get_prospect(pid)
    assert prospect["full_name"] == "Real Prospect"
    assert prospect["headline"] == "Head of Ops"
    assert "Zakaur" not in (prospect["full_name"] or "")
    assert prospect["status"] == "targeted", "our own message is not a reply"
    assert result.new_inbound == 0


@pytest.mark.integration
def test_prospect_identity_comes_from_linkedin_urls_not_the_sender_urn(db_env) -> None:
    """In PB_OURS_ROW the sender URN is our account and linkedInUrls is the
    prospect. Matching on the URN would attach the message to ourselves."""
    msg = inbound_from_row(PB_OURS_ROW)
    assert msg.prospect_provider_id == "ACoAADdAiegBgVhsC0ond2h1_meyUCrSz9vkIn0"
    assert msg.prospect_provider_id != OUR_ACCOUNT_ID


# ============ 6. the full reply workflow ====================================

@pytest.mark.integration
def test_full_reply_workflow_from_a_raw_row(db_env, monkeypatch) -> None:
    """PB inbound -> matched -> inserted -> follow-ups cancelled -> reply
    drafted -> Telegram approval card. The whole chain in one test."""
    from linkedin_agent import db, poll as poll_mod

    pid = _seed(db)
    stale_draft = db.enqueue_draft(pid, "dm2", "scheduled follow-up")

    fake_tg = FakeTelegramClient(_cfg())
    monkeypatch.setattr(poll_mod, "TelegramClient", lambda c: fake_tg)

    drafted: list = []

    def drafter(kind, prospect_id, recent_posts=None, evidence=None):
        drafted.append((kind, prospect_id))
        return "Happy to walk through it — what does your build side look like?"

    result = poll_mod.poll_once(
        _cfg(), router=_router(PhantomBusterLikeProvider([PB_RECENT_ROW])),
        notify=True, draft_replies=True, drafter=drafter)

    # inserted + matched
    assert result.new_inbound == 1
    assert result.matched_prospects == 1
    # status advanced
    assert db.get_prospect(pid)["status"] == "replied"
    # the scheduled follow-up was halted
    assert db.get_draft(stale_draft)["status"] == "rejected"
    # a reply was drafted for the right prospect
    assert drafted == [("reply", pid)]
    # and pushed to Telegram with the inbound embedded for context
    assert len(fake_tg.drafts_pushed) == 1
    card = fake_tg.drafts_pushed[0]
    assert card.kind == "reply"
    assert card.inbound_excerpt == "Thanks, Zakaur"
    assert card.prospect_name == "Arif Hassan"
    # the draft row exists and is linked to the card
    pending = db.list_pending_drafts(prospect_id=pid, status="pending")
    assert [d["kind"] for d in pending] == ["reply"]
    assert db.get_draft(pending[0]["id"])["telegram_message_id"] == card.telegram_message_id


# ============ architectural: InboundMessage is not a conversation ===========

@pytest.mark.integration
def test_history_comes_from_our_own_records_plus_the_latest_inbound(db_env, monkeypatch) -> None:
    """The Inbox Scraper returns one row per thread carrying only the latest
    message, so poll must not be treated as a source of conversation history.

    The drafter's prior_messages must therefore be our stored outbound plus
    that one inbound — which is exactly what the messages table yields.
    """
    from linkedin_agent import db, drafter as drafter_mod, poll as poll_mod

    pid = _seed(db)
    # Real ordering: we sent first, they replied afterwards. Explicit
    # timestamps because record_message otherwise stamps now(), which would
    # place our sends after a reply that actually arrived yesterday.
    db.record_message(pid, "outbound", "Original DM1 we sent",
                      sent_at="2026-08-20T10:00:00+00:00")
    db.record_message(pid, "outbound", "DM2 follow-up we sent",
                      sent_at="2026-08-27T10:00:00+00:00")

    monkeypatch.setattr(poll_mod, "TelegramClient", lambda c: FakeTelegramClient(_cfg()))
    poll_mod.poll_once(
        _cfg(), router=_router(PhantomBusterLikeProvider([PB_RECENT_ROW])),
        notify=True, draft_replies=True, drafter=lambda *a, **k: "drafted reply")

    built = drafter_mod.build_input("reply", pid)
    bodies = [m["body"] for m in built.prior_messages]
    assert "Thanks, Zakaur" in bodies, "the scraped inbound is in the history"
    assert "Original DM1 we sent" in bodies, "our own sends are still there"
    # The inbound is last, which is what the drafter prompt relies on when it
    # says "the last entry is the message you are answering".
    assert bodies[-1] == "Thanks, Zakaur"
    assert built.prior_messages[-1]["direction"] == "inbound"


# ============ dedup, from raw rows ==========================================

@pytest.mark.integration
def test_rescraping_the_same_row_inserts_once(db_env) -> None:
    from linkedin_agent import db
    pid = _seed(db)
    provider = PhantomBusterLikeProvider([PB_REPLY_ROW])
    assert _poll(_cfg(), provider).new_inbound == 1
    assert _poll(_cfg(), provider).new_inbound == 0
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE prospect_id = ?", (pid,)
        ).fetchone()[0] == 1


@pytest.mark.integration
def test_same_thread_newer_date_is_a_new_message(db_env) -> None:
    """Keying on threadUrl alone would silently swallow every later reply."""
    from linkedin_agent import db
    pid = _seed(db)
    _poll(_cfg(), PhantomBusterLikeProvider([PB_REPLY_ROW]))
    newer = {**PB_REPLY_ROW, "message": "Following up on this",
             "lastMessageDate": "2026-09-01T10:00:00.000Z"}
    assert _poll(_cfg(), PhantomBusterLikeProvider([newer])).new_inbound == 1

    with db.connect() as conn:
        bodies = [r["body"] for r in conn.execute(
            "SELECT body FROM messages WHERE prospect_id = ? ORDER BY id", (pid,))]
    assert bodies == ["Thanks, Zakaur", "Following up on this"]


# ============ mixed batch ===================================================

@pytest.mark.integration
def test_mixed_batch_processes_only_genuine_replies(db_env) -> None:
    from linkedin_agent import db
    _seed(db)
    db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/ACoAADdAiegBgVhsC0ond2h1_meyUCrSz9vkIn0",
        full_name="Other", provider_id="ACoAADdAiegBgVhsC0ond2h1_meyUCrSz9vkIn0")

    result = _poll(_cfg(), PhantomBusterLikeProvider([PB_REPLY_ROW, PB_OURS_ROW]))
    assert result.fetched == 2
    assert result.new_inbound == 1, "only the row we did not send"


# ============ staleness guard: the backlog hazard ===========================
#
# The Inbox Scraper is incremental, so its FIRST run against a real account
# returns the entire backlog. The captured inbox contained threads whose last
# message was 15 months old. Without a guard, switching provider would flood
# Telegram with drafted replies to year-old messages.

@pytest.mark.integration
def test_stale_inbound_is_recorded_but_not_drafted(db_env, monkeypatch) -> None:
    from linkedin_agent import db, poll as poll_mod

    pid = _seed(db)
    fake_tg = FakeTelegramClient(_cfg())
    monkeypatch.setattr(poll_mod, "TelegramClient", lambda c: fake_tg)

    def forbidden(*a, **k):
        pytest.fail("must not draft a reply to a 15-month-old message")

    # PB_REPLY_ROW's real date is 2025-05-31.
    result = poll_mod.poll_once(
        _cfg(), router=_router(PhantomBusterLikeProvider([PB_REPLY_ROW])),
        notify=True, draft_replies=True, drafter=forbidden)

    # Still recorded: history and dedup both need it.
    assert result.new_inbound == 1
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE prospect_id = ?", (pid,)
        ).fetchone()[0] == 1
    # No draft was produced...
    assert db.list_pending_drafts(prospect_id=pid, status="pending") == []
    assert fake_tg.drafts_pushed == []
    # ...but the operator is still told, so nothing is silently swallowed.
    assert len(fake_tg.replies_notified) == 1


@pytest.mark.integration
def test_recent_inbound_still_drafts(db_env, monkeypatch) -> None:
    """The guard must not disable the reply flow for genuine new replies."""
    from linkedin_agent import db, poll as poll_mod
    pid = _seed(db)
    monkeypatch.setattr(poll_mod, "TelegramClient", lambda c: FakeTelegramClient(_cfg()))

    poll_mod.poll_once(
        _cfg(), router=_router(PhantomBusterLikeProvider([PB_RECENT_ROW])),
        notify=True, draft_replies=True, drafter=lambda *a, **k: "a fresh reply")

    assert [d["kind"] for d in db.list_pending_drafts(prospect_id=pid)] == ["reply"]


@pytest.mark.unit
def test_is_stale_boundaries() -> None:
    from datetime import datetime, timedelta, timezone
    from linkedin_agent import poll as poll_mod

    now = datetime(2026, 9, 3, tzinfo=timezone.utc)
    recent = (now - timedelta(days=5)).isoformat()
    old = (now - timedelta(days=400)).isoformat()

    assert poll_mod._is_stale(recent, now=now) is False
    assert poll_mod._is_stale(old, now=now) is True


@pytest.mark.unit
def test_undateable_messages_are_not_treated_as_stale() -> None:
    """Unipile payloads do not always carry a timestamp. Suppressing drafts for
    everything we cannot date would silently disable the reply flow."""
    from linkedin_agent import poll as poll_mod
    assert poll_mod._is_stale(None) is False
    assert poll_mod._is_stale("") is False
    assert poll_mod._is_stale("not a date") is False
