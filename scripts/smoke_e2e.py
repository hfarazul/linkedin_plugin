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
from dataclasses import replace
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Imported at module scope, unlike the rest: _stub_email() is a module-level
# function and the evidence module touches no database, so it does not need to
# wait for LINKEDIN_DB_PATH the way db/drafter do.
from linkedin_agent import evidence as evidence_mod  # noqa: E402

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
    ap.add_argument("--skip-geo", action="store_true",
                    help="Bypass the geography check so a profile outside "
                         "the campaign's target region still reaches the "
                         "later stages. Test harness only -- the ICP rules "
                         "used by real campaigns are untouched.")
    ap.add_argument("--skip-role", action="store_true",
                    help="Bypass the headline role check. The campaign pitch "
                         "is written for a specific buyer, so a run using "
                         "this previews an email you would not actually "
                         "send. Test harness only.")
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
    from linkedin_agent.trace import say
    from linkedin_agent.config import load as load_config
    from linkedin_agent.icp_scoring import CampaignICP, grade
    from linkedin_agent.providers import Capability, build_router
    from linkedin_agent.adapters.base import ProspectHit

    cfg = load_config()
    primary = os.getenv("LINKEDIN_PRIMARY_PROVIDER", "phantombuster")
    fallback = os.getenv("LINKEDIN_FALLBACK_PROVIDER") or None

    final_email: dict | None = None
    prospect_id: int | None = None
    drafted_subject: str | None = None
    aborted: str | None = None

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
                brief = None
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
                if args.skip_role:
                    icp = replace(icp, role_required=re.compile(r""))
                if args.skip_geo:
                    # Replace the campaign's pattern rather than patching the
                    # result, so geo_match reports what was actually evaluated
                    # instead of a hand-set True.
                    icp = replace(icp, geo_required=re.compile(r""))
                # Every role they currently hold, not just the one their
                # headline leads with. Anjan B is a co-founder of one company
                # and an engineer at another; his headline names only the
                # engineering job, so headline-only scoring dropped a founder
                # from a founder campaign.
                current_titles = [pos.title for pos in facts.positions
                                  if pos.is_current and pos.title]
                st.note("titles_considered", current_titles or "(headline only)")
                result = grade(hit, icp, titles=current_titles)
                st.note("geo_bypassed", bool(args.skip_geo))
                st.note("role_bypassed", bool(args.skip_role))
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
                elif args.skip_geo or args.skip_role:
                    bypassed = ", ".join(
                        n for n, on in (("geo", args.skip_geo),
                                        ("role", args.skip_role)) if on)
                    st.why(f"qualified only because the {bypassed} gate(s) "
                           f"were bypassed for this run; a real campaign "
                           f"would have stopped here")
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
                        # A video or image post scrapes with no postContent.
                        # It is a valid target for a reaction but gives the
                        # drafter nothing to reference, so the two counts are
                        # reported separately rather than as one number.
                        quotable = [p for p in posts if (p.text or "").strip()]
                        st.note("posts_with_text", len(quotable))
                        for i, p in enumerate(quotable[:2]):
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
                        # positions[1] is whatever LinkedIn lists second, which
                        # is not necessarily a role they have left. Anjan B
                        # holds both concurrently, and calling dan Lab
                        # "previous" would tell the drafter he used to
                        # co-found a company he still co-founds.
                        label = "also current" if prev.is_current else "previous"
                        context_bits.append(
                            f"{label}: {prev.title} at {prev.company}")
                if facts.company_employee_count:
                    context_bits.append(f"company size: {facts.company_employee_count}")
                quotable_posts = [p for p in posts if (p.text or "").strip()]
                if quotable_posts:
                    context_bits.append(f"{len(quotable_posts)} recent post(s)")
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

                # The typed view. `context_bits` above is a display string;
                # this is what actually governs what may be said, and the two
                # are kept separate so a pretty summary can never be mistaken
                # for a licence to make a claim.
                direct = bool(len(facts.positions) > 1 and is_direct_transition(
                    facts.positions[1], facts.positions[0]))
                bundle = evidence_mod.build_evidence(
                    facts, posts, transition_is_direct=direct)
                st.note("evidence_tier", bundle.tier.value)
                st.note("pain_claim_licensed", bundle.pain_claim_licensed)
                st.note("verified_facts", len(bundle.facts))
                st.note("observations", len(bundle.observations))
                st.note("signals", [e.source for e in bundle.signals] or "none")
                st.note("unknowns", len(bundle.unknowns))
                if bundle.tier is evidence_mod.Tier.NONE:
                    st.status = trace.SKIP
                    st.why("no specific facts to personalize from — the drafter "
                           "would return INSUFFICIENT_CONTEXT")
                elif bundle.pain_claim_licensed:
                    st.why("a signal the prospect published licenses a claim "
                           "about their situation, tied to that signal")
                else:
                    st.why(f"tier={bundle.tier.value}: facts may be referenced, "
                           f"but NO claim about their problems is licensed")

            # ---- 11 DRAFTING --------------------------------------------
            body = None
            with run.step("MESSAGE_DRAFTING",
                          mode=trace.LIVE if args.real_drafter else trace.STUBBED) as st:
                st.note("kind", args.kind)
                st.note("max_chars", drafter_mod.KIND_MAX_CHARS.get(args.kind))
                st.note("min_chars", drafter_mod.KIND_MIN_CHARS.get(args.kind))
                st.note("evidence_tier", bundle.tier.value)
                st.note("pain_claim_licensed", bundle.pain_claim_licensed)
                if args.real_drafter:
                    raw = drafter_mod.draft(
                        args.kind, prospect_id,
                        recent_posts=[{"text": p.text, "posted_at": p.posted_at}
                                      for p in posts],
                        evidence=bundle.as_dict())
                    drafted_subject, body = drafter_mod.parse_email(raw)
                    st.note("subject_from_drafter", drafted_subject or "(none)")
                    st.why("generated by `claude -p` from the typed evidence "
                           "bundle, through every gate including the "
                           "unsupported-pain-claim and template-filler checks")
                else:
                    body = _stub_email(facts, bundle)
                    st.note("shaped_by_tier", bundle.tier.value)
                    st.why("STUBBED: no `claude -p` call, but built from the "
                           "same evidence bundle and subject to the same "
                           "gates. Pass --real-drafter for the live drafter")
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
                filler = drafter_mod._contains_filler(body)
                # A diagnosis is only permitted when the prospect themselves
                # published something implying the problem.
                pain = (None if bundle.pain_claim_licensed
                        else drafter_mod._contains_unsupported_pain_claim(body))
                # At the thin tiers, arguing that their category makes us
                # relevant is the same invention with a hedge on it.
                inferred = (drafter_mod._contains_inferred_relevance(body)
                            if bundle.tier in (evidence_mod.Tier.WEAK,
                                               evidence_mod.Tier.NONE)
                            and not bundle.pain_claim_licensed else None)
                # Claims about US are grounded in the brief, not in the
                # evidence about them.
                grounding = evidence_mod.CortivoGrounding(
                    brief.brief if brief else "",
                    drafter_mod._shared_positioning())
                invented = evidence_mod.ungrounded_cortivo_claim(body, grounding)
                st.note("within_length", floor <= len(body) <= cap)
                st.note("template_filler", filler or "none")
                st.note("unsupported_pain_claim", pain or "none")
                st.note("inferred_relevance", inferred or "none")
                st.note("ungrounded_cortivo_claim", invented or "none")
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
                if filler:
                    problems.append(f"recycled template phrase {filler!r}")
                if pain:
                    problems.append(f"unsupported pain claim {pain!r} — no "
                                    f"signal licenses a claim about this "
                                    f"person's problems")
                if inferred:
                    problems.append(f"inferred relevance {inferred!r} — "
                                    f"reasoned from their category, not from "
                                    f"anything they said")
                if invented:
                    problems.append(f"ungrounded Cortivo claim: {invented}")
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
                "subject": drafted_subject or evidence_mod.subject_for(facts, bundle),
                "body": body,
            }
        except Exception as exc:
            # A stage that dies must not take the summary with it. The first
            # run against this profile lost DNS mid-scrape and printed sixty
            # lines of traceback and no summary at all, so nothing said which
            # stages had already passed or how far the pipeline got. The step
            # that failed is already recorded; stop the pipeline, keep the
            # report.
            aborted = f"{type(exc).__name__}: {trace.redact(str(exc))[:200]}"
        finally:
            if router is not None:
                router.close()

    # ------------------------------------------------------------- summary
    say()
    say(run.summary(header={
        "Profile": args.profile,
        "Primary provider": primary,
        "Fallback provider": fallback or "(none)",
        "Temp DB": str(tmp),
    }))

    if final_email:
        say()
        say("FINAL MESSAGE")
        say(f"To: {final_email['to']}")
        say(f"Subject: {final_email['subject']}")
        say("Body:")
        for line in final_email["body"].splitlines():
            say(f"  {line}")
        say()
        say("NOT SENT. Email sending is not implemented (Phase 5): there is no")
        say("EmailAdapter, no address discovery, and no suppression list. This")
        say("stage renders what would be sent and stops.")
    say("=" * 64)

    if aborted:
        say()
        say(f"RUN ABORTED after the stage above: {aborted}")
        say("Stages not listed above were never reached.")

    return 1 if (run.failed() or aborted) else 0


