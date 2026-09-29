"""The eight-email cadence: one thread, with state, never eight independent
drafts.

The GTM brief's rules, and what each test pins:

  * "do not use a single content piece in more than 3 emails (never 3 emails
    in a row)" — the ledger, enforced at selection
  * "if no or not-enough shared content ... speculate their problems" — a
    HYPOTHESIS per email, asked about, never stated
  * "at least 3 follow ups: light on words but asking questions"
  * "thread the subject and have their first name in the first or second
    word of the subject"
  * "except the follow up emails ... at least 80 words long (and always less
    than 120 words)"
  * the career angle — tenure may choose the question, never be said back
  * and the one place the cadence refuses the brief: with nothing fresh left
    to say, it stops rather than pads
"""

from __future__ import annotations

from datetime import date

import pytest

from linkedin_agent import drafter as d
from linkedin_agent import evidence as ev
from linkedin_agent import sequence as seq
from linkedin_agent.providers.capabilities import Position, ProfileFacts

TODAY = date(2026, 9, 26)
SIGN_OFF = "Best,\nManav\nAgentic Labs"
BRIEF = d._shared_positioning()


def _pos(company, title, start, end=None, current=False):
    return Position(company=company, title=title, start_date=start,
                    end_date=end, is_current=current, date_precision="month")


COLIN = ProfileFacts(
    full_name="Colin Parker",
    headline="Chief Operating Officer - AmTrust Specialty Limited",
    positions=[
        _pos("AmTrust International", "Chief Operating Officer", "2023-05-01",
             current=True),
        _pos("AmTrust Financial Services, Inc.", "Underwriting Manager",
             "2018-08-01", "2020-05-01"),
    ])

LONG_CTO = ProfileFacts(
    full_name="Dana Wu", headline="CTO at Acme",
    positions=[_pos("Acme", "CTO", "2019-01-01", current=True)])

NOBODY = ProfileFacts(full_name="Pat Doe", headline="", positions=[])

UNMATCHED_ROLE = ProfileFacts(
    full_name="Lee Ray", headline="Sommelier",
    positions=[_pos("Vine & Co", "Sommelier", "2025-01-01", current=True)])

HIRING = "We're hiring senior backend engineers to join our team in London."
OWN = "Moved our claims triage onto one queue this quarter. Big change."
SHARED = "Evals are the new unit tests for any team shipping agents."


def _body(words: int, *, question: bool = True, subject: str | None = None,
          tag: str = "") -> str:
    filler = " ".join(f"w{tag}{i}" for i in range(words - 2))
    text = f"Hi Colin, {filler}" + ("?" if question else ".")
    if subject:
        text = f"Subject: {subject}\n\n{text}"
    return f"{text}\n\n{SIGN_OFF}"


class StubDrafter:
    """Returns a body that fits the touch, and remembers what it was given."""

    def __init__(self, fail_at: int | None = None):
        self.calls: list[dict] = []
        self.fail_at = fail_at

    def __call__(self, kind, prospect_id, *, recent_posts, evidence,
                 attempts_out, touch):
        self.calls.append({"kind": kind, "evidence": evidence, "touch": touch,
                           "recent_posts": recent_posts})
        n = touch["number"]
        if n == self.fail_at:
            raise d.DrafterError("all 3 drafter attempts failed; last=x")
        attempts_out.append(d.DraftAttempt(1, "accepted"))
        if kind == seq.MAIN:
            return _body(90, subject="Colin, a question" if n == 1 else None,
                         tag=str(n))
        return _body(20, tag=str(n))


def _run(facts=COLIN, posts=(), **kw):
    stub = kw.pop("stub", None) or StubDrafter()
    state = seq.generate(1, facts, list(posts), brief=BRIEF,
                         sign_off=SIGN_OFF, today=TODAY, draft_fn=stub, **kw)
    return state, stub


# ============================== the ledger =================================

@pytest.mark.unit
def test_a_piece_may_be_used_in_at_most_three_emails() -> None:
    ledger = seq.ContentLedger()
    for touch in (1, 3, 6):
        ledger.record("post-1", touch)
    assert not ledger.allows("post-1", 8)


@pytest.mark.unit
def test_a_piece_is_never_used_three_emails_in_a_row() -> None:
    ledger = seq.ContentLedger()
    ledger.record("post-1", 3)
    ledger.record("post-1", 4)
    assert not ledger.allows("post-1", 5)
    gapped = seq.ContentLedger()
    gapped.record("post-1", 2)
    gapped.record("post-1", 4)
    assert gapped.allows("post-1", 5)


