"""PB-8 — inbound polling through the capability router.

The existing tests in test_poll.py cover the Unipile path and still pass
unmodified, because UnipileProvider calls the same endpoint the deleted client
did. These cover the provider-neutral behaviour and the PhantomBuster shape,
whose differences are the ones that can quietly corrupt state:

  * no native message id, so external_id is synthesised
  * one row per thread, latest message only
  * a timestamp that can be hours older than the moment we observe it
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from linkedin_agent.providers.capabilities import Capability, InboundMessage


def _iso_ago(days: float) -> str:
    """A provider timestamp `days` before now, in the Inbox Scraper's real
    millisecond-precision shape ("2025-05-31T09:57:02.966Z").

    These fixtures used fixed calendar dates, and they expired. `poll` treats
    an inbound older than REPLY_DRAFT_MAX_AGE_DAYS (30) as backlog when we
    have never written to the prospect, so a reply dated 2026-08-30 stopped
    counting as a reply on 2026-09-29 and two tests failed with no code
    change. Relative dates keep testing the behaviour the tests are about.
    """
    when = datetime.now(timezone.utc) - timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


# A reply that arrived yesterday: well inside the window, whenever this runs.
RECENT = _iso_ago(1)


class FakeInboxProvider:
    name = "phantombuster"

    def __init__(self, messages, supported=True):
        self._messages = messages
        self._supported = supported
        self.calls = []

    def supports(self, capability):
        return self._supported and capability == Capability.INBOX_READ

    def fetch_inbox(self, limit=50):
        self.calls.append(("fetch_inbox", limit))
        return list(self._messages)

    def close(self):
        self.calls.append(("close",))


def _router(provider):
    from linkedin_agent.providers.router import CapabilityRouter
    return CapabilityRouter(provider, None)


def _cfg():
    return type("CFG", (), {
        "unipile_api_key": None, "unipile_account_id": None, "unipile_dsn": None,
        "telegram_bot_token": None, "telegram_chat_id": None, "dry_run": False,
    })()


def _seed(db, provider_id="ACoPOLL1"):
    return db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/poll-test",
        full_name="Poll Test", provider_id=provider_id)


def _msg(**over):
    base = dict(
        external_id="abc123", prospect_provider_id="ACoPOLL1",
        body="Thanks, happy to chat", sent_at=RECENT,
        thread_id="https://www.linkedin.com/messaging/thread/2-xyz/",
        is_from_me=False, source="phantombuster",
    )
    base.update(over)
    return InboundMessage(**base)


def _poll(cfg, provider, **kw):
    from linkedin_agent import poll as poll_mod
    kw.setdefault("notify", False)
    kw.setdefault("draft_replies", False)
    kw.setdefault("drafter", lambda *a, **k: "unused")
    return poll_mod.poll_once(cfg, router=_router(provider), **kw)


# ----------------------------- happy path ------------------------------------

@pytest.mark.integration
def test_inbound_reply_is_recorded_and_flips_status(db_env) -> None:
    from linkedin_agent import db
    pid = _seed(db)
    result = _poll(_cfg(), FakeInboxProvider([_msg()]))

    assert result.new_inbound == 1
    assert result.matched_prospects == 1
    assert db.get_prospect(pid)["status"] == "replied"


@pytest.mark.integration
def test_thread_id_and_provider_timestamp_are_stored(db_env) -> None:
    """sent_at must be the provider's timestamp, not ours. A batch scraper can
    surface a reply hours after it arrived, and follow-up timing reads this."""
    from linkedin_agent import db
    pid = _seed(db)
    _poll(_cfg(), FakeInboxProvider([_msg()]))

    with db.connect() as conn:
        row = conn.execute(
            "SELECT sent_at, thread_id, channel, direction FROM messages "
            "WHERE prospect_id = ?", (pid,)).fetchone()
    assert row["sent_at"] == RECENT
    assert row["thread_id"].endswith("2-xyz/")
    assert row["channel"] == "linkedin"
    assert row["direction"] == "inbound"


@pytest.mark.integration
def test_reply_cancels_pending_drafts(db_env) -> None:
    """A reply must halt the follow-up sequence — sending DM3 to someone who
    already answered is the failure this prevents."""
    from linkedin_agent import db
    pid = _seed(db)
    draft_id = db.enqueue_draft(pid, "dm2", "scheduled follow-up")
    _poll(_cfg(), FakeInboxProvider([_msg()]))
    assert db.get_draft(draft_id)["status"] == "rejected"


# ----------------------------- dedup -----------------------------------------

@pytest.mark.integration
def test_same_message_twice_is_recorded_once(db_env) -> None:
    """PhantomBuster has no native message id, so external_id is synthesised.
    The UNIQUE index is what makes re-scraping idempotent."""
    from linkedin_agent import db
    pid = _seed(db)
    provider = FakeInboxProvider([_msg()])
    assert _poll(_cfg(), provider).new_inbound == 1
    assert _poll(_cfg(), provider).new_inbound == 0

    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE prospect_id = ?", (pid,)).fetchone()[0]
    assert count == 1


@pytest.mark.integration
def test_a_later_reply_in_the_same_thread_is_recorded(db_env) -> None:
    """The inverse risk: keying on the thread alone would silently swallow
    every subsequent reply from the same person."""
    from linkedin_agent import db
    _seed(db)
    _poll(_cfg(), FakeInboxProvider([_msg()]))
    later = _msg(external_id="def456", body="Following up",
                 sent_at=_iso_ago(0.5))
    assert _poll(_cfg(), FakeInboxProvider([later])).new_inbound == 1


# ----------------------------- filtering -------------------------------------

@pytest.mark.integration
def test_our_own_messages_are_ignored(db_env) -> None:
    from linkedin_agent import db
    _seed(db)
    result = _poll(_cfg(), FakeInboxProvider([_msg(is_from_me=True)]))
    assert result.new_inbound == 0


@pytest.mark.integration
def test_unknown_sender_is_skipped_not_imported(db_env) -> None:
    """Random recruiter DMs are not part of the pipeline."""
    result = _poll(_cfg(), FakeInboxProvider([_msg(prospect_provider_id="ACoSTRANGER")]))
    assert result.skipped_unknown_sender == 1
    assert result.new_inbound == 0


@pytest.mark.integration
def test_message_without_provider_id_is_skipped(db_env) -> None:
    from linkedin_agent import db
    _seed(db)
    result = _poll(_cfg(), FakeInboxProvider([_msg(prospect_provider_id=None)]))
    assert result.new_inbound == 0
    assert result.skipped_unknown_sender == 0


# ----------------------------- no provider -----------------------------------

@pytest.mark.integration
def test_no_inbox_provider_skips_gracefully(db_env) -> None:
    """`daily` runs in tests with a fake adapter and no credentials. Polling
    must no-op rather than fail the whole cron cycle."""
    result = _poll(_cfg(), FakeInboxProvider([_msg()], supported=False))
    assert result.fetched == 0
    assert result.new_inbound == 0


@pytest.mark.integration
def test_provider_is_asked_for_the_requested_limit(db_env) -> None:
    from linkedin_agent import db
    _seed(db)
    provider = FakeInboxProvider([_msg()])
    _poll(_cfg(), provider, limit=25)
    assert provider.calls[0] == ("fetch_inbox", 25)


# ----------------------------- ordering --------------------------------------

@pytest.mark.integration
def test_messages_are_processed_oldest_first(db_env) -> None:
    """Providers return newest-first; notifications should arrive in the order
    the conversation actually happened."""
    from linkedin_agent import db
    pid = _seed(db)
    newest = _msg(external_id="new", body="second", sent_at=_iso_ago(1))
    oldest = _msg(external_id="old", body="first", sent_at=_iso_ago(2))
    _poll(_cfg(), FakeInboxProvider([newest, oldest]))

    with db.connect() as conn:
        bodies = [r["body"] for r in conn.execute(
            "SELECT body FROM messages WHERE prospect_id = ? ORDER BY id", (pid,))]
    assert bodies == ["first", "second"]
