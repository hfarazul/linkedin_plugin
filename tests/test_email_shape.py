"""The emails themselves: do they differ because the evidence differs?

The complaint that prompted this file was not that the old emails were wrong
in a detail. It was that swapping the name and company produced the same email
every time — technically personalised, substantively a mail merge. A test that
only checks "does it contain the first name" would have passed throughout.

So the property here is comparative: mask the names and companies, and two
prospects with materially different evidence must still produce materially
different text. If they do not, the personalisation lived entirely in the
substitutions.
"""

from __future__ import annotations

import importlib.util
import re
from difflib import SequenceMatcher
from pathlib import Path

import pytest

from linkedin_agent import drafter as d
from linkedin_agent.evidence import Tier, build_evidence
from linkedin_agent.providers.capabilities import Position, ProfileFacts

ROOT = Path(__file__).resolve().parent.parent


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "smoke_e2e", ROOT / "scripts" / "smoke_e2e.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_harness()


def _pos(company, title, start, end=None, current=False):
    return Position(company=company, title=title, start_date=start,
                    end_date=end, is_current=current, date_precision="month")


def _post(text):
    return {"text": text, "posted_at": "2026-08-01T00:00:00Z"}


# The three evidence states, as different people.
STRONG = (
    ProfileFacts(full_name="Vincent Picot", headline="Group CFO",
                 positions=[_pos("Tikehau Capital", "Group CFO",
                                 "2023-10-01", current=True)]),
    [_post("We are looking for an exceptional candidate to join our Private "
           "Debt team in London as an Investment Analyst.")],
)
MODERATE = (
    ProfileFacts(full_name="Ghazi Sultan", headline="Engineer",
                 positions=[_pos("Pawp", "Senior Python Engineer",
                                 "2025-05-01", current=True)]),
    [_post("Parallel coding agents changed my workflow. Not in the way I "
           "expected. I have been using coding agents for months now.")],
)
WEAK = (
    ProfileFacts(full_name="Ahmed Ashfaq", headline="Director of Operations",
                 positions=[
                     _pos("Millennium Hotels", "Director of Operations",
                          "2025-06-01", current=True),
                     _pos("Jawakara Islands Maldives", "Cluster Resort Manager",
                          "2023-01-01", "2025-06-01"),
                 ]),
    [],
)


def _email(case, direct=False):
    facts, posts = case
    bundle = build_evidence(facts, posts, transition_is_direct=direct)
    return smoke._stub_email(facts, bundle)


_NAMES = re.compile(
    r"(?i)\b(vincent|ghazi|ahmed|paula|anjan|tikehau capital|tikehau|pawp|"
    r"millennium hotels|millennium|jawakara islands maldives|jawakara|"
    r"talkinglands|dan lab)\b")


def _mask(text: str) -> str:
    """Strip every name and company, leaving only the structure."""
    return _NAMES.sub("X", text)


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _mask(a), _mask(b)).ratio()


# ========================= 1: not a mail merge ===============================

@pytest.mark.unit
@pytest.mark.parametrize("left,right,label", [
    (STRONG, MODERATE, "strong vs moderate"),
    (STRONG, WEAK, "strong vs weak"),
    (MODERATE, WEAK, "moderate vs weak"),
])
def test_different_evidence_produces_different_emails(left, right, label) -> None:
    """Requirement 1, stated as the thing that actually matters: once the
    names are masked out, is there anything left that differs?

    The old template scored ~0.99 here for every pair — the only difference
    between two prospects' emails was the substitutions.
    """
    score = _similarity(_email(left), _email(right))
    assert score < 0.6, (
        f"{label}: emails are {score:.0%} identical after masking names — "
        f"the personalisation is only substitution")


@pytest.mark.unit
def test_the_old_template_would_fail_this_test() -> None:
    """A control. Without it, the threshold above is an unfalsified guess.

    This is the previous output for two of the profiles above, verbatim from
    real runs on 2026-09-03/04.
    """
    old_a = ("Hi Vincent,\n\nWe have yet to be properly introduced, but I'm "
             "Haque with Cortivo, and what caught my eye is the work you are "
             "doing at Tikehau Capital.\n\nTeams building at that stage end up "
             "rebuilding internal tooling and data pipelines by hand while the "
             "product gets the attention.\n\nThat's our outside read. Curious "
             "if the real squeeze at Tikehau Capital is closer to go-to-market "
             "ops or product velocity, or somewhere we haven't surfaced.")
    old_b = old_a.replace("Vincent", "Ghazi").replace("Tikehau Capital", "Pawp")
    assert _similarity(old_a, old_b) > 0.95, \
        "the control must reproduce the mail-merge it is controlling for"