@pytest.mark.unit
def test_the_ledger_refuses_a_use_it_does_not_allow() -> None:
    ledger = seq.ContentLedger({"post-1": [3, 4]})
    with pytest.raises(ValueError):
        ledger.record("post-1", 5)


# ============================== the thread =================================

@pytest.mark.unit
def test_eight_emails_four_of_them_short_follow_ups() -> None:
    state, stub = _run()
    assert [t.kind for t in state.touches] == [
        seq.MAIN, seq.FOLLOWUP, seq.MAIN, seq.MAIN,
        seq.FOLLOWUP, seq.MAIN, seq.FOLLOWUP, seq.FOLLOWUP]
    assert state.stopped is None
    assert all(c["touch"]["words"] == {"max": d.FOLLOWUP_MAX_WORDS}
               for c in stub.calls if c["kind"] == seq.FOLLOWUP)
    assert all(c["touch"]["words"] == {"min": 80, "max": 119}
               for c in stub.calls if c["kind"] == seq.MAIN)


@pytest.mark.unit
def test_the_subject_is_written_once_and_threaded_after() -> None:
    state, stub = _run()
    assert stub.calls[0]["touch"]["subject"] is None
    assert state.subject == "Colin, a question"
    assert all(c["touch"]["subject"] == "Re: Colin, a question"
               for c in stub.calls[1:])
    assert all(t.subject == "Re: Colin, a question" for t in state.touches[1:])


@pytest.mark.unit
def test_every_email_sees_the_whole_thread_before_it() -> None:
    _, stub = _run()
    for i, call in enumerate(stub.calls):
        earlier = call["touch"]["previous_emails"]
        assert [e["number"] for e in earlier] == list(range(1, i + 1))


SALES_LONG = ProfileFacts(
    full_name="Robin Hale", headline="Head of Sales at Kestrel",
    positions=[_pos("Kestrel", "Head of Sales", "2019-03-01", current=True)])


@pytest.mark.unit
@pytest.mark.parametrize("facts", [COLIN, LONG_CTO, SALES_LONG],
                         ids=["coo", "cto", "sales"])
def test_no_case_study_or_guess_is_used_twice(facts) -> None:
    """Every one of a long-tenured sales head's guesses is supported by the
    same case study (OrionQ), so this is where reuse would show."""
    state, _ = _run(facts=facts)
    proofs = [t.proof_point for t in state.touches if t.proof_point]
    guesses = [t.hypothesis for t in state.touches
               if t.hypothesis and t.kind == seq.MAIN]
    assert len(proofs) == len(set(proofs))
    assert len(guesses) == len(set(guesses))


@pytest.mark.unit
def test_consecutive_main_emails_never_close_the_same_way() -> None:
    state, _ = _run()
    closings = [t.closing for t in state.touches if t.kind == seq.MAIN]
    assert all(a != b for a, b in zip(closings, closings[1:]))


@pytest.mark.unit
def test_a_follow_up_carries_the_guess_it_follows_and_no_post() -> None:
    """So the gate that keeps a guess a question still applies to it."""
    _, stub = _run(posts=[{"text": OWN, "is_repost": False}])
    by_number = {c["touch"]["number"]: c for c in stub.calls}
    assert by_number[5]["touch"]["content"] is None
    assert (by_number[5]["touch"]["hypothesis"]["name"]
            == by_number[4]["touch"]["hypothesis"]["name"]
            if by_number[4]["touch"]["hypothesis"] else True)
    assert by_number[5]["evidence"]["observations"] == []


# ============================== content ====================================

@pytest.mark.unit
def test_an_email_sees_only_the_post_it_was_given() -> None:
    posts = [{"text": OWN, "is_repost": False},
             {"text": SHARED, "is_repost": True}]
    _, stub = _run(posts=posts)
    for call in stub.calls:
        given = call["touch"]["content"]
        seen = ([o["detail"] for o in call["evidence"]["observations"]]
                + [i["detail"] for i in call["evidence"]["interests"]])
        assert seen == ([given["text"]] if given else [])


@pytest.mark.unit
def test_a_signal_travels_only_with_the_post_that_made_it() -> None:
    """A claim licensed by a hiring post may appear only in an email that was
    given that post."""
    _, stub = _run(posts=[{"text": HIRING, "is_repost": False}])
    for call in stub.calls:
        given = call["touch"]["content"]
        assert call["evidence"]["pain_claim_licensed"] == bool(
            given and given["text"] == HIRING)


