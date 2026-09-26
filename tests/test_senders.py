"""Who a message is from, and what that person may claim about themselves.

The GTM brief asked every email to "lead with the authority of two decades of
experience". No company source supports it, and asked directly Manav confirmed
it is his own: "a personal experience and valid for me, but will be different
for someone else".

That makes it a SENDER credential. The tests below pin the three things that
follow from his answer:

  * it is licensed only when Manav is the sender
  * even then, only in the first person — he said personal, so "we bring two
    decades" is false for him too
  * a collective seniority figure is licensed for nobody
"""

from __future__ import annotations

import pytest

from linkedin_agent import drafter as d
from linkedin_agent import evidence as evidence_mod
from linkedin_agent import senders


# ===================== loading and resolution ==============================

@pytest.mark.unit
def test_the_default_sender_preserves_the_old_behaviour(monkeypatch) -> None:
    """Every email was signed by Haque before senders existed."""
    monkeypatch.delenv("OUTREACH_SENDER", raising=False)
    assert senders.resolve().slug == "haque"


@pytest.mark.unit
def test_a_campaign_can_name_its_sender(monkeypatch) -> None:
    monkeypatch.delenv("OUTREACH_SENDER", raising=False)
    assert senders.resolve("manav").slug == "manav"


@pytest.mark.unit
def test_the_environment_can_name_the_sender(monkeypatch) -> None:
    monkeypatch.setenv("OUTREACH_SENDER", "manav")
    assert senders.resolve().slug == "manav"


@pytest.mark.unit
def test_the_campaign_outranks_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("OUTREACH_SENDER", "manav")
    assert senders.resolve("haque").slug == "haque"


@pytest.mark.unit
def test_an_unknown_sender_is_a_hard_error_not_a_fallback(monkeypatch) -> None:
    """Quietly signing with a different person's name is worse than failing to
    draft — so a named sender without a profile must not fall back."""
    monkeypatch.delenv("OUTREACH_SENDER", raising=False)
    with pytest.raises(senders.SenderError, match="Refusing to fall back"):
        senders.resolve("nobody-by-this-name")


@pytest.mark.unit
def test_each_sender_signs_as_themselves_at_agentic_labs() -> None:
    assert senders.load("manav").sign_off == "Best,\nManav\nAgentic Labs"
    assert senders.load("haque").sign_off == "Best,\nHaque\nAgentic Labs"


@pytest.mark.unit
def test_only_manav_carries_a_personal_seniority_claim() -> None:
    assert senders.load("manav").personal_seniority
    assert senders.load("haque").personal_seniority is None


@pytest.mark.unit
def test_a_sender_without_a_calendar_gets_none_not_someone_elses() -> None:
    """The prompt used to hardcode Haque's calendar link, so every sender would
    have handed out his calendar."""
    assert senders.load("manav").calendar_url is None
    assert "haque" in (senders.load("haque").calendar_url or "")


# ===================== personal seniority ==================================

MANAV = senders.load("manav").as_dict()
HAQUE = senders.load("haque").as_dict()


@pytest.mark.unit
def test_manav_may_state_his_own_two_decades() -> None:
    body = ("I've spent two decades working across sectors and leadership "
            "profiles, and this is the pattern I see most.")
    assert d._contains_unsupported_authority(body, MANAV) is None


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    "We bring two decades of experience across sectors.",
    "Our team has twenty years of this behind it.",
    "Agentic Labs has decades of experience in exactly this.",
])
def test_manav_may_not_attribute_it_to_the_team(body) -> None:
    """He said personal. Attached to "we", it becomes a claim about the team
    that nobody has made — false even when he is the one sending."""
    found = d._contains_unsupported_authority(body, MANAV)
    assert found is not None and "not the sender's own" in found


@pytest.mark.unit
def test_nobody_else_may_state_it_at_all() -> None:
    body = "I've spent two decades working across sectors."
    assert d._contains_unsupported_authority(body, HAQUE) is not None


@pytest.mark.unit
def test_with_no_sender_nothing_is_licensed() -> None:
    """Fail closed: no sender payload means no personal claim is licensed."""
    body = "I've spent two decades working across sectors."
    assert d._contains_unsupported_authority(body, None) is not None


@pytest.mark.unit
@pytest.mark.parametrize("sender", [MANAV, HAQUE, None])
@pytest.mark.parametrize("body", [
    "Between us we have decades of combined experience.",
    "We collectively bring more than anyone you'll hire.",
])
def test_a_collective_seniority_figure_is_licensed_for_nobody(sender, body) -> None:
    assert d._contains_unsupported_authority(body, sender) is not None


# ===================== unapproved engagement claims ========================

@pytest.mark.unit
@pytest.mark.parametrize("sender", [MANAV, HAQUE, None])
@pytest.mark.parametrize("body", [
    "One engineer of ours is what you'd otherwise hire three or four people to do.",
    "You get a 3-4 person team for one engineer's cost.",
    "A typical engagement runs six to ten weeks, kickoff to live users.",
    "We usually go from kickoff to live users in 6-10 weeks.",
])
def test_the_unapproved_engagement_claims_are_licensed_for_nobody(sender, body) -> None:
    """The brief marks both NOT APPROVED. The digit gate cannot see "3-4":
    both digits are approved for unrelated claims ("Top 3% on Toptal",
    "4 minutes time-to-itinerary"), and it checks whether a number is
    approved, not what it is approved FOR."""
    assert d._contains_unsupported_authority(body, sender) is not None


