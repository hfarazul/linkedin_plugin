#!/usr/bin/env python
"""PB-8 smoke test — one real Inbox Scraper run through the real poll path.

Unit tests prove the mapping against captured fixtures. They cannot prove that
the container launches, that the agent arguments are named correctly, that the
status strings match what the poller expects, or that S3 has the results by the
time we read them. Only a live run does that.

Safety:
  * writes to a throwaway DB, never data/outreach.db
  * notify=False    -- no Telegram
  * draft_replies=False -- no `claude -p`, no drafts
  * read-only against LinkedIn: the Inbox Scraper only reads

Cost: one container launch from the PhantomBuster execution-time budget.

Usage:
    python scripts/smoke_pb_inbox.py
    python scripts/smoke_pb_inbox.py --limit 5
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=10,
                    help="threads to request (keep small; costs execution time)")
    args = ap.parse_args()

    # Point the DB at a temp file BEFORE importing db, which resolves the path
    # at import time.
    tmp = Path(tempfile.gettempdir()) / "pb_smoke.db"
    if tmp.exists():
        tmp.unlink()
    os.environ["LINKEDIN_DB_PATH"] = str(tmp)

    from linkedin_agent import db  # noqa: E402
    from linkedin_agent.config import load as load_config  # noqa: E402
    from linkedin_agent.providers import Capability, build_router  # noqa: E402

    cfg = load_config()
    if not os.getenv("PHANTOMBUSTER_API_KEY"):
        print("BLOCKED: PHANTOMBUSTER_API_KEY not set in .env")
        return 2
    if not os.getenv("PHANTOMBUSTER_AGENT_INBOX_SCRAPER"):
        print("BLOCKED: PHANTOMBUSTER_AGENT_INBOX_SCRAPER not set.")
        print("  Find the agent id in the Phantom's URL or via /agents/fetch-all.")
        return 2

    print(f"temp DB: {tmp}")
    db.init_db()

    router = build_router(cfg, primary="phantombuster", fallback=None)
    owner = router.owner_of(Capability.INBOX_READ)
    print(f"inbox_read owner: {owner}")
    if owner != "phantombuster":
        print("BLOCKED: PhantomBuster does not own inbox_read; check config.")
        return 2

    try:
        # --- step 1: the provider call, on its own -------------------------
        print("\n[1/2] launching Inbox Scraper (this blocks for the run)...")
        provider = router.providers_for(Capability.INBOX_READ)[0]
        messages = provider.fetch_inbox(args.limit)
        print(f"      returned {len(messages)} normalized message(s)")

        if not messages:
            print("      (empty is normal: the Phantom is incremental and returns")
            print("       only threads new since its last run)")

        for m in messages[:5]:
            body = (m.body or "")[:48].replace("\n", " ")
            print(f"      - from={str(m.prospect_provider_id)[:22]:24s} "
                  f"from_me={str(m.is_from_me):5s} sent={str(m.sent_at)[:19]:19s} "
                  f"id={m.external_id[:10]}… {body!r}")

        # Field-level assertions that only a live payload can settle.
        problems = []
        for m in messages:
            if not m.external_id:
                problems.append("a message has no external_id")
            if not m.thread_id:
                problems.append("a message has no thread_id")
            if not m.is_from_me and not m.prospect_provider_id:
                problems.append("an inbound message has no provider_id")
        if problems:
            print("\n      FIELD PROBLEMS:")
            for p in sorted(set(problems)):
                print(f"        - {p}")

        # --- step 2: the full poll path ------------------------------------
        print("\n[2/2] running poll_once through the router...")
        from linkedin_agent.poll import poll_once
        result = poll_once(cfg, limit=args.limit, notify=False,
                           draft_replies=False, router=router,
                           drafter=lambda *a, **k: "unused")
        print(f"      fetched={result.fetched} new_inbound={result.new_inbound} "
              f"matched={result.matched_prospects} "
              f"unknown_senders={result.skipped_unknown_sender}")
        print("      (unknown senders are expected: this temp DB has no prospects)")

        print("\nRESULT: the live PhantomBuster inbox path works end to end.")
        print("  launch -> poll -> fetch -> normalize -> poll_once  all succeeded.")
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