# ===================== emails obey their own evidence ========================

@pytest.mark.unit
@pytest.mark.parametrize("case,direct", [
    (MODERATE, False),
    (WEAK, True),
])
def test_an_unlicensed_email_diagnoses_nothing(case, direct) -> None:
    """No signal means no claim — not a softer claim."""
    facts, posts = case
    bundle = build_evidence(facts, posts, transition_is_direct=direct)
    assert bundle.pain_claim_licensed is False
    body = smoke._stub_email(facts, bundle)
    assert d._contains_unsupported_pain_claim(body) is None, body


@pytest.mark.unit
@pytest.mark.parametrize("case,direct", [
    (STRONG, False), (MODERATE, False), (WEAK, True),
])
def test_no_email_reuses_the_old_template(case, direct) -> None:
    facts, posts = case
    bundle = build_evidence(facts, posts, transition_is_direct=direct)
    assert d._contains_filler(smoke._stub_email(facts, bundle)) is None


@pytest.mark.unit
def test_a_licensed_email_may_reference_what_they_published() -> None:
    """The fix removes invention, not personalisation. A strong signal must
    still reach the page, or we have traded one failure for another."""
    facts, posts = STRONG
    bundle = build_evidence(facts, posts)
    body = smoke._stub_email(facts, bundle)
    assert bundle.tier is Tier.STRONG
    assert "hiring" in body.lower()


@pytest.mark.unit
def test_the_weak_email_is_short() -> None:
    """Restraint has to be visible. Padding a thin email to look substantial
    is the same failure wearing a different coat."""
    facts, posts = WEAK
    body = smoke._stub_email(facts, build_evidence(facts, posts,
                                                   transition_is_direct=True))
    assert len(body) < 700, f"weak-evidence email is {len(body)} chars"


@pytest.mark.unit
@pytest.mark.parametrize("case,direct", [
    (STRONG, False), (MODERATE, False), (WEAK, True),
])
def test_every_email_still_passes_the_pre_existing_gates(case, direct) -> None:
    """Requirement 10. The new gates must not have been bought by weakening
    the old ones."""
    facts, posts = case
    body = smoke._stub_email(facts, build_evidence(facts, posts,
                                                   transition_is_direct=direct))
    assert d._contains_spam_tell(body) is None, body
    assert d._contains_surveillance_tell(body) is None, body
    assert not re.search(r"https?://|cal\.com", body), body
    assert len(body) <= d.KIND_MAX_CHARS["email1"]
    assert len(body) >= d.KIND_MIN_CHARS["email1"]


@pytest.mark.unit
def test_signal_sentences_are_written_in_second_person() -> None:
    """A live run produced "You wrote about are hiring" and "If they are
    adding build capacity" — third-person payload strings spliced into an
    email addressed to the person they describe."""
    facts, posts = STRONG
    body = smoke._stub_email(facts, build_evidence(facts, posts))
    assert "about are hiring" not in body
    assert "If they are" not in body
    assert "they are adding" not in body


@pytest.mark.unit
def test_a_quote_is_a_whole_sentence_or_nothing() -> None:
    """A live run quoted "...using coding agents for a" — a character-count
    truncation. A broken quote shows the machine more clearly than no quote."""
    facts, posts = MODERATE
    body = smoke._stub_email(facts, build_evidence(facts, posts))
    quoted = re.search(r'"([^"]+)"', body)
    assert quoted, body
    assert quoted.group(1).rstrip().endswith((".", "!", "?")), quoted.group(1)


@pytest.mark.unit
def test_an_unquotable_post_falls_back_rather_than_quoting_a_fragment() -> None:
    """One 400-character sentence has no clean cut. Say less instead."""
    facts = ProfileFacts(full_name="Test Person",
                         positions=[_pos("Acme", "CEO", "2025-01-01",
                                         current=True)])
    body = smoke._stub_email(facts, build_evidence(facts, [_post("x " * 200)]))
    assert '"' not in body, body
    assert d._contains_unsupported_pain_claim(body) is None
