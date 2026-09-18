"""Attempt history: how many passes the drafter took, and why each failed.

A green MESSAGE_DRAFTING stage used to say nothing about whether the quality
gates had done anything. A draft accepted on the first pass and one rejected
twice for inventing a claim about Cortivo produced identical traces, so after a
live run there was no way to answer "did the gates fire, or did the prompt
alone get it right?" — which is the question you ask when deciding whether the
gates are earning their place.

The constraint that shapes the design: the history records verdicts, never
drafts. A rejected body is assembled from a real person's scraped profile, and
a trace is diagnostic output that gets pasted into tickets and chat.
"""

from __future__ import annotations

import pytest

from linkedin_agent import drafter as d
from linkedin_agent.evidence import build_evidence
from linkedin_agent.providers.capabilities import Position, ProfileFacts


def _pos(company, title, start, end=None, current=False):
    return Position(company=company, title=title, start_date=start,
                    end_date=end, is_current=current, date_precision="month")


FACTS = ProfileFacts(
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
         "tooling so a non-technical founder can get AI agents built without "
         "standing up a team in-house.\n\nI've no idea whether "
         "that's relevant to what you're doing — would it be?\n\nBest,\n"
         "Haque\nCortivo")

# Rejected bodies, each tripping exactly one gate. Every one carries the
# prospect's name and employer, which is the point of the no-leak test below.
INVENTS_PAIN = ("Hi Ahmed,\n\nMost teams at that transition point end up "
                "rebuilding the same reporting by hand, and it quietly eats "
                "a quarter at Millennium Hotels before anyone notices it is "
                "happening at all.\n\nI'm Haque, co-founder of Cortivo. We "
                "build custom software for teams who would rather not hire "
                "in-house to get it done.\n\nWould this be relevant?\n\n"
                "Best,\nHaque")
INVENTS_CORTIVO = ("Hi Ahmed,\n\nWriting off the back of your move to "
                   "Millennium Hotels, where you head up operations across "
                   "the GCC and Iraq.\n\nI'm Haque, co-founder of Cortivo. "
                   "A lot of our week goes into exactly this kind of "
                   "multi-property integration work for hospitality "
                   "operators in your position, and we have done it for "
                   "several groups already.\n\nWould this be relevant on "
                   "your side, or have I read it wrong?\n\nBest,\nHaque\n"
                   "Cortivo")


def _drive(monkeypatch, responses):
    """Run draft() against a scripted sequence of model outputs."""
    seen = []

    def fake(prompt, timeout=90):
        seen.append(prompt)
        return responses[min(len(seen) - 1, len(responses) - 1)]

    monkeypatch.setattr(d, "_invoke_claude", fake)
    monkeypatch.setattr(d, "build_input",
                        lambda kind, pid, recent_posts=None, evidence=None:
                        d.DrafterInput(kind=kind, campaign={"brief": ""},
                                       prospect={}, evidence=evidence))
    tries: list = []
    bundle = build_evidence(FACTS, [], transition_is_direct=True)
    body = d.draft("email1", 1, evidence=bundle.as_dict(), attempts_out=tries)
    return body, tries


# ============================== zero retries =================================

@pytest.mark.unit
def test_a_clean_first_draft_records_one_accepted_attempt(monkeypatch) -> None:
    body, tries = _drive(monkeypatch, [CLEAN])
    assert body == CLEAN
    assert len(tries) == 1
    assert tries[0].number == 1
    assert tries[0].outcome == "accepted"
    assert tries[0].category is None
    assert tries[0].reason is None


@pytest.mark.unit
def test_zero_retries_is_distinguishable_from_unknown(monkeypatch) -> None:
    """The whole point: a first-pass accept must be visibly different from a
    draft that needed the gates, not merely both "ok"."""
    _, clean = _drive(monkeypatch, [CLEAN])
    _, retried = _drive(monkeypatch, [INVENTS_PAIN, CLEAN])
    assert len(clean) == 1 and len(retried) == 2
    assert [t.outcome for t in clean] == ["accepted"]
    assert [t.outcome for t in retried] == ["rejected", "accepted"]


# ============================ one or more retries ============================

@pytest.mark.unit
def test_one_rejection_then_success_is_recorded_in_order(monkeypatch) -> None:
    body, tries = _drive(monkeypatch, [INVENTS_PAIN, CLEAN])
    assert body == CLEAN
    assert [(t.number, t.outcome) for t in tries] == [(1, "rejected"),
                                                      (2, "accepted")]
    assert tries[0].category == "unsupported_pain_claim"
    assert tries[0].reason