@pytest.mark.unit
def test_their_own_post_is_preferred_over_a_repost_for_the_opener() -> None:
    posts = [{"text": SHARED, "is_repost": True},
             {"text": OWN, "is_repost": False}]
    _, stub = _run(posts=posts)
    assert stub.calls[0]["touch"]["content"]["kind"] == "post"


@pytest.mark.unit
def test_usage_recorded_in_the_state_respects_the_rule() -> None:
    state, _ = _run(posts=[{"text": OWN, "is_repost": False}])
    for used in state.content_uses.values():
        assert len(used) <= seq.MAX_CONTENT_USES
        assert not any(b == a + 1 and c == b + 1
                       for a, b, c in zip(used, used[1:], used[2:]))


@pytest.mark.unit
def test_recurring_words_across_posts_are_themes() -> None:
    items = seq.content_items([
        {"text": "Underwriting automation is overdue."},
        {"text": "More on underwriting and claims queues."},
        {"text": "A note on claims handling."}])
    assert set(seq.content_themes(items)) >= {"underwriting", "claims"}
    assert seq.content_themes(items[:1]) == []


# ============================== stopping ===================================

@pytest.mark.unit
def test_nothing_is_drafted_when_nothing_is_verified() -> None:
    state, stub = _run(facts=NOBODY)
    assert state.touches == [] and stub.calls == []
    assert "nothing drafted" in state.stopped


@pytest.mark.unit
def test_it_stops_rather_than_pads() -> None:
    """A role nothing specific matches has one honest guess. After the opener
    uses it there is nothing fresh for email 3 — so there is no email 3."""
    state, _ = _run(facts=UNMATCHED_ROLE)
    assert [t.number for t in state.touches] == [1, 2]
    assert "email 3" in state.stopped and "padding" in state.stopped


@pytest.mark.unit
def test_a_failed_email_stops_the_thread_where_it_failed() -> None:
    """Later emails build on earlier ones; skipping one would leave the
    thread referring to an email that was never written."""
    state, _ = _run(stub=StubDrafter(fail_at=4))
    assert [t.number for t in state.touches] == [1, 2, 3]
    assert state.stopped.startswith("email 4 could not be drafted")


# ============================== hypotheses =================================

@pytest.mark.unit
def test_an_operations_leader_gets_operations_guesses() -> None:
    names = [h.name for h in ev.build_hypotheses(COLIN, today=TODAY)]
    assert names[:3] == ["manual_handoffs", "ai_pilots_stall",
                         "reporting_by_hand"]
    assert names[-1] == "where_ai_fits"


@pytest.mark.unit
def test_long_tenure_asks_about_a_lever_and_carries_no_figure() -> None:
    lever = ev.build_hypotheses(LONG_CTO, today=TODAY)[-1]
    assert lever.name == ev.NEXT_LEVER
    assert not any(ch.isdigit() for ch in lever.statement + (lever.asks or ""))


@pytest.mark.unit
def test_leaving_and_coming_back_is_not_a_long_stretch() -> None:
    """Colin was at AmTrust, left, and returned three years later."""
    names = [h.name for h in ev.build_hypotheses(COLIN, today=TODAY)]
    assert ev.NEXT_LEVER not in names


@pytest.mark.unit
def test_a_guess_never_raises_the_tier_or_licenses_a_claim() -> None:
    bundle = ev.build_evidence(COLIN, [])
    bundle.items.extend(ev.build_hypotheses(COLIN, today=TODAY))
    assert bundle.tier is ev.Tier.WEAK
    assert bundle.pain_claim_licensed is False
    assert all("licensed_only_as_a_question" in h
               for h in bundle.as_dict()["hypotheses"])


@pytest.mark.unit
def test_payloads_without_guesses_are_unchanged() -> None:
    assert "hypotheses" not in ev.build_evidence(COLIN, []).as_dict()


@pytest.mark.unit
def test_case_studies_come_from_the_brief_with_their_tier() -> None:
    text, citable = ev.proof_point_brief("OrionQ", BRIEF)
    assert "−80% manual tasks" in text and citable
    assert ev.proof_point_brief("Experial", BRIEF)[1] is False
    assert ev.proof_point_brief("Invented Co", BRIEF) is None


# ============================== the gates ==================================

