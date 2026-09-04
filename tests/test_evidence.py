"""Tests for evidence typing and the claims it licenses.

These pin the behaviour that a real run got wrong on 2026-09-04: a verified
career move became "Most teams at that transition point end up rebuilding
internal tooling", a claim nothing supported, in an email addressed to a real
person at their real work address.

The property under test throughout is not "does it produce good prose". It is
**does a fact license only what it actually supports** — because every bad
email this system has produced came from answering that question wrongly.
"""

from __future__ import annotations

import pytest

from linkedin_agent import drafter as d
from linkedin_agent.evidence import (
    EvidenceKind,
    Tier,
    ask_for,
    build_evidence,
    detect_signals,
    subject_for,
)
from linkedin_agent.providers.capabilities import Position, ProfileFacts


# ------------------------------------------------------------- golden fixtures
# Five prospects covering the evidence states the pipeline actually encounters.
# Drawn from the real profiles this system has been run against, so the tests
# fail on the same shapes production sees rather than on invented ones.

def _pos(company, title, start, end=None, current=False, desc=None):
    return Position(company=company, title=title, start_date=start,
                    end_date=end, is_current=current, date_precision="month",
                    description=desc)


def _post(text):
    return {"text": text, "posted_at": "2026-08-01T00:00:00Z"}


GOLDEN = {
    # Strong: the prospect published something implying a real problem.
    "strong_hiring": ProfileFacts(
        full_name="Vincent Picot",
        headline="Group CFO at Tikehau Capital",
        positions=[_pos("Tikehau Capital", "Group CFO", "2023-10-01", current=True)],
    ),
    # Moderate: they publish, but nothing implying a problem.
    "moderate_posts": ProfileFacts(
        full_name="Ghazi Sultan",
        headline="Engineer",
        positions=[_pos("Pawp", "Senior Python Engineer", "2025-05-01", current=True)],
    ),
    # Weak: a verified consecutive move and nothing else.
    "weak_transition": ProfileFacts(
        full_name="Ahmed Ashfaq",
        headline="Director of Operations GCC & Iraq",
        positions=[
            _pos("Millennium Hotels", "Director of Operations", "2025-06-01",
                 current=True),
            _pos("Jawakara Islands Maldives", "Cluster Resort Manager",
                 "2023-01-01", "2025-06-01"),
        ],
    ),
    # Weak: founder title, no published anything.
    "weak_founder": ProfileFacts(
        full_name="Anjan B",
        headline="Software Engineer @ TalkingLands | AI Developer",
        positions=[
            _pos("TalkingLands", "Software Engineer", "2026-02-01", current=True),
            _pos("dan Lab", "Co-Founder", "2025-08-01", current=True),
        ],
    ),
    # None: nothing usable at all.
    "no_signal": ProfileFacts(full_name="Paula Camille N.", positions=[]),
}

HIRING_POST = _post(
    "We are looking for an exceptional candidate to join our Private Debt "
    "team in London as an Investment Analyst.")
NEUTRAL_POST = _post(
    "Parallel coding agents changed my workflow. Not in the way I expected.")


# ============================ 2, 3, 4: forbidden inferences ==================

@pytest.mark.unit
def test_a_career_transition_licenses_no_pain_claim() -> None:
    """Requirement 2. The exact failure: moved employer -> "rebuilding internal
    tooling". Changing jobs says where someone works, not what is broken."""
    bundle = build_evidence(GOLDEN["weak_transition"], [],
                            transition_is_direct=True)
    assert bundle.pain_claim_licensed is False
    assert bundle.tier is Tier.WEAK
    # The move itself is still referenceable — we are removing invention, not
    # the personalisation.
    assert any("recently moved from" in e.statement for e in bundle.facts)


@pytest.mark.unit
def test_a_founder_title_licenses_no_pain_claim() -> None:
    """Requirement 3. Being a founder is not evidence of a tooling problem."""
    bundle = build_evidence(GOLDEN["weak_founder"], [])
    assert bundle.pain_claim_licensed is False
    assert any("Co-Founder" in e.statement for e in bundle.facts)


