"""Style: the fingerprint left across many emails, not inside one.

The five live drafts reviewed on 2026-09-08 were individually good and
collectively a tell. Every one came out in the same order — observation,
interpretation, Cortivo, "I don't know if this is relevant", question — and
leaned on a dash to join each observation to its explanation. The
honest-uncertainty line had quietly become the new template component, which is
the same failure the old filler was removed for, in a more likeable costume.

Nothing here tests whether one email is good. Each test asks whether a hundred
of them would look like one author.
"""

from __future__ import annotations

import pytest

from linkedin_agent import drafter as d
from linkedin_agent.evidence import (
    available_shapes,
    build_evidence,
    choose_shape,
    detect_signals,
)
from linkedin_agent.providers.capabilities import Position, ProfileFacts


def _pos(company, title, start, end=None, current=False):
    return Position(company=company, title=title, start_date=start,
                    end_date=end, is_current=current, date_precision="month")


def _post(text):
    return {"text": text, "posted_at": "2026-08-01T00:00:00Z"}


FACTS = ProfileFacts(full_name="Test Person", headline="CEO",
                     positions=[_pos("Acme", "CEO", "2024-01-01", current=True)])

LICENSED = build_evidence(FACTS, [_post("We're hiring engineers"),
                                  _post("Another thing we published")])
UNLICENSED = build_evidence(FACTS, [_post("A thought about the industry")])
THIN = build_evidence(FACTS, [])


# ===================== signals must be about the author ======================
# Vinay Goel is a recruiter. His post said a placement was joining a client
# "following their recent funding round" — StudentCrowd's round, not his. The
# bare `funding round` alternative matched and licensed a claim that he was
# under pressure to ship what he had raised. He had raised nothing.

@pytest.mark.unit
@pytest.mark.parametrize("text,why", [
    ("He's joining StudentCrowd following their recent funding round.",
     "a client's raise"),
    ("Following substantial institutional investment, the business is expanding.",
     "a client's investment"),
    ("New Account Manager role. Our client is a fast-growing data business.",
     "a client's vacancy"),
    ("Pivot asked us to find them a Business Development leader.",
     "a client's search"),
    ("Congratulations to the team at Acme on closing their Series A.",
     "someone else's round"),
])
def test_events_belonging_to_someone_else_are_not_signals(text, why) -> None:
    assert detect_signals([_post(text)]) == [], why


@pytest.mark.unit
@pytest.mark.parametrize("text,expected", [
    ("We're hiring engineers this quarter", "hiring_engineers"),
    ("WE’RE HIRING | DATA ENGINEER", "hiring_engineers"),
    ("We've just raised our seed round", "fundraise"),
    ("We closed our Series A last week", "fundraise"),
])
def test_the_authors_own_events_still_signal(text, expected) -> None:
    """First-person discipline must not mute the real thing."""
    assert [s.name for s in detect_signals([_post(text)])] == [expected]


# =========================== shape variation =================================

@pytest.mark.unit
def test_a_list_of_prospects_spreads_across_shapes() -> None:
    """The point of the whole mechanism. One shape for everyone is the
    fingerprint; the evidence still decides what may be said."""
    chosen = {choose_shape(LICENSED, f"https://linkedin.com/in/p{i}").name
              for i in range(30)}
    assert len(chosen) >= 4, f"shapes collapsed to {chosen}"


@pytest.mark.unit
def test_the_same_prospect_always_gets_the_same_shape() -> None:
    """Re-running a profile must be reproducible, or you cannot tell a real
    change from resampling."""
    key = "https://www.linkedin.com/in/wiktoria-brzezicka-architekt/"
    assert choose_shape(LICENSED, key) == choose_shape(LICENSED, key)


@pytest.mark.unit
def test_the_hypothesis_shape_needs_a_licence() -> None:
    """Shape orders what may be said. It can never widen it."""
    names = [s.name for s in available_shapes(UNLICENSED)]
    assert "observation_hypothesis_question" not in names
    assert "observation_hypothesis_question" in [s.name for s in
                                                 available_shapes(LICENSED)]


@pytest.mark.unit
def test_the_two_observation_shape_needs_two_observations() -> None:
    assert "two_observations_question" not in [s.name for s in
                                               available_shapes(THIN)]


@pytest.mark.unit
def test_thin_evidence_still_has_shapes_to_choose_from() -> None:
    """Restraint must not collapse to a single forced order either."""
    assert len(available_shapes(THIN)) >= 2
    assert choose_shape(THIN, "anything") in available_shapes(THIN)


@pytest.mark.unit
def test_the_shape_reaches_the_prompt() -> None:
    shape = choose_shape(LICENSED, "k")
    inp = d.DrafterInput(kind="email1", campaign={}, prospect={},
                         evidence=LICENSED.as_dict(shape))
    prompt = d.render_prompt(inp)
    assert shape.name in prompt
    assert shape.outline[:40] in prompt


@pytest.mark.unit
def test_evidence_without_a_shape_still_serialises() -> None:
    """Callers that don't pass one must keep working."""
    assert "shape" not in LICENSED.as_dict()


# ============================== dash discipline ==============================

@pytest.mark.unit
@pytest.mark.parametrize("body,n", [
    ("No dashes at all here.", 0),
    ("One clause — then another.", 1),
    ("One — two — three.", 2),
    ("An en dash – counts too.", 1),
    ("A spaced hyphen - counts as well.", 1),
    ("Hyphenated-words and em-dash-free prose do not count.", 0),
])
def test_dash_counting(body, n) -> None:
    assert d.count_connector_dashes(body) == n


@pytest.mark.unit
def test_one_dash_is_allowed() -> None:
    """Banning them outright reads as artificially constrained, which is the
    same tell from the other side."""
    assert d._overuses_dashes("A clause — then the rest of it.") is None