@pytest.mark.unit
def test_two_rejections_record_their_own_reasons(monkeypatch) -> None:
    """Different gates on different attempts must not be collapsed into one
    generic 'rejected' — which gate fired is the useful part."""
    body, tries = _drive(monkeypatch, [INVENTS_PAIN, INVENTS_CORTIVO, CLEAN])
    assert body == CLEAN
    assert len(tries) == 3
    assert tries[0].category == "unsupported_pain_claim"
    assert tries[1].category == "ungrounded_cortivo_claim"
    assert tries[2].outcome == "accepted"


@pytest.mark.unit
def test_exhausting_the_budget_still_leaves_a_history(monkeypatch) -> None:
    """The run that most needs explaining is the one that produced nothing."""
    tries: list = []
    monkeypatch.setattr(d, "_invoke_claude",
                        lambda p, timeout=90: INVENTS_PAIN)
    monkeypatch.setattr(d, "build_input",
                        lambda kind, pid, recent_posts=None, evidence=None:
                        d.DrafterInput(kind=kind, campaign={"brief": ""},
                                       prospect={}, evidence=evidence))
    bundle = build_evidence(FACTS, [], transition_is_direct=True)
    with pytest.raises(d.DrafterError):
        d.draft("email1", 1, evidence=bundle.as_dict(), attempts_out=tries)
    assert len(tries) == 3
    assert all(t.outcome == "rejected" for t in tries)


@pytest.mark.unit
def test_insufficient_context_is_recorded_before_it_raises(monkeypatch) -> None:
    tries: list = []
    monkeypatch.setattr(d, "_invoke_claude",
                        lambda p, timeout=90: d.INSUFFICIENT)
    monkeypatch.setattr(d, "build_input",
                        lambda kind, pid, recent_posts=None, evidence=None:
                        d.DrafterInput(kind=kind, campaign={"brief": ""},
                                       prospect={}, evidence=evidence))
    with pytest.raises(d.DrafterError, match="INSUFFICIENT_CONTEXT"):
        d.draft("email1", 1, attempts_out=tries)
    assert [t.category for t in tries] == ["insufficient_context"]


# ============================== no draft leaks ===============================

@pytest.mark.unit
@pytest.mark.parametrize("rejected", [INVENTS_PAIN, INVENTS_CORTIVO])
def test_the_history_never_carries_the_rejected_draft(monkeypatch, rejected) -> None:
    """The load-bearing privacy property. A rejected body is built from a real
    person's scraped profile, and this history is written into a trace that
    gets pasted into tickets and chat."""
    _, tries = _drive(monkeypatch, [rejected, CLEAN])
    recorded = " ".join(f"{t.category} {t.reason} {t}" for t in tries)
    assert "Ahmed" not in recorded
    assert "Millennium" not in recorded
    assert "Jawakara" not in recorded
    # And no long fragment of the draft survived either.
    for chunk in rejected.split(". "):
        stripped = chunk.strip()
        if len(stripped) > 30:
            assert stripped not in recorded


@pytest.mark.unit
def test_the_cortivo_reason_names_terms_not_the_sentence(monkeypatch) -> None:
    """The grounding check used to quote 70 characters of the draft."""
    _, tries = _drive(monkeypatch, [INVENTS_CORTIVO, CLEAN])
    reason = tries[0].reason
    assert "multi-property" in reason or "hospitality" in reason, reason
    assert "Ahmed" not in reason and "Millennium" not in reason, reason


# ============================ the optional plumbing ==========================

@pytest.mark.unit
def test_callers_that_do_not_ask_for_history_are_unaffected(monkeypatch) -> None:
    """poll.py and daily.py call draft() without the list; recording must be
    entirely opt-in."""
    monkeypatch.setattr(d, "_invoke_claude", lambda p, timeout=90: CLEAN)
    monkeypatch.setattr(d, "build_input",
                        lambda kind, pid, recent_posts=None, evidence=None:
                        d.DrafterInput(kind=kind, campaign={"brief": ""},
                                       prospect={}))
    assert d.draft("email1", 1) == CLEAN


@pytest.mark.unit
def test_an_attempt_renders_readably() -> None:
    assert str(d.DraftAttempt(1, "accepted")) == "attempt 1: accepted"
    rendered = str(d.DraftAttempt(2, "rejected", "spam_tell", "'i noticed you'"))
    assert rendered == "attempt 2: rejected — spam_tell: 'i noticed you'"