@pytest.mark.unit
def test_an_engineering_title_licenses_no_bottleneck_claim() -> None:
    """Requirement 4. An engineer is not evidence of a technical bottleneck."""
    bundle = build_evidence(GOLDEN["moderate_posts"], [])
    assert bundle.pain_claim_licensed is False


@pytest.mark.unit
def test_company_size_licenses_no_pain_claim() -> None:
    """Headcount is a firmographic, not a diagnosis."""
    facts = ProfileFacts(full_name="X", company_name="BigCo",
                         company_employee_count=819)
    assert build_evidence(facts, []).pain_claim_licensed is False


@pytest.mark.unit
def test_publishing_alone_licenses_no_pain_claim() -> None:
    """A post about coding agents does not mean they have a bottleneck."""
    bundle = build_evidence(GOLDEN["moderate_posts"], [NEUTRAL_POST])
    assert bundle.tier is Tier.MODERATE
    assert bundle.pain_claim_licensed is False
    assert len(bundle.observations) == 1


# ============================== 5: real signals ==============================

@pytest.mark.unit
def test_a_hiring_post_is_a_signal_and_licenses_a_claim() -> None:
    """Requirement 5. Evidence the prospect published themselves is exactly
    what the system SHOULD use — the fix must not make it mute."""
    bundle = build_evidence(GOLDEN["strong_hiring"], [HIRING_POST])
    assert bundle.tier is Tier.STRONG
    assert bundle.pain_claim_licensed is True
    assert bundle.signals[0].claim


@pytest.mark.unit
def test_the_licensed_claim_stays_tied_to_the_signal() -> None:
    """A hiring post licenses a claim about build capacity, and nothing
    wider. Widening is how a real signal becomes an invented diagnosis."""
    bundle = build_evidence(GOLDEN["strong_hiring"], [HIRING_POST])
    assert "build capacity" in bundle.signals[0].claim


@pytest.mark.unit
@pytest.mark.parametrize("text,expected", [
    ("We just raised our seed round", True),
    ("We're hiring engineers", True),
    ("Still doing this by hand in spreadsheets instead of automating", True),
    ("We just launched the new app", True),
    ("Great conference in Lisbon last week", False),
    ("Congratulations to the team on the award", False),
    ("Thoughts on the future of AI in hospitality", False),
])
def test_signal_detection_is_specific(text, expected) -> None:
    assert bool(detect_signals([_post(text)])) is expected


@pytest.mark.unit
def test_signals_come_only_from_the_prospects_own_words() -> None:
    """A signal is a claim we will make about their business, so it has to
    come from them, not from our reading of their resume."""
    facts = ProfileFacts(
        full_name="X",
        headline="We are hiring engineers! Scaling fast, drowning in tech debt",
        positions=[_pos("Acme", "CEO", "2024-01-01", current=True)],
    )
    assert build_evidence(facts, []).pain_claim_licensed is False


# =========================== 7: restraint under weak evidence ================

@pytest.mark.unit
def test_thin_evidence_yields_the_weak_tier_not_a_manufactured_one() -> None:
    """Requirement 7. Nothing to say must stay a comfortable place to be."""
    bundle = build_evidence(GOLDEN["weak_founder"], [])
    assert bundle.tier is Tier.WEAK
    assert bundle.unknowns, "the gaps must be named, not left silent"
    assert any("no evidence either way" in u for u in bundle.unknowns)


@pytest.mark.unit
def test_no_usable_facts_yields_tier_none() -> None:
    bundle = build_evidence(GOLDEN["no_signal"], [])
    assert bundle.tier is Tier.NONE


@pytest.mark.unit
def test_unknowns_name_what_we_did_not_retrieve() -> None:
    bundle = build_evidence(GOLDEN["weak_founder"], [])
    assert any("no posts retrieved" in u for u in bundle.unknowns)


# ================================ 8: subjects ================================