GUESSES = {"hypotheses": [{"name": "reporting_by_hand"}]}


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    "Your reporting is still assembled by hand every month.",
    "You're pulling numbers from four systems for every report.",
])
def test_a_guess_said_about_them_as_fact_is_rejected(body) -> None:
    assert d._states_hypothesis_as_fact(body, GUESSES) is not None


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    "Something I see a lot with COOs is reporting rebuilt by hand. True for you?",
    "I'm curious whether your reporting still gets assembled by hand.",
    "A lot of operations teams assemble their reporting by hand.",
])
def test_a_guess_asked_about_or_seen_elsewhere_is_fine(body) -> None:
    assert d._states_hypothesis_as_fact(body, GUESSES) is None


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    "Feels like you might be stuck where you are.",
    "It could be the next chapter for your career.",
    "This is a chance to be the hero on AI.",
    "You've spent eight years at AmTrust, which is a long run.",
    "After 8 years with the company you know it inside out.",
])
def test_nothing_is_said_about_their_career(body) -> None:
    assert d._contains_career_diagnosis(body) is not None


@pytest.mark.unit
def test_the_senders_own_years_are_not_a_career_verdict() -> None:
    assert d._contains_career_diagnosis(
        "I've spent 20 years across sectors and leadership roles.") is None


@pytest.mark.unit
def test_a_case_study_is_the_teams_work_not_the_senders() -> None:
    assert d._claims_team_work_personally(
        "I built Experial, which Coca-Cola and Bosch piloted.", BRIEF)
    assert d._claims_team_work_personally(
        "We built Experial, which Coca-Cola and Bosch piloted.", BRIEF) is None


@pytest.mark.unit
def test_a_sentence_lifted_from_an_earlier_email_is_caught() -> None:
    earlier = (f"Hi Colin, most operations teams I talk to rebuild the same "
               f"report by hand every month.\n\n{SIGN_OFF}")
    again = (f"Hi Colin, as I said, operations teams I talk to rebuild the "
             f"same report by hand every month.\n\n{SIGN_OFF}")
    fresh = f"Hi Colin, one quick question on pilots.\n\n{SIGN_OFF}"
    assert d._repeats_previous_email(again, [earlier], SIGN_OFF)
    assert d._repeats_previous_email(fresh, [earlier], SIGN_OFF) is None


@pytest.mark.unit
def test_the_shared_sign_off_is_not_a_repeat() -> None:
    a = f"Hi Colin, first note entirely about pilots.\n\n{SIGN_OFF}"
    b = f"Hi Colin, second note entirely about reporting.\n\n{SIGN_OFF}"
    assert d._repeats_previous_email(b, [a], SIGN_OFF) is None


@pytest.mark.unit
def test_words_are_counted_without_subject_or_sign_off() -> None:
    body = f"Subject: Colin, one thing\n\nHi Colin, five words here?\n\n{SIGN_OFF}"
    assert d.cadence_words(body, SIGN_OFF) == 5


@pytest.mark.unit
@pytest.mark.parametrize("subject, ok", [
    ("Colin, a question on pilots", True),
    ("Quick one, Colin", False),
    ("Re Colin", True),
    (None, False),
])
def test_first_name_is_the_first_or_second_word(subject, ok) -> None:
    assert (d.subject_problem(subject, "Colin") is None) is ok


# ============================== draft() with a touch =======================

def _touch(n=3, kind=seq.MAIN, previous=()):
    return {"number": n, "of": 8, "kind": kind, "role": "r", "brief": "b",
            "words": ({"min": 80, "max": 119} if kind == seq.MAIN
                      else {"max": 50}),
            "subject": None if n == 1 else "Re: Colin, a question",
            "content": None, "hypothesis": None, "proof_point": None,
            "previous_emails": list(previous), "already_used": {}}


def _drive(monkeypatch, responses, touch, kind=seq.MAIN):
    seen = []

    def fake(prompt, timeout=90):
        seen.append(prompt)
        return responses[min(len(seen) - 1, len(responses) - 1)]

    monkeypatch.setattr(d, "_invoke_claude", fake)
    monkeypatch.setattr(
        d, "build_input",
        lambda kind, pid, recent_posts=None, evidence=None, touch=None:
        d.DrafterInput(kind=kind, campaign={"brief": ""},
                       prospect={"first_name": "Colin"}, evidence=evidence,
                       sender={"sign_off": SIGN_OFF}, touch=touch))
    tries: list = []
    evidence = ev.build_evidence(COLIN, []).as_dict()
    body = d.draft(kind, 1, evidence=evidence, attempts_out=tries, touch=touch)
    return body, tries, seen