@pytest.mark.unit
def test_three_dashes_is_flagged() -> None:
    assert d._overuses_dashes("a — b — c — d") is not None


# ---- the soft-gate contract: re-prompt, but never lose the draft ----

# Both clear the 300-char email floor. Twice already a fixture has been shorter
# than that, so the LENGTH gate rejected it before the gate under test ever ran
# and the test passed while exercising nothing — the guard at the bottom of
# this module pins it for every fixture here.
STYLED = ("Hi there,\n\nYour post on parallel agents stood out — mostly "
          "the part about Docker behaving differently — and the env files "
          "picking their moment.\n\nI'm Haque, co-founder of Cortivo. We "
          "build custom software for teams who would rather not hire and "
          "manage a whole in-house team just to get something shipped.\n\n"
          "Would this be relevant on your side?\n\nBest,\nHaque")
PLAIN = ("Hi there,\n\nYour post on parallel agents stood out, mostly the "
         "part about Docker behaving differently and the env files picking "
         "their moment.\n\nI'm Haque, co-founder of Cortivo. We build "
         "custom software for teams who would rather not hire and manage a "
         "whole in-house team just to get something shipped.\n\n"
         "Would this be relevant on your side?\n\nBest,\nHaque")


def _drive(monkeypatch, responses, max_attempts=3):
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
    body = d.draft("email1", 1, evidence=UNLICENSED.as_dict(),
                   attempts_out=tries, max_attempts=max_attempts)
    return body, tries, seen


@pytest.mark.unit
def test_dash_overuse_drives_a_retry(monkeypatch) -> None:
    body, tries, seen = _drive(monkeypatch, [STYLED, PLAIN])
    assert body == PLAIN
    assert tries[0].category == "dash_overuse"
    assert "one dash" in seen[1].lower()


@pytest.mark.unit
def test_a_style_issue_never_destroys_the_draft(monkeypatch) -> None:
    """The load-bearing difference between a soft gate and a real one. An
    email with two dashes is worse than one with one, and far better than no
    email at all — a correctness gate would rather send nothing; this one
    would not."""
    body, tries, _ = _drive(monkeypatch, [STYLED], max_attempts=2)
    assert body == STYLED, "a style preference must not cost us the email"
    assert tries[-1].outcome == "accepted"


@pytest.mark.unit
def test_a_surviving_style_issue_is_recorded_not_hidden(monkeypatch) -> None:
    """It went out with the flaw; the trace should say so."""
    _, tries, _ = _drive(monkeypatch, [STYLED], max_attempts=1)
    accepted = tries[-1]
    assert accepted.outcome == "accepted"
    assert accepted.category == "style_warning"
    assert "dashes" in (accepted.reason or "")


@pytest.mark.unit
def test_a_clean_draft_carries_no_warning(monkeypatch) -> None:
    _, tries, _ = _drive(monkeypatch, [PLAIN])
    assert tries[-1].outcome == "accepted"
    assert tries[-1].category is None


@pytest.mark.unit
def test_correctness_gates_still_outrank_style(monkeypatch) -> None:
    """A draft that invents a pain claim must be rejected on that, not
    accepted with a style note, even on the final attempt."""
    invents = ("Hi there,\n\nMost teams at that transition point end up "
               "rebuilding internal reporting by hand, and it quietly eats "
               "a quarter before anyone notices what is happening.\n\nI'm "
               "Haque, co-founder of Cortivo. We build custom software for "
               "teams who would rather not hire in-house.\n\nWould this be "
               "relevant?\n\nBest,\nHaque")
    with pytest.raises(d.DrafterError):
        _drive(monkeypatch, [invents], max_attempts=1)


@pytest.mark.unit
@pytest.mark.parametrize("name,body", [("STYLED", STYLED), ("PLAIN", PLAIN)])
def test_fixtures_clear_the_length_floor(name, body) -> None:
    """Guards every test above. Twice now a fixture has been shorter than the
    300-char email minimum, so the LENGTH gate rejected it first and the test
    passed while exercising nothing it claimed to."""
    assert len(body) >= d.KIND_MIN_CHARS["email1"], f"{name}: {len(body)} chars"


@pytest.mark.unit
def test_length_is_measured_on_the_body_not_the_subject_line(monkeypatch) -> None:
    """A live run accepted a 292-char body against a 300-char floor because
    "Subject: your SDET and AI security roles" made up the difference. The
    gate and the validation stage were measuring two different strings.
    """
    body = "x" * (d.KIND_MIN_CHARS["email1"] - 20)
    raw = f"Subject: a subject long enough to close the gap\n\n{body}"
    assert len(raw) > d.KIND_MIN_CHARS["email1"], "fixture must span the floor"

    monkeypatch.setattr(d, "_invoke_claude", lambda p, timeout=90: raw)
    monkeypatch.setattr(d, "build_input",
                        lambda kind, pid, recent_posts=None, evidence=None:
                        d.DrafterInput(kind=kind, campaign={"brief": ""},
                                       prospect={}))
    tries: list = []
    with pytest.raises(d.DrafterError):
        d.draft("email1", 1, attempts_out=tries, max_attempts=1)
    assert tries[0].category == "length_under"


@pytest.mark.unit
def test_content_gates_still_see_the_subject(monkeypatch) -> None:
    """Only length ignores the subject. A subject can carry a stock phrase —
    "<Company> — shipping without hiring a team" was one — so the content
    gates must keep reading it."""
    raw = ("Subject: Acme, shipping without hiring a team\n\n"
           + "Hi there, this body is otherwise entirely fine and long "
             "enough to clear the floor without any trouble at all. " * 3)
    assert d._contains_filler(raw) is not None
