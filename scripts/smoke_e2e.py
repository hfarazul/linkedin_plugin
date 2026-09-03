#!/usr/bin/env python
"""End-to-end pipeline smoke test: one LinkedIn profile to a final message.

Runs the real pipeline against a throwaway DB and narrates every stage: which
provider served it, which capability was requested, how long it took, what it
produced, and why a record was accepted or rejected.

    python scripts/smoke_e2e.py --profile https://www.linkedin.com/in/<slug>

Stage modes are labelled honestly, because a green run made of stubs proves
nothing:

    LIVE            a real provider call happened
    STUBBED         deliberately faked to avoid cost or side effects
    NOT_IMPLEMENTED the capability does not exist in this codebase yet

Safety, all default-on:
  * throwaway DB, never data/outreach.db
  * no LinkedIn writes: this reads profiles, it does not connect or message
  * no Telegram unless --real-telegram
  * no email sent, ever — sending is not implemented (Phase 5)
  * drafting is stubbed unless --real-drafter (which spends `claude -p` calls)

Known gap this test makes visible rather than hides: email SENDING does not
exist. There is no EmailAdapter, no address discovery, and no `email` column.
Stage 12 renders the email that would be sent and stops.
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

TOTAL_STAGES = 12


class RecordingTelegram:
    def __init__(self, cfg=None):
        self.cards = []
        self._next = 900

    def push_draft_for_approval(self, draft_id, kind, body, prospect_name, **kw):
        self._next += 1
        self.cards.append({"draft_id": draft_id, "kind": kind,
                           "prospect_name": prospect_name})
        return self._next

    def notify_reply(self, *a, **k):
        self._next += 1
        return self._next

    def notify_text(self, text):
        self._next += 1
        return self._next

    def close(self):
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True,
                    help="LinkedIn profile URL or ACoAA… provider id")
    ap.add_argument("--campaign", default="recently-funded-non-tech",
                    help="campaign slug whose brief and ICP rules to apply")
    ap.add_argument("--kind", default="email1",
                    choices=["email1", "dm1", "connect_note"],
                    help="what to draft at the end (default: email1)")
    ap.add_argument("--real-drafter", action="store_true",
                    help="invoke `claude -p` instead of a stub")
    ap.add_argument("--real-telegram", action="store_true")
    args = ap.parse_args()

    import logging
    logging.getLogger("linkedin").setLevel(logging.ERROR)
    logging.getLogger("linkedin.providers").setLevel(logging.CRITICAL)

    tmp = Path(tempfile.gettempdir()) / "e2e_smoke.db"
    if tmp.exists():
        tmp.unlink()
    os.environ["LINKEDIN_DB_PATH"] = str(tmp)

    from linkedin_agent import campaigns as campaigns_mod
    from linkedin_agent import db, drafter as drafter_mod, trace
    from linkedin_agent.config import load as load_config
    from linkedin_agent.icp_scoring import CampaignICP, grade
    from linkedin_agent.providers import Capability, build_router
    from linkedin_agent.adapters.base import ProspectHit

    cfg = load_config()
    primary = os.getenv("LINKEDIN_PRIMARY_PROVIDER", "phantombuster")
    fallback = os.getenv("LINKEDIN_FALLBACK_PROVIDER") or None

    final_email: dict | None = None
    prospect_id: int | None = None

    with trace.run(total=TOTAL_STAGES) as run:
        router = None
        try:
            # ---- 01 DISCOVERY -------------------------------------------
            with run.step("PROFILE_DISCOVERY") as st:
                st.note("input", args.profile)
                st.note("primary_provider", primary)
                st.note("fallback_provider", fallback or "(none)")
                db.init_db()
                st.note("db", f"schema initialised at {db.DB_PATH.name}")
                router = build_router(cfg, primary=primary, fallback=fallback)
                identifier = args.profile
                if identifier.startswith("http"):
                    slug = identifier.rstrip("/").rsplit("/", 1)[-1]
                else:
                    slug = identifier
                st.note("identifier", slug)
                st.why("profile supplied directly; no search performed "
                       "(search_people is unverified on PhantomBuster)")

            # ---- 02 ROUTING ---------------------------------------------
            with run.step("CAPABILITY_ROUTING") as st:
                table = {}
                for cap in (Capability.PROFILE, Capability.EXPERIENCE,
                            Capability.RECENT_POSTS, Capability.SEND_DM):
                    table[cap.value] = router.owner_of(cap) or "NONE"
                for k, v in table.items():
                    st.note(k, v)
                if table["profile"] == "NONE":
                    st.status = trace.FAIL
                    raise RuntimeError("no provider can serve Capability.PROFILE")
                st.why(f"profile will be served by {table['profile']}")

            # ---- 03 ENRICHMENT (live provider call) ----------------------
            with run.step("PROFILE_ENRICHMENT",
                          capability=Capability.PROFILE.value) as st:
                st.note("identifier", slug)
                facts = router.perform(Capability.PROFILE, "fetch_profile",
                                       slug, with_experience=True)
                if facts is None:
                    st.status = trace.FAIL
                    raise RuntimeError("provider returned no profile "
                                       "(locked profile, 404, or bad identifier)")
                st.note("full_name", facts.full_name)
                st.note("headline", facts.headline)
                st.note("location", facts.location)
                st.note("provider_id", facts.provider_id)
                st.note("network_distance", facts.network_distance)
                st.note("email_on_profile", facts.email or "(none)")
                st.why("normalized into ProfileFacts; raw provider payload not "
                       "propagated beyond this boundary")

            # ---- 04 NORMALIZATION + PROSPECT UPSERT ----------------------
            with run.step("PROSPECT_MATCHING") as st:
                linkedin_url = facts.linkedin_url or (
                    args.profile if args.profile.startswith("http")
                    else f"https://www.linkedin.com/in/{slug}")
                existing = (db.get_prospect_by_provider_id(facts.provider_id)
                            if facts.provider_id else None)
                st.note("matched_existing", bool(existing))
                st.note("match_key", "provider_id" if facts.provider_id else "linkedin_url")
                prospect_id = db.upsert_prospect(
                    linkedin_url=linkedin_url, full_name=facts.full_name,
                    headline=facts.headline, location=facts.location,
                    company=facts.company_name, provider_id=facts.provider_id)
                st.note("db_write", f"prospects id={prospect_id} (upsert)")
                st.why("created" if not existing else
                       f"updated existing prospect {existing['id']}")

            # ---- 05 EXPERIENCE ------------------------------------------
            with run.step("WORK_EXPERIENCE",
                          capability=Capability.EXPERIENCE.value) as st:
                stored = db.replace_positions(prospect_id, facts.positions) \
                    if facts.positions else 0
                st.note("positions_returned", len(facts.positions))
                st.note("db_write", f"positions rows={stored}")
                for i, p in enumerate(facts.positions[:3]):
                    st.note(f"position_{i}",
                            f"{p.title or '?'} @ {p.company} "
                            f"[{p.start_date or '?'}..{p.end_date or 'present'}] "
                            f"precision={p.date_precision} current={p.is_current}")
                usable = [p for p in facts.positions if p.has_usable_start]
                st.note("usable_for_recency", f"{len(usable)}/{len(facts.positions)}")
                if not facts.positions:
                    st.status = trace.SKIP
                    st.why("provider returned no work history")
                else:
                    st.why(f"{len(usable)} position(s) have month-or-better date "
                           f"precision; the rest cannot support a recency window")

            # ---- 06 COMPANY DATA ----------------------------------------
            with run.step("COMPANY_DATA") as st:
                st.note("company", facts.company_name or "(none)")
                st.note("employee_count", facts.company_employee_count)
                st.note("size_label", facts.company_size_label or "(none)")
                if facts.company_employee_count is None:
                    st.status = trace.SKIP
                    st.why("provider supplied no firmographics "
                           "(Unipile does not; PhantomBuster does)")
                else:
                    st.why("firmographics available — no curated employer list needed")

            # ---- 07 ICP SCORING -----------------------------------------
            with run.step("ICP_SCORING") as st:
                try:
                    brief = campaigns_mod.load_brief(args.campaign)
                    meta, _ = campaigns_mod._parse_frontmatter(brief.path.read_text())
                    icp = CampaignICP.from_brief_meta(meta)
                    st.note("campaign", f"{brief.name} ({args.campaign})")
                except FileNotFoundError:
                    icp = CampaignICP()
                    st.note("campaign", f"{args.campaign} (not found, using defaults)")
                hit = ProspectHit(linkedin_url=linkedin_url,
                                  full_name=facts.full_name,
                                  headline=facts.headline,
                                  location=facts.location)
                result = grade(hit, icp)
                st.note("geo_match", result.geo_match)
                st.note("role_match", result.role_match)
                st.note("noise_excluded", result.noise_excluded)
                st.note("is_keeper", result.is_keeper)
                st.why("; ".join(result.notes) if result.notes
                       else "all three ICP signals passed")

            # ---- 08 QUALIFICATION ---------------------------------------
            with run.step("CAMPAIGN_QUALIFICATION") as st:
                qualified = result.is_keeper
                st.note("decision", "qualified" if qualified else "not_qualified")
                if not qualified:
                    st.status = trace.SKIP
                    st.why("ICP gate failed — continuing anyway so the rest of "
                           "the pipeline stays observable in this test")
                else:
                    st.why("passes the campaign's ICP rules")

            # ---- 09 ACTIVITY --------------------------------------------
            posts: list = []
            recent_owner = router.owner_of(Capability.RECENT_POSTS)
            with run.step("ACTIVITY_RETRIEVAL",
                          mode=trace.LIVE if recent_owner else trace.NOT_IMPLEMENTED,
                          capability=Capability.RECENT_POSTS.value) as st:
                if not recent_owner:
                    st.status = trace.SKIP
                    st.why("no provider serves recent_posts "
                           "(PhantomBuster Activity Extractor unverified, task T-3 family)")
                else:
                    try:
                        posts = router.perform(Capability.RECENT_POSTS,
                                               "get_recent_posts", linkedin_url, 3)
                        st.note("posts_found", len(posts))
                        for i, p in enumerate(posts[:2]):
                            st.note(f"post_{i}", (p.text or "")[:100])
                        st.why("post text becomes drafter hook material")
                    except Exception as e:
                        st.status = trace.SKIP
                        st.error_class = trace.classify_error(e)
                        st.why(f"activity unavailable ({st.error_class}); "
                               f"drafting continues without post hooks")

            # ---- 10 PERSONALIZATION CONTEXT ------------------------------
            with run.step("PERSONALIZATION_CONTEXT") as st:
                context_bits = []
                if facts.positions:
                    cur = facts.positions[0]
                    context_bits.append(f"current: {cur.title} at {cur.company}")
                    if len(facts.positions) > 1:
                        prev = facts.positions[1]
                        context_bits.append(f"previous: {prev.title} at {prev.company}")
                if facts.company_employee_count:
                    context_bits.append(f"company size: {facts.company_employee_count}")
                if posts:
                    context_bits.append(f"{len(posts)} recent post(s)")
                pitch_context = ". ".join(context_bits) or None
                if pitch_context:
                    db.set_pitch_context(prospect_id, pitch_context) \
                        if hasattr(db, "set_pitch_context") else None
                st.note("pitch_context", pitch_context or "(none)")
                st.note("evidence_available", len(context_bits))
                if len(facts.positions) > 1:
                    from linkedin_agent.providers.capabilities import (
                        _months_between, is_direct_transition)
                    a, b = facts.positions[1], facts.positions[0]
                    gap = _months_between(a.end_date, b.start_date)
                    st.note("transition_is_direct", is_direct_transition(a, b))
                    st.note("gap_months", "unknown" if gap is None else gap)
                if not context_bits:
                    st.status = trace.SKIP
                    st.why("no specific facts to personalize from — the drafter "
                           "would return INSUFFICIENT_CONTEXT")
                else:
                    st.why(f"{len(context_bits)} grounded fact(s) available to the drafter")

            # ---- 11 DRAFTING --------------------------------------------
            body = None
            with run.step("MESSAGE_DRAFTING",
                          mode=trace.LIVE if args.real_drafter else trace.STUBBED) as st:
                st.note("kind", args.kind)
                st.note("max_chars", drafter_mod.KIND_MAX_CHARS.get(args.kind))
                st.note("min_chars", drafter_mod.KIND_MIN_CHARS.get(args.kind))
                if args.real_drafter:
                    body = drafter_mod.draft(
                        args.kind, prospect_id,
                        recent_posts=[{"text": p.text, "posted_at": p.posted_at}
                                      for p in posts])
                    st.why("generated by `claude -p` through the real drafter, "
                           "including its length, spam-tell and audience-label gates")
                else:
                    cur = facts.positions[0] if facts.positions else None
                    prev = facts.positions[1] if len(facts.positions) > 1 else None
                    first = (facts.full_name or "there").split()[0]
                    now_co = cur.company if cur else "your new company"
                    # Narrative, not database. The transition is described by
                    # what they built and where they went — never by dates or
                    # headcount, which read as surveillance in a cold email.
                    from linkedin_agent.providers.capabilities import (
                        is_direct_transition)
                    # Only claim a move when the two roles are actually
                    # consecutive. PhantomBuster returns current + one
                    # other, and that other can be years earlier with
                    # unknown roles in between.
                    direct = bool(prev) and is_direct_transition(prev, cur)
                    if direct:
                        built = (prev.description or "").strip().rstrip(".")
                        # Whole clause or nothing: a mid-word truncation
                        # reads as machine output.
                        what = (f"building {prev.company}'s {built.split('.')[0]}"
                                if built and len(built.split('.')[0]) <= 70
                                else f"your time at {prev.company}")
                        move = f"the move from {what} to {now_co}"
                    else:
                        # Fall back to what is certain: the current role.
                        move = f"the work you are doing at {now_co}"
                    body = (
                        f"Hi {first},\n\n"
                        f"We have yet to be properly introduced, but I'm Haque "
                        f"with Cortivo, and what caught my eye is {move}.\n\n"
                        "Most founders at that transition point end up rebuilding "
                        "internal tooling and data pipelines by hand while the "
                        "product gets the attention. We would build the custom "
                        "systems that take that load off, tailored to how your "
                        "company actually works.\n\n"
                        f"That's our outside read. Curious if the real squeeze at "
                        f"{now_co} is closer to go-to-market ops or product "
                        "velocity, or somewhere we haven't surfaced.\n\n"
                        "Do you have time this week or early next to walk through "
                        "what we'd build? Let me know what works and I'll send "
                        "the invite.\n\n"
                        "Best,\nHaque Farazul\nCortivo")
                    st.why("STUBBED: no `claude -p` call. Pass --real-drafter to "
                           "exercise the live drafter and its quality gates")
                st.note("length", len(body))

            # ---- 12 VALIDATION ------------------------------------------
            with run.step("VALIDATION") as st:
                cap = drafter_mod.KIND_MAX_CHARS.get(args.kind, 10_000)
                floor = drafter_mod.KIND_MIN_CHARS.get(args.kind, 0)
                spam = drafter_mod._contains_spam_tell(body)
                surveillance = (drafter_mod._contains_surveillance_tell(body)
                                if args.kind.startswith("email") else None)
                has_link = bool(re.search(r"https?://|cal\.com", body))
                label_check = getattr(drafter_mod, '_contains_audience_label', None)
                label = label_check(body) if label_check else None
                st.note("within_length", floor <= len(body) <= cap)
                st.note("spam_tell", spam or "none")
                if args.kind.startswith("email"):
                    st.note("surveillance_tell", surveillance or "none")
                    st.note("contains_link", has_link)
                st.note("audience_label",
                        (label or "none") if label_check
                        else "gate not present on this branch (PR #1)")
                problems = []
                if not (floor <= len(body) <= cap):
                    problems.append(f"length {len(body)} outside [{floor}, {cap}]")
                if spam:
                    problems.append(f"spam tell {spam!r}")
                if surveillance:
                    problems.append(f"scraped detail {surveillance!r} "
                                    f"(reads as surveillance)")
                if has_link and args.kind.startswith("email"):
                    problems.append("cold email contains a link")
                if label:
                    problems.append(f"audience label {label!r}")
                if problems:
                    st.status = trace.FAIL
                    st.error_class = "VALIDATION_FAILURE"
                    st.error = "; ".join(problems)
                    st.why("draft would be rejected and retried by the drafter")
                else:
                    st.why("passes every gate the production drafter applies")

                draft_id = db.enqueue_draft(prospect_id, args.kind, body)
                st.note("db_write", f"pending_drafts id={draft_id} status=pending")

                telegram = RecordingTelegram()
                if args.real_telegram:
                    from linkedin_agent.telegram import TelegramClient
                    telegram = TelegramClient(cfg)
                mid = telegram.push_draft_for_approval(
                    draft_id=draft_id, kind=args.kind, body=body,
                    prospect_name=facts.full_name)
                db.set_draft_telegram_id(draft_id, mid)
                st.note("approval_card",
                        "SENT to Telegram" if args.real_telegram
                        else "recorded in-process (use --real-telegram to send)")

            final_email = {
                "to": facts.email or "(no address — discovery not implemented)",
                "subject": _subject_for(facts),
                "body": body,
            }
        finally:
            if router is not None:
                router.close()

    # ------------------------------------------------------------- summary
    print()
    print(run.summary(header={
        "Profile": args.profile,
        "Primary provider": primary,
        "Fallback provider": fallback or "(none)",
        "Temp DB": str(tmp),
    }))

    if final_email:
        print()
        print("FINAL MESSAGE")
        print(f"To: {final_email['to']}")
        print(f"Subject: {final_email['subject']}")
        print("Body:")
        for line in final_email["body"].splitlines():
            print(f"  {line}")
        print()
        print("NOT SENT. Email sending is not implemented (Phase 5): there is no")
        print("EmailAdapter, no address discovery, and no suppression list. This")
        print("stage renders what would be sent and stops.")
    print("=" * 64)

    return 1 if run.failed() else 0


def _subject_for(facts) -> str:
    if facts.positions:
        cur = facts.positions[0]
        return f"{cur.company} — shipping without hiring a team"
    return "Quick question about your build side"


if __name__ == "__main__":
    raise SystemExit(main())