@pytest.mark.unit
@pytest.mark.parametrize("body,gate", [
    (INVENTS_PAIN, "unsupported_pain_claim"),
    (INVENTS_CORTIVO, "ungrounded_cortivo_claim"),
])
def test_rejection_fixtures_clear_the_length_floor(body, gate) -> None:
    """Guards the tests above. INVENTS_CORTIVO was 285 chars, so the LENGTH
    gate rejected it and the test recorded length_under while claiming to
    exercise the Cortivo gate."""
    assert len(body) >= d.KIND_MIN_CHARS["email1"], f"{gate}: {len(body)} chars"


# ============================ soft gates =====================================

# Clean on every hard gate, but delivers no question — so the
# observation_question shape it was assigned is unfulfilled.
NO_QUESTION = ("Hi Ahmed,\n\nWriting off the back of your move to Millennium "
               "Hotels, where you head up operations across the GCC and "
               "Iraq.\n\nI'm Haque, co-founder of Cortivo. We pair a senior "
               "engineer with AI tooling so a non-technical founder can get "
               "AI agents built without standing up a team in-house.\n\n"
               "Happy to be told this is not relevant.\n\nBest,\n"
               "Haque\nCortivo")


def _drive_with_shape(monkeypatch, responses, shape_name="observation_question"):
    """As _drive, but assigns a shape so the shape_unfulfilled gate can fire."""
    from linkedin_agent import evidence as ev

    seen = []

    def fake(prompt, timeout=90):
        seen.append(prompt)
        return responses[min(len(seen) - 1, len(responses) - 1)]

    monkeypatch.setattr(d, "_invoke_claude", fake)
    monkeypatch.setattr(d, "build_input",
                        lambda kind, pid, recent_posts=None, evidence=None:
                        d.DrafterInput(kind=kind, campaign={"brief": ""},
                                       prospect={}, evidence=evidence))
    shape = next(s for s in ev._SHAPES if s.name == shape_name)
    tries: list = []
    bundle = build_evidence(FACTS, [], transition_is_direct=True)
    body = d.draft("email1", 1, evidence=bundle.as_dict(shape=shape),
                   attempts_out=tries)
    return body, tries


@pytest.mark.unit
def test_an_unfulfilled_shape_is_recorded_as_such_not_as_dash_overuse(monkeypatch) -> None:
    """Which gate fired is the useful part of a rejection.

    The two soft gates share a branch, and the failure description was built
    before the branch that distinguishes them — so a shape rejection was
    reported as a punctuation problem. The attempt history has to name the gate
    that actually fired or it misdirects the next thing anyone tries.
    """
    body, tries = _drive_with_shape(monkeypatch, [NO_QUESTION, CLEAN])

    assert body == CLEAN
    assert [t.outcome for t in tries] == ["rejected", "accepted"]
    assert tries[0].category == "shape_unfulfilled"
    assert "question" in (tries[0].reason or "")
    assert "dash" not in (tries[0].reason or "").lower()


@pytest.mark.unit
def test_a_soft_gate_never_destroys_the_last_draft(monkeypatch) -> None:
    """Soft gates re-prompt while budget remains and then yield.

    A draft that misses its shape is worse than one that hits it, and far
    better than no draft at all — the opposite trade to the correctness gates,
    which would rather send nothing. The surviving issue is recorded ON the
    accepted attempt rather than hidden, because it went out with it.
    """
    body, tries = _drive_with_shape(monkeypatch, [NO_QUESTION])

    assert body == NO_QUESTION, "a soft failure must not raise"
    assert len(tries) == d.MAX_DRAFT_ATTEMPTS
    assert tries[-1].outcome == "accepted"
    assert tries[-1].category == "shape_unfulfilled"
    assert tries[-1].reason


@pytest.mark.unit
def test_dash_overuse_is_still_reported_as_dash_overuse(monkeypatch) -> None:
    """The counterpart: naming the branch must not mislabel the other gate."""
    dashes = CLEAN.replace("operations across the GCC",
                           "operations - across - the GCC")
    body, tries = _drive_with_shape(monkeypatch, [dashes, CLEAN])

    assert body == CLEAN
    assert tries[0].category == "dash_overuse"
    assert "dash" in (tries[0].reason or "")