def _prose(words: int, *, ask: bool = True) -> str:
    base = ("Something I run into often with operations leaders is work that "
            "moves between teams by email and gets rekeyed along the way, "
            "which is slow and hard to see. We built OrionQ for exactly that "
            "kind of work, and it took out most of the manual steps. ")
    extra = ("It plugs into the systems a team already runs rather than "
             "replacing them, so nothing big has to change first. ")
    text = (base + extra * 4).split()
    body = "Hi Colin, " + " ".join(text[:words - 2]).rstrip(".,") + (
        ". Does any of that sound familiar?" if ask else ".")
    return f"{body}\n\n{SIGN_OFF}"


@pytest.mark.unit
def test_a_main_email_outside_the_word_band_is_retried(monkeypatch) -> None:
    short, right = _prose(40), _prose(95)
    body, tries, seen = _drive(monkeypatch, [short, right], _touch())
    assert tries[0].category == "word_band"
    assert body == right
    assert "THIS EMAIL — 3 of 8" in seen[0]


@pytest.mark.unit
def test_a_follow_up_must_ask_something(monkeypatch) -> None:
    flat = f"Hi Colin, just bumping this up your inbox.\n\n{SIGN_OFF}"
    asks = f"Hi Colin, is any of this on your plate this quarter?\n\n{SIGN_OFF}"
    body, tries, _ = _drive(monkeypatch, [flat, asks],
                            _touch(2, seq.FOLLOWUP), kind=seq.FOLLOWUP)
    assert tries[0].category == "followup_without_question"
    assert body == asks


@pytest.mark.unit
def test_the_first_email_needs_the_first_name_in_its_subject(monkeypatch) -> None:
    bad = "Subject: A question about operations\n\n" + _prose(95)
    good = "Subject: Colin, a question about operations\n\n" + _prose(95)
    body, tries, _ = _drive(monkeypatch, [bad, good], _touch(1))
    assert tries[0].category == "subject_rule"
    assert body == good


@pytest.mark.unit
def test_a_cadence_email_may_not_link(monkeypatch) -> None:
    linked = _prose(95).replace("We built OrionQ",
                                "We built OrionQ (theagenticlabs.ai/work/orionq)")
    body, tries, _ = _drive(monkeypatch, [linked, _prose(95)], _touch())
    assert tries[0].category == "link"


@pytest.mark.unit
def test_the_cadence_prompt_does_not_tell_later_emails_to_reintroduce(
        monkeypatch) -> None:
    _, _, seen = _drive(monkeypatch, [_prose(95)], _touch())
    assert "introduce Agentic Labs plainly" not in seen[0]


@pytest.mark.unit
def test_non_cadence_drafts_do_not_get_cadence_gates(monkeypatch) -> None:
    """email1 keeps its own character bounds; a 60-word email1 is fine."""
    monkeypatch.setattr(d, "_invoke_claude",
                        lambda prompt, timeout=90: _prose(60))
    monkeypatch.setattr(d, "build_input",
                        lambda kind, pid, recent_posts=None, evidence=None:
                        d.DrafterInput(kind=kind, campaign={"brief": ""},
                                       prospect={}, evidence=evidence))
    tries: list = []
    d.draft("email1", 1, evidence=ev.build_evidence(COLIN, []).as_dict(),
            attempts_out=tries)
    assert all(t.category not in ("word_band", "subject_rule") for t in tries)


# ============================== claims about us ============================

GROUNDING = ev.CortivoGrounding(BRIEF)


@pytest.mark.unit
def test_what_the_sender_runs_into_is_not_a_claim_about_us() -> None:
    """The cadence asks for problems "you've come across often". "I run into"
    matched the "I run <company>" trigger and made "COOs" an invented name."""
    assert ev.ungrounded_cortivo_claim(
        "Something I run into a lot with COOs is reporting done by hand.",
        GROUNDING) is None


@pytest.mark.unit
def test_a_sentence_naming_agentic_labs_is_checked_like_one_saying_we() -> None:
    """The trigger listed Cortivo and never Agentic Labs, so a claim that
    named the company without "we" or "our" was never checked."""
    assert ev.ungrounded_cortivo_claim(
        "Agentic Labs has worked with Deloitte for years.", GROUNDING)
    assert ev.ungrounded_cortivo_claim(
        "I run Agentic Labs with Ritik.", GROUNDING) is None


# ============================== the harness ================================

@pytest.mark.unit
@pytest.mark.parametrize("extra, message", [
    ([], "add --real-drafter"),
    (["--real-drafter", "--skip-geo"], "genuinely qualified"),
])
def test_the_harness_refuses_a_cadence_it_should_not_draft(extra, message) -> None:
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [sys.executable, str(root / "scripts" / "smoke_e2e.py"), "--profile",
         "https://www.linkedin.com/in/nobody/", "--sequence", *extra],
        capture_output=True, text=True, cwd=root, timeout=60)
    assert out.returncode == 2
    assert message in out.stderr