@pytest.mark.unit
@pytest.mark.parametrize("sender", [MANAV, HAQUE, None])
@pytest.mark.parametrize("body", [
    "A typical engagement is six-to-ten weeks.",
    "Our engagements are six-to-ten weeks.",
    "We usually ship in six to eight weeks.",
    "We put a three-to-four person team on it.",
    "We work as a 3 to 4-person team.",
    "One of our engineers costs what you would otherwise pay three hires.",
    "We have decades of combined engineering experience.",
    "Our team has a twenty-year track record.",
    "We bring 20+ years.",
    "Our team has two-decade careers.",
])
def test_rewording_does_not_get_a_claim_past_the_gate(sender, body) -> None:
    """Measured on 2026-09-26: against the literal lists alone, eight of these
    got through both this gate and the brief-vocabulary gate. A hyphen, a plus
    sign, a spelled-out number or one extra word was enough. None of them is
    licensed for anyone — Manav's credential is his, so "our team has a
    twenty-year track record" is false even when he sends."""
    assert d._contains_unsupported_authority(body, sender) is not None


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    "Could we find 15 minutes in the next two weeks?",
    "We are 15+ developers across 10+ industries.",
    "You have spent 12 years at Deloitte, which is a long run.",
    "I have spent 5+ years building production AI.",
    "We built Gymed, which goes from intake to plan in 60s.",
    "Tango cut time-to-itinerary to 4 minutes for us.",
])
def test_catching_rewordings_does_not_catch_ordinary_sentences(body) -> None:
    """The shapes are matched only in sentences about us, and a duration only
    in a sentence about delivering work. An ask with a timeframe, the
    prospect's own tenure, a published scale claim and Haque's documented
    "5+ years" all stay sayable."""
    assert d._contains_unsupported_authority(body, HAQUE) is None


@pytest.mark.unit
def test_manav_may_state_a_reworded_credential_in_the_first_person() -> None:
    assert d._contains_unsupported_authority(
        "I've had a two-decade run across sectors and leadership profiles.",
        MANAV) is None


# ===================== the catalogue may not contradict the brief ==========

@pytest.mark.unit
def test_no_positioning_angle_instructs_an_unapproved_claim() -> None:
    """An angle is an instruction to the drafter. An angle asserting a claim
    the brief forbids is the catalogue telling the drafter to break the brief.

    Two did exactly that and were live — "three or four people" for any
    founder, "six to ten weeks" on any fundraise signal — after the brief had
    already marked both NOT APPROVED. This keeps the catalogue from drifting
    out of sync with the brief again: add an angle that asserts a forbidden
    claim and this fails.
    """
    for angle in evidence_mod._POSITIONING:
        found = d._contains_unsupported_authority(angle.angle, None)
        assert found is None, (
            f"positioning angle {angle.name!r} instructs {found!r}, which the "
            f"approved brief does not permit")


@pytest.mark.unit
def test_the_prompt_does_not_instruct_an_unapproved_claim() -> None:
    """The positioning catalogue had this problem, and so did the prompt: after
    the brief marked both NOT APPROVED, the prompt still held up "a six-to-ten
    week engagement" and "three or four people" as the brief's best unused
    material, and told the drafter what to say for the removed
    `engagement_shape` angle.

    The prompt cannot be run through the gate whole — it has to name "two
    decades" to explain the first-person rule — so this checks the phrasings
    that are never legitimate in it."""
    prompt = d._load_subagent_prompt().lower()
    never = (d._COLLECTIVE_SENIORITY_TELLS + d._UNAPPROVED_ENGAGEMENT_TELLS
             + ("six-to-ten", "engagement_shape", "small_team_equivalent"))
    for phrase in never:
        assert phrase not in prompt, f"the drafter prompt still says {phrase!r}"


# ===================== reaching the prompt =================================

@pytest.mark.unit
def test_the_prompt_signs_as_the_sender_not_as_a_hardcoded_person() -> None:
    prompt = d._load_subagent_prompt()
    assert "`Best,` / `Haque`" not in prompt
    assert "sender.sign_off" in prompt
    assert "cal.com/haque" not in prompt, "a sender would hand out Haque's calendar"


@pytest.mark.integration
def test_the_sender_reaches_the_drafter_input(db_env, monkeypatch) -> None:
    from linkedin_agent import db

    monkeypatch.setenv("OUTREACH_SENDER", "manav")
    pid = db.upsert_prospect(linkedin_url="https://www.linkedin.com/in/sender-check",
                             full_name="Sam Lee")
    inp = d.build_input("dm1", pid)

    assert inp.sender["name"] == "Manav"
    assert inp.sender["sign_off"].endswith("Agentic Labs")
    assert inp.sender["personal_seniority"]
