#!/usr/bin/env python
"""PB-8 live-data verification — a real inbound message through the real path.

Unit tests prove the mapping against captured fixtures. They cannot prove that
the container launches, that agent arguments are named correctly, that status
strings match what the poller expects, that S3 has results by the time we read
them, or that a genuinely NEW message survives normalization. Only a live run
with fresh data does that.

Ten checks, matching the PB-8 acceptance list:

   1  raw PB result contains the new message
   2  linkedInUrls        -> provider_id
   3  threadUrl           -> thread_id
   4  message             -> body
   5  isLastMessageFromMe -> false for an inbound
   6  lastMessageDate     -> populated
   7  deterministic external_id generated
   8  poll_once matches the prospect via get_prospect_by_provider_id()
   9  inbound persisted; stale follow-up drafts cancelled; reply drafted and
      pushed for approval
  10  a second pass over the SAME payload does not reprocess it

Pass 10 replays the already-fetched payload rather than launching a second
container. The Phantom is incremental, so a re-launch would return zero rows
and the dedup check would pass vacuously — proving nothing.

Safety:
  * throwaway DB, never data/outreach.db
  * Telegram is recorded in-process, not sent (use --real-telegram to opt in)
  * the drafter is stubbed, so no `claude -p` calls (--real-drafter to opt in)
  * the Inbox Scraper only reads

Setup:
  1. From a throwaway LinkedIn profile, send ONE message to the connected
     account.
  2. python scripts/smoke_pb_inbox.py --expect-from <ACoAA... | profile-url>

T-9 note: this does NOT verify the Unread filter. That is a separate question
about which inbox segment the Phantom queries. Set the filter in the
PhantomBuster UI, then use --show-args to confirm what the agent will actually
run with before claiming T-9 is closed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROVIDER_ID_RE = re.compile(r"(ACo[A-Za-z0-9_\-]{10,})")

# A Phantom's saved arguments include the LinkedIn session cookie it runs with.
# That cookie IS the account: anyone holding it can act as the user until it is
# invalidated. Diagnostic output gets pasted into tickets and chat, so it is
# redacted here rather than left to the operator to notice.
_SECRET_ARG_KEYS = {"sessioncookie", "cookie", "li_at", "password", "token",
                    "apikey", "api_key", "secret", "accesstoken"}


def _redact_agent_args(args: dict) -> dict:
    """Blank any credential-shaped value before printing agent arguments."""
    out = {}
    for key, value in (args or {}).items():
        if key.lower().replace("-", "").replace("_", "") in {
                k.replace("_", "") for k in _SECRET_ARG_KEYS}:
            out[key] = f"<redacted, {len(str(value))} chars>"
        else:
            out[key] = value
    return out


class RecordingTelegram:
    """Captures approval cards instead of sending them."""

    def __init__(self, cfg=None):
        self.cards = []
        self.plain = []
        self._next = 500

    def push_draft_for_approval(self, draft_id, kind, body, prospect_name,
                                prospect_company=None, prospect_url=None,
                                campaign_name=None, inbound_excerpt=None):
        self._next += 1
        self.cards.append({"draft_id": draft_id, "kind": kind, "body": body,
                           "inbound_excerpt": inbound_excerpt,
                           "prospect_name": prospect_name})
        return self._next

    def notify_reply(self, prospect_name, prospect_company, body, thread_url=None):
        self.plain.append(body)
        self._next += 1
        return self._next

    def notify_text(self, text):
        self.plain.append(text)
        self._next += 1
        return self._next

    def close(self):
        pass


class ReplayProvider:
    """Wraps a real provider, serving a payload fetched once.

    Pass 10 needs the SAME messages twice. Re-launching would return zero rows
    (the Phantom is incremental) and the dedup assertion would pass without
    testing anything.
    """

    is_async = True

    def __init__(self, name, messages):
        self.name = name
        self._messages = messages

    def supports(self, capability):
        from linkedin_agent.providers import Capability
        return capability == Capability.INBOX_READ

    def fetch_inbox(self, limit=50):
        return list(self._messages)

    def close(self):
        pass


def _check(results, label, ok, detail=""):
    results.append((label, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--expect-from", default=None,
                    help="provider id or profile URL of the throwaway account "
                         "that sent the test message")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--show-args", action="store_true",
                    help="print the agent's saved arguments and exit (for T-9)")
    ap.add_argument("--real-telegram", action="store_true")
    ap.add_argument("--real-drafter", action="store_true",
                    help="invoke `claude -p` instead of a stub")
    args = ap.parse_args()

    tmp = Path(tempfile.gettempdir()) / "pb_smoke.db"
    if tmp.exists():
        tmp.unlink()
    os.environ["LINKEDIN_DB_PATH"] = str(tmp)

    from linkedin_agent import db, poll as poll_mod
    from linkedin_agent.config import load as load_config
    from linkedin_agent.providers import Capability, build_router
    from linkedin_agent.providers.router import CapabilityRouter

    cfg = load_config()
    agent_id = os.getenv("PHANTOMBUSTER_AGENT_INBOX_SCRAPER")
    if not os.getenv("PHANTOMBUSTER_API_KEY") or not agent_id:
        print("BLOCKED: PHANTOMBUSTER_API_KEY and "
              "PHANTOMBUSTER_AGENT_INBOX_SCRAPER must both be set in .env")
        return 2

    router = build_router(cfg, primary="phantombuster", fallback=None)
    if router.owner_of(Capability.INBOX_READ) != "phantombuster":
        print("BLOCKED: PhantomBuster does not own inbox_read; check config.")
        router.close()
        return 2
    provider = router.providers_for(Capability.INBOX_READ)[0]

    # ---- T-9 helper: what will the agent actually run with? -----------------
    if args.show_args:
        try:
            detail = provider.jobs()._request(
                "GET", "/agents/fetch", params={"id": agent_id})
            saved = detail.get("argument")
            parsed = json.loads(saved) if isinstance(saved, str) else (saved or {})
            print("Agent saved arguments:")
            print(json.dumps(_redact_agent_args(parsed), indent=2)[:2000])
            print("\nFor T-9, confirm the inbox filter here reads Unread before")
            print("claiming the Unread filter is verified. Note that seeing the")
            print("filter SET is configuration, not behaviour — T-9 needs an")
            print("actual unread message to prove the filter selects correctly.")
        finally:
            router.close()
        return 0

    expected_id = None
    if args.expect_from:
        m = PROVIDER_ID_RE.search(args.expect_from)
        expected_id = m.group(1) if m else None
        if not expected_id:
            print(f"BLOCKED: could not read an ACoAA… id from "
                  f"{args.expect_from!r}. Use the profile URL that contains it.")
            router.close()
            return 2

    print(f"temp DB: {tmp}")
    db.init_db()
    results: list[tuple] = []

    try:
        # ================= fetch once =====================================
        print("\n=== launching Inbox Scraper (blocks for the run) ===")
        messages = provider.fetch_inbox(args.limit)
        print(f"returned {len(messages)} normalized message(s)\n")

        for m in messages[:8]:
            body = (m.body or "")[:40].replace("\n", " ")
            print(f"  from={str(m.prospect_provider_id)[:22]:24s} "
                  f"from_me={str(m.is_from_me):5s} "
                  f"sent={str(m.sent_at)[:19]:19s} {body!r}")
        print()

        print("=== checks ===")
        _check(results, "1  raw result contains at least one message",
               bool(messages),
               "" if messages else "the Phantom is incremental — send a NEW "
                                   "message, then re-run")
        if not messages:
            return 1

        target = None
        if expected_id:
            target = next((m for m in messages
                           if m.prospect_provider_id == expected_id), None)
            _check(results, "2  linkedInUrls -> expected provider_id",
                   target is not None,
                   f"looking for {expected_id[:20]}…")
        else:
            target = next((m for m in messages if not m.is_from_me), None)
            _check(results, "2  linkedInUrls -> provider_id",
                   target is not None and bool(target.prospect_provider_id),
                   "no --expect-from given; using the first inbound")
        if target is None:
            print("\n  Cannot continue without a target message.")
            return 1

        _check(results, "3  threadUrl -> thread_id", bool(target.thread_id))
        _check(results, "4  message -> body", bool(target.body),
               f"{(target.body or '')[:40]!r}")
        _check(results, "5  isLastMessageFromMe is false", target.is_from_me is False)
        _check(results, "6  lastMessageDate populated", bool(target.sent_at),
               str(target.sent_at))
        _check(results, "7  deterministic external_id",
               bool(target.external_id) and len(target.external_id) == 32,
               target.external_id or "")

        # ================= seed state the poll should act on ===============
        pid = db.upsert_prospect(
            linkedin_url=f"https://www.linkedin.com/in/{target.prospect_provider_id}",
            full_name="Smoke Test Prospect", company="SmokeCo",
            provider_id=target.prospect_provider_id)
        with db.connect() as conn:
            conn.execute("UPDATE prospects SET status='dm_sent' WHERE id=?", (pid,))
        stale_draft = db.enqueue_draft(pid, "dm2", "a scheduled follow-up")

        matched_ids: list = []
        original_lookup = db.get_prospect_by_provider_id

        def spy(provider_id):
            matched_ids.append(provider_id)
            return original_lookup(provider_id)

        poll_mod.db.get_prospect_by_provider_id = spy

        recorder = RecordingTelegram()
        if not args.real_telegram:
            poll_mod.TelegramClient = lambda c: recorder

        drafter = None if args.real_drafter else (
            lambda kind, prospect_id, recent_posts=None:
            "Thanks for coming back to me — what does your build side look like?")

        replay = CapabilityRouter(
            ReplayProvider(provider.name, messages), None)

        # ================= pass 1 ==========================================
        print("\n=== pass 1: poll_once over the live payload ===")
        first = poll_mod.poll_once(cfg, limit=args.limit, notify=True,
                                   draft_replies=True, router=replay,
                                   drafter=drafter)
        print(f"  fetched={first.fetched} new_inbound={first.new_inbound} "
              f"matched={first.matched_prospects}")

        _check(results, "8  matched via get_prospect_by_provider_id()",
               target.prospect_provider_id in matched_ids)
        with db.connect() as conn:
            stored = conn.execute(
                "SELECT body, thread_id, sent_at, external_id FROM messages "
                "WHERE prospect_id = ?", (pid,)).fetchone()
        _check(results, "9a inbound persisted", stored is not None)
        if stored:
            _check(results, "9b persisted fields match the payload",
                   stored["body"] == target.body
                   and stored["thread_id"] == target.thread_id
                   and stored["external_id"] == target.external_id)
        _check(results, "9c stale follow-up cancelled",
               db.get_draft(stale_draft)["status"] == "rejected")

        stale_inbound = poll_mod._is_stale(target.sent_at)
        drafts = db.list_pending_drafts(prospect_id=pid, status="pending")
        if stale_inbound:
            _check(results, "9d reply draft suppressed (message >30d old)",
                   not drafts and bool(recorder.plain),
                   "backlog guard fired — send a FRESH message to test drafting")
        else:
            _check(results, "9d reply draft generated",
                   [d["kind"] for d in drafts] == ["reply"])
            _check(results, "9e pushed for approval",
                   len(recorder.cards) == 1
                   and recorder.cards[0]["inbound_excerpt"] == target.body)

        # ================= pass 2: dedup ===================================
        print("\n=== pass 2: same payload again (dedup) ===")
        second = poll_mod.poll_once(cfg, limit=args.limit, notify=True,
                                    draft_replies=True, router=replay,
                                    drafter=drafter)
        print(f"  fetched={second.fetched} new_inbound={second.new_inbound}")
        with db.connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE prospect_id = ?",
                (pid,)).fetchone()[0]
        _check(results, "10 same message not reprocessed",
               second.new_inbound == 0 and total == 1,
               f"messages rows = {total}")

        # ================= verdict =========================================
        failed = [label for label, ok, _ in results if not ok]
        print("\n" + "=" * 66)
        if failed:
            print(f"RESULT: {len(failed)} check(s) FAILED")
            for label in failed:
                print(f"  - {label}")
            return 1
        print(f"RESULT: all {len(results)} checks passed.")
        print("PB-8 live-data verification complete.")
        if stale_inbound:
            print("\nNOTE: the message used was older than 30 days, so the")
            print("drafting path was exercised only as far as the backlog guard.")
            print("Send a fresh message to verify draft generation end to end.")
        print("\nT-9 (Unread filter) is NOT covered by this run. Use --show-args.")
        return 0
    except Exception as e:
        print(f"\nFAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        router.close()


if __name__ == "__main__":
    raise SystemExit(main())
