"""Claims about US, and relevance arguments made from nothing.

The prospect gate stopped the system inventing THEIR problems. It said nothing
about inventing OUR credentials, and the first live drafter run did exactly
that, to a prospect who had just posted about running parallel coding agents:

    "so a lot of our week is spent in exactly that parallel-agent workflow,
     on client code rather than side projects"

Nothing in the brief says that. It was assembled by mirroring the prospect's
own vocabulary back at them as a description of us — flattering, plausible,
untrue. Someone who replies to it is replying to something we made up, and the
first call has to walk it back.

The second failure is quieter. Given one fact about someone, the drafter argues
that their *category* is a place our work matters:

    "Multi-property, multi-country operations is a setting where that work
     tends to matter"

That is a pain claim reasoned from a job title with a hedge bolted on. Both are
now rejected rather than reported.
"""

from __future__ import annotations

import pytest

from linkedin_agent import campaigns as campaigns_mod
from linkedin_agent import drafter as d
from linkedin_agent.evidence import (
    CortivoGrounding,
    Tier,
    build_evidence,
    ungrounded_cortivo_claim,
)
from linkedin_agent.providers.capabilities import Position, ProfileFacts


@pytest.fixture(scope="module")
def grounding() -> CortivoGrounding:
    """The real brief. Fabricating one here would test the fixture, not the
    thing the drafter is actually held to."""
    return CortivoGrounding(
        campaigns_mod.brief_path_for("_cortivo").read_text(encoding="utf-8"))


# =========================== ungrounded Cortivo claims =======================

@pytest.mark.unit
def test_the_ghazi_sentence_is_rejected(grounding) -> None:
    """The exact sentence from the live run that prompted this."""
    body = ("I'm at Cortivo, a small AI-engineering studio. We pair a senior "
            "engineer with AI tooling to build v1 products for founders who "
            "don't have an engineering team yet — so a lot of our week is "
            "spent in exactly that parallel-agent workflow, on client code "
            "rather than side projects.")
    found = ungrounded_cortivo_claim(body, grounding)
    assert found is not None
    assert "parallel-agent" in found, \
        f"must name the invented term, not an incidental word: {found}"


@pytest.mark.unit
def test_brief_supported_positioning_passes(grounding) -> None:
    """The gate must not make it impossible to describe the business. This is
    the brief's own positioning, near-verbatim."""
    body = ("I'm Haque, co-founder of Cortivo. We pair a senior engineer with "
            "AI tooling so a non-technical founder gets the equivalent of a "
            "3-4 person eng team for one engineer's cost, typically 6-10 "
            "weeks from kickoff to live users.")
    assert ungrounded_cortivo_claim(body, grounding) is None


@pytest.mark.unit
@pytest.mark.parametrize("body,why", [
    ("We work with Marriott on exactly this kind of thing.",
     "invented client"),
    ("We've shipped 47 products for founders in your position.",
     "invented figure"),
    ("Our clients in hospitality see this constantly.",
     "invented client segment"),
    ("We typically run discovery sprints before any build.",
     "invented process"),
    ("A lot of our week goes into hospitality integrations.",
     "invented practice"),
])
def test_invented_specifics_are_rejected(grounding, body, why) -> None:
    assert ungrounded_cortivo_claim(body, grounding) is not None, why


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    "Experial is being piloted by Coca-Cola and Bosch.",
    "Microforge is used by a16z, Sequoia, Elevation Capital, and Accel.",
    "Ritik was an SDE at Amazon; I was a Product Manager at Mastercard.",
    "Team is engineers from the IITs.",
])
def test_real_proof_points_pass(grounding, body) -> None:
    """Every claim here is in the brief. Blocking these would be worse than
    the bug — it would make the approved facts unusable."""
    assert ungrounded_cortivo_claim(body, grounding) is None, body


@pytest.mark.unit
def test_sentences_that_are_not_about_us_are_left_alone(grounding) -> None:
    """The prospect's own company and posts are governed by the evidence
    rules, not by our brief."""
    body = ("You wrote about running three agents on git worktrees at "
            "TalkingLands, and Docker fighting back.")
    assert ungrounded_cortivo_claim(body, grounding) is None


@pytest.mark.unit
def test_a_missing_brief_grounds_nothing(monkeypatch) -> None:
    """Fail closed. With no authority, no specific claim about us is
    permitted — silence beats invention."""
    empty = CortivoGrounding("")
    assert ungrounded_cortivo_claim(
        "We work with Marriott on this.", empty) is not None


# ======================= relevance reasoned from nothing =====================

@pytest.mark.unit
def test_the_ahmed_sentence_is_rejected() -> None:
    """The exact sentence from the live run, for a prospect we knew one fact
    about."""
    body = ("Multi-property, multi-country operations is a setting where that "
            "work tends to matter, though I have no idea what your stack "
            "looks like today.")
    assert d._contains_inferred_relevance(body) is not None


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    "Hospitality is an environment where this usually comes up.",
    "That kind of thing tends to matter at your scale.",
    "In my experience, teams like yours hit this early.",
    "That's usually where we come in.",
])
def test_reasoned_relevance_shapes_are_caught(body) -> None:
    assert d._contains_inferred_relevance(body) is not None


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    "I've no idea whether that's relevant to you — would it be?",
    "Would this be relevant on your side?",
    "I'm not assuming anything is broken on your side.",
    "Saw you moved to Millennium, where you head up operations in the GCC.",
])
def test_asking_about_relevance_is_not_asserting_it(body) -> None:
    """The fix is to ask instead of argue. Blocking the question too would
    leave the email with nothing to say."""
    assert d._contains_inferred_relevance(body) is None, body