def _quotable_sentence(text: str, limit: int = 140) -> str:
    """A whole sentence of theirs, or nothing.

    Quoting someone back to themselves is only worth doing if the quote is
    intact. A character-count truncation produced 'using coding agents for a',
    which reads worse than not quoting them at all — it shows the machine.
    """
    clean = " ".join(text.split())
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    for sentence in sentences:
        if 25 <= len(sentence) <= limit:
            return sentence.rstrip()
    # No sentence is a usable length; better to say nothing than to cut one.
    return ""


def _stub_email(facts, bundle) -> str:
    """The email this evidence supports — nothing more.

    Not a template with slots. The three branches are genuinely different
    emails because the three evidence states are genuinely different
    situations, and collapsing them into one shape with the nouns swapped is
    what made every prospect receive the same message.

    The weak branch is the important one. It is short, it diagnoses nothing,
    and it is meant to look plain: at that tier a plain honest note is the
    correct output, not a failure to personalise.
    """
    first = (facts.full_name or "there").split()[0]
    company = ""
    if facts.positions:
        company = (facts.positions[0].company or "").strip()
    company = company or (facts.company_name or "").strip() or "your company"

    cortivo = ("I'm Haque, co-founder of Cortivo — we're a small engineering "
               "studio that builds custom software for teams who don't want to "
               "hire a whole in-house team to get something built.")
    ask = evidence_mod.ask_for(bundle)

    if bundle.tier is evidence_mod.Tier.STRONG:
        signal = bundle.signals[0]
        # Second-person phrasing comes from the rule, not from mangling the
        # third-person payload string — that produced "You wrote about are
        # hiring" on a live run.
        said, claim = evidence_mod.phrasing_for(signal)
        paragraphs = [
            f"Hi {first},",
            f"Saw {said} — that's why I'm writing, rather than a list I "
            f"pulled you off.",
            cortivo,
            f"If {claim}, that's usually where we're useful. Might be "
            f"completely off — you'd know better than me.",
            ask,
        ]
    elif (bundle.tier is evidence_mod.Tier.MODERATE
          and _quotable_sentence(bundle.observations[0].detail or "")):
        post = bundle.observations[0]
        excerpt = _quotable_sentence(post.detail or "")
        paragraphs = [
            f"Hi {first},",
            f'Your post — "{excerpt}" — is what prompted this, so this isn\'t '
            f"going to a list.",
            cortivo,
            f"No idea whether that's relevant to what you're doing at "
            f"{company}; I'm not going to pretend I know your situation from "
            f"the outside.",
            ask,
        ]
    else:
        move = next((e.statement for e in bundle.facts
                     if e.statement.startswith("recently moved")), None)
        why = (f"Saw you {move}, which is the only reason I'm writing — "
               f"no list involved."
               if move else
               f"You're at {company}, which is the whole reason I'm writing. "
               f"I'll be straight that I don't know much beyond that.")
        paragraphs = [
            f"Hi {first},",
            why,
            cortivo,
            "I've no idea whether that's useful to you right now, and I'm not "
            "going to guess at what's on your plate.",
            ask,
        ]

    paragraphs.append("Best,\nHaque\nCortivo")
    return "\n\n".join(paragraphs)


if __name__ == "__main__":
    raise SystemExit(main())