# ============================== second cut: the review feedback ============
#
# The first cut, reviewed: two case studies cited for problems they do not
# document solving, the company named in seven of eight emails, three main
# emails announcing their own change of subject, and follow-ups that asked
# the previous question again.


@pytest.mark.unit
def test_a_case_study_is_cited_only_where_it_is_documented_to_help() -> None:
    """Experial is documented only as PILOTED, so it cannot prove "pilots that
    never go live". Bespoke is a wealth-advisor copilot with no reporting
    claim, so it cannot prove "manual reporting". Neither guess has a
    documented case study, and neither gets one."""
    assert ev.hypothesis_proof("ai_pilots_stall") == ()
    assert ev.hypothesis_proof("reporting_by_hand") == ()
    mapped = {name for h in ev._ROLE_HYPOTHESES for name in h.proof}
    assert not mapped & {"Experial", "Bespoke"}


@pytest.mark.unit
def test_every_mapped_case_study_exists_in_the_brief() -> None:
    mapped = {name for h in ev._ROLE_HYPOTHESES + (ev._WHERE_AI_FITS,)
              for name in h.proof} | set(ev.hypothesis_proof(ev.NEXT_LEVER))
    for name in mapped:
        assert ev.proof_point_brief(name, BRIEF), f"{name} is not in the brief"


@pytest.mark.unit
def test_without_a_supported_case_study_the_email_cites_none() -> None:
    """No fallback list. For Colin, emails 3 and 4 are built on guesses with
    no documented case study, so they cite nothing; only email 6 does."""
    state, stub = _run()
    by_number = {c["touch"]["number"]: c["touch"] for c in stub.calls}
    assert by_number[3]["proof_point"] is None
    assert by_number[4]["proof_point"] is None
    assert [t.proof_point for t in state.touches if t.proof_point] == ["OrionQ"]


@pytest.mark.unit
def test_the_opener_never_asks_the_drafter_to_pick_a_case_study() -> None:
    """The proof-point angle says "name the closest thing we have built" — a
    case study chosen by resemblance, which is what the fallback did."""
    fund = ProfileFacts(
        full_name="Ada Moss", headline="CEO at Birch Wealth Capital",
        positions=[_pos("Birch Wealth Capital", "CEO", "2024-01-01",
                        current=True)])
    assert ev._fits(next(a for a in ev._POSITIONING if a.name == "proof_point"),
                    fund, ev.build_evidence(fund, [])), "fixture must qualify"
    _, stub = _run(facts=fund)
    positioning = stub.calls[0]["evidence"].get("positioning") or {}
    assert positioning.get("name") != "proof_point"


@pytest.mark.unit
def test_positioning_can_exclude_an_angle() -> None:
    fund = ProfileFacts(full_name="Ada Moss", headline="CFO, wealth fund",
                        positions=[])
    bundle = ev.build_evidence(fund, [])
    for key in map(str, range(30)):
        assert ev.choose_positioning(fund, bundle, key,
                                     exclude=("proof_point",)).name != "proof_point"


# ---- company name ----------------------------------------------------------

@pytest.mark.unit
def test_the_company_budget_is_once_per_main_never_in_a_follow_up() -> None:
    _, stub = _run()
    for call in stub.calls:
        company = call["touch"]["company"]
        assert company["word"] == "amtrust"
        expected = 1 if call["kind"] == seq.MAIN else 0
        assert company["max_mentions"] <= expected


@pytest.mark.unit
def test_after_three_emails_name_the_company_no_more_may() -> None:
    class NamesIt(StubDrafter):
        def __call__(self, kind, prospect_id, **kw):
            body = super().__call__(kind, prospect_id, **kw)
            return body.replace("Hi Colin,", "Hi Colin, AmTrust", 1) \
                if kw["touch"]["company"]["max_mentions"] else body

    _, stub = _run(stub=NamesIt())
    budgets = [c["touch"]["company"]["max_mentions"] for c in stub.calls
               if c["kind"] == seq.MAIN]
    assert budgets == [1, 1, 1, 0]


@pytest.mark.unit
def test_a_company_named_by_an_ordinary_word_is_not_policed() -> None:
    """"Capital" or "Work" would match ordinary prose."""
    generic = ProfileFacts(full_name="Jo Park", headline="COO",
                           positions=[_pos("Capital Partners", "COO",
                                           "2024-01-01", current=True)])
    _, stub = _run(facts=generic)
    assert all(c["touch"]["company"] is None for c in stub.calls)