# ========================= wired into the retry loop =========================

def _pos(company, title, start, end=None, current=False):
    return Position(company=company, title=title, start_date=start,
                    end_date=end, is_current=current, date_precision="month")


WEAK_FACTS = ProfileFacts(
    full_name="Ahmed Ashfaq",
    headline="Director of Operations GCC & Iraq",
    positions=[
        _pos("Millennium Hotels", "Director of Operations", "2025-06-01",
             current=True),
        _pos("Jawakara Islands Maldives", "Cluster Resort Manager",
             "2023-01-01", "2025-06-01"),
    ])

CLEAN = ("Hi Ahmed,\n\nWriting off the back of your move to Millennium "
         "Hotels, where you head up operations across the GCC and Iraq.\n\n"
         "I'm Haque, co-founder of Cortivo. We pair a senior engineer with AI "
         "tooling so a non-technical founder gets the equivalent of a 3-4 "
         "person eng team for one engineer's cost.\n\nI've no idea whether "
         "that's relevant to what you're doing — would it be?\n\nBest,\n"
         "Haque\nCortivo")


def _stub_drafter(monkeypatch, responses):
    seen = []

    def fake(prompt, timeout=90):
        seen.append(prompt)
        return responses[min(len(seen) - 1, len(responses) - 1)]

    monkeypatch.setattr(d, "_invoke_claude", fake)
    monkeypatch.setattr(d, "build_input",
                        lambda kind, pid, recent_posts=None, evidence=None:
                        d.DrafterInput(kind=kind,
                                       campaign={"brief": ""},
                                       prospect={}, evidence=evidence))
    return seen


@pytest.mark.unit
def test_draft_retries_on_an_ungrounded_cortivo_claim(monkeypatch) -> None:
    bad = ("Hi Ahmed,\n\nWriting off the back of your move to Millennium "
           "Hotels.\n\nI'm Haque, co-founder of Cortivo. We work with several "
           "hospitality groups already, and a lot of our week goes into "
           "exactly this kind of multi-property integration work for "
           "operators in your position.\n\nWould this be relevant on your "
           "side?\n\nBest,\nHaque\nCortivo")
    seen = _stub_drafter(monkeypatch, [bad, CLEAN])
    bundle = build_evidence(WEAK_FACTS, [], transition_is_direct=True)
    out = d.draft("email1", 1, evidence=bundle.as_dict())
    assert out == CLEAN
    assert len(seen) == 2, "the invented claim about us should have been rejected"
    assert "brief does not support" in seen[1]


@pytest.mark.unit
def test_draft_retries_on_relevance_reasoned_from_a_job_title(monkeypatch) -> None:
    bad = ("Hi Ahmed,\n\nWriting off the back of your move to Millennium "
           "Hotels, where you head up operations across the GCC and Iraq.\n\n"
           "I'm Haque, co-founder of Cortivo. Multi-property, multi-country "
           "operations is a setting where that work tends to matter, so this "
           "may well land at the right time for you.\n\nWould this be "
           "relevant on your side?\n\nBest,\nHaque\nCortivo")
    seen = _stub_drafter(monkeypatch, [bad, CLEAN])
    bundle = build_evidence(WEAK_FACTS, [], transition_is_direct=True)
    assert bundle.tier is Tier.WEAK
    out = d.draft("email1", 1, evidence=bundle.as_dict())
    assert out == CLEAN
    assert len(seen) == 2
    assert "reasoned that from their job title" in seen[1]


@pytest.mark.unit
def test_relevance_reasoning_is_allowed_once_a_signal_licenses_it(monkeypatch) -> None:
    """The gate keys on the evidence, not the words. With a signal the same
    move is legitimate — otherwise we have banned a sentence shape rather than
    fixed the reasoning."""
    body = ("Hi Vincent,\n\nYou posted looking for an Investment Specialist "
            "to work across the investment and sales teams on new fund "
            "launches.\n\nI'm Haque, co-founder of Cortivo. We pair a senior "
            "engineer with AI tooling so a founder gets the equivalent of a "
            "3-4 person eng team for one engineer's cost. That's usually "
            "where we're useful.\n\nWorth comparing notes?\n\nBest,\nHaque\n"
            "Cortivo")
    _stub_drafter(monkeypatch, [body])
    strong = build_evidence(
        ProfileFacts(full_name="Vincent Picot",
                     positions=[_pos("Tikehau Capital", "Group CFO",
                                     "2023-10-01", current=True)]),
        [{"text": "We are looking for an exceptional candidate to join our "
                  "Private Debt team in London as an Investment Analyst."}])
    assert strong.tier is Tier.STRONG
    assert d.draft("email1", 1, evidence=strong.as_dict()) == body


@pytest.mark.unit
def test_a_clean_weak_email_passes_every_gate(monkeypatch) -> None:
    """Requirement: the tightened rules must still leave a writable email."""
    _stub_drafter(monkeypatch, [CLEAN])
    bundle = build_evidence(WEAK_FACTS, [], transition_is_direct=True)
    assert d.draft("email1", 1, evidence=bundle.as_dict()) == CLEAN