@pytest.mark.unit
def test_subjects_are_not_one_template() -> None:
    """Requirement 8. Every prospect used to get "<Company> - shipping without
    hiring a team": one structure, and a claim we had not verified."""
    subjects = {
        subject_for(GOLDEN["strong_hiring"],
                    build_evidence(GOLDEN["strong_hiring"], [HIRING_POST])),
        subject_for(GOLDEN["weak_transition"],
                    build_evidence(GOLDEN["weak_transition"], [],
                                   transition_is_direct=True)),
        subject_for(GOLDEN["weak_founder"],
                    build_evidence(GOLDEN["weak_founder"], [])),
    }
    assert len(subjects) == 3, f"subjects collapsed to a template: {subjects}"


@pytest.mark.unit
def test_a_subject_never_asserts_an_unverified_pain() -> None:
    for key in ("weak_transition", "weak_founder", "moderate_posts"):
        facts = GOLDEN[key]
        subject = subject_for(facts, build_evidence(facts, []))
        assert d._contains_unsupported_pain_claim(subject) is None, subject
        assert d._contains_filler(subject) is None, subject


@pytest.mark.unit
def test_a_verified_move_can_appear_in_the_subject() -> None:
    facts = GOLDEN["weak_transition"]
    bundle = build_evidence(facts, [], transition_is_direct=True)
    assert subject_for(facts, bundle) == "Your move to Millennium Hotels"


# ================================== 9: CTAs ==================================

@pytest.mark.unit
def test_the_ask_scales_with_the_evidence() -> None:
    """Requirement 9. "walk through what we'd build" presumes a problem worth
    building for, which at the weak tier we have not established."""
    strong = ask_for(build_evidence(GOLDEN["strong_hiring"], [HIRING_POST]))
    weak = ask_for(build_evidence(GOLDEN["weak_founder"], []))
    assert strong != weak
    for ask in (strong, weak):
        assert "walk through what" not in ask.lower()
        assert "meeting" not in ask.lower()


# ========================= 6: unsupported claims rejected ====================

@pytest.mark.unit
@pytest.mark.parametrize("body", [
    "Most teams at that transition point end up rebuilding internal tooling.",
    "You're probably wrestling with a lot of manual process right now.",
    "The real squeeze is usually go-to-market ops.",
    "Teams building at that stage end up doing this by hand.",
])
def test_unsupported_claims_are_detected(body) -> None:
    """Requirement 6."""
    assert d._contains_unsupported_pain_claim(body) is not None


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    "You mentioned you're hiring an investment analyst in London.",
    "Saw you moved over to Millennium.",
    "I'm Haque, co-founder of Cortivo. Would this be relevant on your side?",
])
def test_honest_restrained_copy_passes(body) -> None:
    """The gate must not make it impossible to write a normal sentence."""
    assert d._contains_unsupported_pain_claim(body) is None
    assert d._contains_filler(body) is None


@pytest.mark.unit
@pytest.mark.parametrize("phrase", [
    "what caught my eye is the work you are doing",
    "Teams building at that stage",
    "internal tooling and data pipelines",
    "That's our outside read",
    "go-to-market ops or product velocity",
    "or somewhere we haven't surfaced",
    "shipping without hiring a team",
])
def test_every_phrase_from_the_old_template_is_rejected(phrase) -> None:
    """Requirement 4 of the brief: these must not survive as components."""
    body = f"Hi there,\n\n{phrase} and so on.\n\nBest,\nHaque"
    assert (d._contains_filler(body) or
            d._contains_unsupported_pain_claim(body)) is not None, phrase


# ===================== the seam into the actual drafter ======================
# `claude` is not installed on every machine this runs on, so these exercise
# everything up to and including the gates with the model faked out. What they
# cannot prove is the model's judgment; what they do prove is that the evidence
# reaches it and that a bad answer is rejected.

@pytest.mark.unit
def test_the_evidence_reaches_the_prompt() -> None:
    bundle = build_evidence(GOLDEN["strong_hiring"], [HIRING_POST])
    inp = d.DrafterInput(kind="email1", campaign={}, prospect={},
                         evidence=bundle.as_dict())
    prompt = d.render_prompt(inp)
    assert "pain_claim_licensed" in prompt
    assert "Investment Analyst" in prompt, "their own words must be in there"
    assert "unknowns" in prompt