def _content_problem(body, touch_over=None, evidence=None):
    touch = {"number": 3, "kind": seq.MAIN, "previous_emails": [],
             "company": {"name": "AmTrust International", "word": "amtrust",
                         "max_mentions": 1}}
    touch.update(touch_over or {})
    inp = d.DrafterInput(kind=touch["kind"], campaign={}, prospect={},
                         sender={"sign_off": SIGN_OFF})
    return d._cadence_content_problem(body, touch, inp, evidence, BRIEF)


@pytest.mark.unit
def test_naming_the_company_twice_in_a_main_email_is_retried() -> None:
    twice = f"Hi Colin, at AmTrust the work at AmTrust is varied.\n\n{SIGN_OFF}"
    once = f"Hi Colin, at AmTrust the work is varied.\n\n{SIGN_OFF}"
    assert _content_problem(twice)[0] == "company_repetition"
    assert _content_problem(once) is None


@pytest.mark.unit
def test_naming_the_company_in_a_follow_up_is_retried() -> None:
    body = f"Hi Colin, is that true at AmTrust?\n\n{SIGN_OFF}"
    problem = _content_problem(body, {"kind": seq.FOLLOWUP, "company": {
        "name": "AmTrust International", "word": "amtrust",
        "max_mentions": 0}})
    assert problem[0] == "company_repetition"


# ---- transitions -----------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("opener", [
    "Different question this time.",
    "Leaving AI pilots aside, here is one I see often.",
    "I'll leave reporting there.",
    "Switching gears for a moment.",
])
def test_a_main_email_may_not_announce_a_change_of_subject(opener) -> None:
    body = f"Hi Colin,\n\n{opener} Pilots stall a lot.\n\n{SIGN_OFF}"
    assert _content_problem(body)[0] == "announced_transition"


@pytest.mark.unit
def test_the_last_note_may_leave_it_there() -> None:
    body = f"Hi Colin, I'll leave it there. Worth a chat?\n\n{SIGN_OFF}"
    assert _content_problem(body, {"kind": seq.FOLLOWUP, "number": 8}) is None


# ---- follow-ups ------------------------------------------------------------

@pytest.mark.unit
def test_each_follow_up_has_its_own_job() -> None:
    follow_ups = [p for p in seq.PLAN if p.kind == seq.FOLLOWUP]
    assert len({p.role for p in follow_ups}) == len(follow_ups)
    assert all("repeat" in p.brief or p.number == 8 for p in follow_ups)


@pytest.mark.unit
def test_no_brief_asks_for_a_case_study_unconditionally() -> None:
    """The first cut's briefs said "cite the case study" whether or not one
    had been assigned, inviting the drafter to supply its own."""
    for plan in seq.PLAN:
        if "case study" in plan.brief:
            assert "if none is, cite none" in plan.brief


# ---- activity we can and cannot see ----------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("body", [
    "Saw you liked a post on claims automation.",
    "Your reaction on that thread caught my eye.",
    "You commented on a piece about underwriting.",
])
def test_likes_reactions_and_comments_are_never_mentioned(body) -> None:
    """None of them is collected, so any mention describes something we
    never saw."""
    assert _content_problem(f"Hi Colin, {body}\n\n{SIGN_OFF}")[0] == \
        "unseen_activity"


@pytest.mark.unit
def test_the_prompt_says_a_repost_is_shared_and_only_suggests_an_interest() -> None:
    touch = {"number": 1, "of": 8, "kind": seq.MAIN, "role": "opener",
             "brief": "", "words": {"min": 80, "max": 119}, "subject": None,
             "content": {"id": "repost-1", "kind": "repost", "text": SHARED},
             "hypothesis": None, "proof_point": None, "previous_emails": [],
             "already_used": {}, "company": None}
    block = d._touch_block(touch)
    assert "REPOSTED" in block and "never that they wrote" in block
    assert "may indicate an interest" in block
    assert "never evidence of a problem" in block
    own = d._touch_block({**touch, "content": {"id": "post-1", "kind": "post",
                                               "text": OWN}})
    assert "WROTE" in own and "REPOSTED" not in own


@pytest.mark.unit
def test_a_repost_opener_is_offered_as_a_repost_not_a_post() -> None:
    _, stub = _run(posts=[{"text": SHARED, "is_repost": True}])
    first = stub.calls[0]
    assert first["touch"]["content"]["kind"] == "repost"
    assert first["evidence"]["observations"] == []
    assert [i["detail"] for i in first["evidence"]["interests"]] == [SHARED]
    assert first["evidence"]["pain_claim_licensed"] is False