@pytest.mark.unit
def test_an_unlicensed_prospect_is_told_so_outside_the_json() -> None:
    """Buried in a payload field, the constraint competes with everything else
    in the payload. It is the binding rule, so it is restated as one."""
    bundle = build_evidence(GOLDEN["weak_founder"], [])
    inp = d.DrafterInput(kind="email1", campaign={}, prospect={},
                         evidence=bundle.as_dict())
    prompt = d.render_prompt(inp)
    assert "NOTHING licenses a claim" in prompt


@pytest.mark.unit
def test_a_licensed_prospect_is_told_to_stay_tied_to_the_signal() -> None:
    bundle = build_evidence(GOLDEN["strong_hiring"], [HIRING_POST])
    inp = d.DrafterInput(kind="email1", campaign={}, prospect={},
                         evidence=bundle.as_dict())
    assert "tied to that signal" in d.render_prompt(inp)


@pytest.mark.unit
def test_the_drafter_retries_when_the_model_invents_a_pain(monkeypatch) -> None:
    """End to end through draft(), with the model faked: a first answer that
    diagnoses an unlicensed prospect must be rejected and re-prompted."""
    attempts = []
    # Must clear the 300-char minimum, or the LENGTH gate rejects it first and
    # this test silently stops exercising the pain gate at all.
    bad = ("Hi Vincent,\n\nMost teams at that transition point end up "
           "rebuilding internal tooling by hand while the product gets all "
           "the attention, and it quietly eats a quarter before anyone "
           "notices it happening.\n\nI'm Haque, co-founder of Cortivo. We "
           "build the systems that take that off your plate, shaped around "
           "how your team actually works day to day.\n\nDo you have time "
           "this week to get into it?\n\nBest,\nHaque")
    good = ("Hi Vincent,\n\nSaw you moved over to Millennium, which is the "
            "only reason I'm writing — no list involved, and no pretence "
            "that I know the place.\n\nI'm Haque, co-founder of Cortivo. "
            "We're a small engineering studio, and we build custom software "
            "for teams who would rather not hire and manage a whole in-house "
            "team just to get something shipped.\n\nI've no idea whether "
            "that's useful to you right now, and I'm not going to guess at "
            "what's on your plate.\n\nWould this be relevant on your side?"
            "\n\nBest,\nHaque")

    def fake(prompt, timeout=90):
        attempts.append(prompt)
        return bad if len(attempts) == 1 else good

    monkeypatch.setattr(d, "_invoke_claude", fake)
    monkeypatch.setattr(d, "build_input",
                        lambda kind, pid, recent_posts=None, evidence=None:
                        d.DrafterInput(kind=kind, campaign={}, prospect={},
                                       evidence=evidence))
    bundle = build_evidence(GOLDEN["weak_transition"], [],
                            transition_is_direct=True)
    out = d.draft("email1", 1, evidence=bundle.as_dict())
    assert out == good
    assert len(attempts) == 2, "the invented diagnosis should have been rejected"
    assert "nothing in the evidence supports" in attempts[1]


@pytest.mark.unit
def test_a_licensed_prospect_may_keep_the_same_sentence(monkeypatch) -> None:
    """The gate must key on the evidence, not on the words. The same body is
    acceptable when a signal supports it — otherwise we have banned a
    vocabulary rather than fixed the reasoning."""
    body = ("Hi Vincent,\n\nYou're probably adding build capacity faster than "
            "you would like right now, going by the roles you have open at "
            "the moment.\n\nI'm Haque, co-founder of Cortivo. We build custom "
            "software for teams who would rather not hire a whole in-house "
            "team to do it.\n\nWorth comparing notes?\n\nBest,\nHaque")
    monkeypatch.setattr(d, "_invoke_claude", lambda p, timeout=90: body)
    monkeypatch.setattr(d, "build_input",
                        lambda kind, pid, recent_posts=None, evidence=None:
                        d.DrafterInput(kind=kind, campaign={}, prospect={},
                                       evidence=evidence))
    licensed = build_evidence(GOLDEN["strong_hiring"], [HIRING_POST])
    assert d.draft("email1", 1, evidence=licensed.as_dict()) == body

    unlicensed = build_evidence(GOLDEN["weak_founder"], [])
    with pytest.raises(d.DrafterError):
        d.draft("email1", 1, evidence=unlicensed.as_dict(), max_attempts=1)


@pytest.mark.unit
def test_the_subject_line_is_parsed_off_the_body() -> None:
    subject, body = d.parse_email("Subject: Your move to Millennium\n\nHi "
                                  "Ahmed,\n\nSaw you moved.")
    assert subject == "Your move to Millennium"
    assert body.startswith("Hi Ahmed,")


@pytest.mark.unit
def test_a_missing_subject_is_none_not_invented() -> None:
    subject, body = d.parse_email("Hi Ahmed,\n\nSaw you moved.")
    assert subject is None
    assert body.startswith("Hi Ahmed,")


@pytest.mark.unit
def test_a_pain_claim_is_caught_above_the_length_floor() -> None:
    """Guards the test above, whose first fixture was 202 chars: the length
    gate rejected it first, so the test passed while exercising nothing."""
    body = ("Hi Vincent,\n\nMost teams at that transition point end up "
            "rebuilding internal tooling by hand while the product gets all "
            "the attention, and it quietly eats a quarter before anyone "
            "notices it happening.\n\nI am Haque, co-founder of Cortivo. We "
            "build the systems that take that off your plate, shaped around "
            "how your team actually works day to day.\n\nDo you have time "
            "this week to get into it?\n\nBest,\nHaque")
    assert len(body) >= d.KIND_MIN_CHARS["email1"], len(body)
    assert d._contains_unsupported_pain_claim(body) is not None


# ------------------------- typographic apostrophes ---------------------------
# LinkedIn's composer and every phone keyboard emit U+2019, not an ASCII
# apostrophe, so "We’re Hiring" is what actually arrives from a scrape. Every
# pattern written with a straight quote failed against it silently.
#
# Observed 2026-09-04: a prospect who had posted three hiring ads in two days
# scored signals=none, tier=moderate, and got the cautious email written for
# someone we know nothing about. In the spam gate the same gap let "I'd love to
# connect" through to a real person.

CURLY = "\u2019"


@pytest.mark.unit
@pytest.mark.parametrize("text", [
    f"We{CURLY}re Hiring | AI-Driven QA Automation Engineer (SDET)",
    f"We{CURLY}re hiring engineers this quarter",
])
def test_a_hiring_post_with_a_curly_apostrophe_is_still_a_signal(text) -> None:
    assert [s.name for s in detect_signals([_post(text)])] == ["hiring_engineers"]


@pytest.mark.unit
def test_both_apostrophe_forms_score_the_same_tier() -> None:
    """The scrape must not decide the email. Two renderings of one sentence
    cannot produce two different evidence tiers."""
    straight = build_evidence(GOLDEN["strong_hiring"],
                              [_post("We're hiring engineers")])
    curly = build_evidence(GOLDEN["strong_hiring"],
                           [_post(f"We{CURLY}re hiring engineers")])
    assert straight.tier is curly.tier is Tier.STRONG
    assert straight.pain_claim_licensed == curly.pain_claim_licensed is True


@pytest.mark.unit
def test_a_curly_spam_tell_does_not_reach_a_real_person() -> None:
    """The costliest instance of the same bug: a missed signal loses a good
    email, but a missed spam tell sends a bad one."""
    assert d._contains_spam_tell(f"Hi there, I{CURLY}d love to connect.")


@pytest.mark.unit
@pytest.mark.parametrize("check,text", [
    ("_contains_unsupported_pain_claim",
     f"You{CURLY}re probably rebuilding all of that by hand."),
    ("_contains_inferred_relevance",
     f"That{CURLY}s usually where we come in."),
])
def test_the_other_gates_fold_quotes_too(check, text) -> None:
    assert getattr(d, check)(text) is not None