@pytest.mark.unit
def test_the_last_note_does_not_re_ask_the_lever_question() -> None:
    """The second cut's email 8 asked email 6's question again in new words
    ("has anything cleared that bar"): the close needs a question of its own."""
    close = next(p for p in seq.PLAN if p.number == 8)
    assert "no earlier email has asked" in close.brief
    assert "not whether AI has helped" in close.brief



# ============================== follow-up jobs, checked ====================
#
# Two jobs have a deterministic signature; two do not, and are left to their
# instruction rather than to a similarity score.

SECOND_CUT_EMAIL_8 = (
    "Hi Colin,\n\nThis is my last note on this thread.\n\nTelling useful AI "
    "apart from the noise is taking most operations leaders a while. Has "
    "anything cleared that bar for you yet, even something small?\n\nIf now "
    "isn't the time, that's fine. Happy to pick this up later.\n\n" + SIGN_OFF)
THIRD_CUT_EMAIL_8 = (
    "Hi Colin,\n\nThis is my last note, so I'll keep it short. Is now the "
    "wrong time, or is this something someone else on your side looks after? "
    "Either answer helps, and if it comes up later, my inbox is open.\n\n"
    + SIGN_OFF)


@pytest.mark.unit
def test_each_follow_up_carries_its_job_and_mains_carry_none() -> None:
    _, stub = _run()
    jobs = {c["touch"]["number"]: c["touch"]["job"] for c in stub.calls}
    assert jobs == {1: None, 2: "yes_no", 3: None, 4: None, 5: "example",
                    6: None, 7: "narrower", 8: "close"}


@pytest.mark.unit
def test_the_close_that_re_asked_email_6_fails_its_job() -> None:
    """The second cut's email 8 asked email 6's question again in new words.
    Its only non-question mention of timing does not count."""
    assert d.followup_job_problem(SECOND_CUT_EMAIL_8, "close", SIGN_OFF) == \
        "its question is not about timing or who owns this"
    assert d.followup_job_problem(THIRD_CUT_EMAIL_8, "close", SIGN_OFF) is None


@pytest.mark.unit
def test_the_close_must_say_it_is_the_last_note() -> None:
    body = f"Hi Colin, is now the wrong time for this?\n\n{SIGN_OFF}"
    assert d.followup_job_problem(body, "close", SIGN_OFF) == \
        "does not say it is the last note"


@pytest.mark.unit
@pytest.mark.parametrize("question, ok", [
    ("Is there one handoff that still happens over email?", True),
    ("To make it easy: when work moves teams, does it go by email?", True),
    ("Email or a spreadsheet, mostly?", True),
    ("What does your handoff process look like?", False),
    ("How would you describe the way work moves between teams?", False),
])
def test_the_yes_or_no_follow_up_asks_a_closed_question(question, ok) -> None:
    body = f"Hi Colin,\n\n{question}\n\n{SIGN_OFF}"
    assert (d.followup_job_problem(body, "yes_no", SIGN_OFF) is None) is ok


@pytest.mark.unit
@pytest.mark.parametrize("job", ["example", "narrower", None])
def test_jobs_without_a_signature_are_not_checked(job) -> None:
    body = f"Hi Colin,\n\nWhat does your week look like?\n\n{SIGN_OFF}"
    assert d.followup_job_problem(body, job, SIGN_OFF) is None


@pytest.mark.unit
def test_a_follow_up_failing_its_job_is_retried(monkeypatch) -> None:
    open_q = f"Hi Colin, what does your handoff process look like?\n\n{SIGN_OFF}"
    closed = f"Hi Colin, does most work still move between teams by email?\n\n{SIGN_OFF}"
    touch = {**_touch(2, seq.FOLLOWUP), "job": "yes_no"}
    body, tries, _ = _drive(monkeypatch, [open_q, closed], touch,
                            kind=seq.FOLLOWUP)
    assert tries[0].category == "followup_job"
    assert body == closed


@pytest.mark.unit
def test_the_review_says_which_jobs_were_checked() -> None:
    state, _ = _run()
    review = seq.render_review(state, prospect_label="p", sender_label="s",
                               campaign_label="c")
    assert "Email 2 follow-up job `yes_no`: checked" in review
    assert "Email 8 follow-up job `close`: checked" in review
    assert "Email 5 follow-up job `example`: not machine-checkable" in review
    assert "Email 7 follow-up job `narrower`: not machine-checkable" in review
