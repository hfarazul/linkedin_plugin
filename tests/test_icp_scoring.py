"""Tests for the ICP-fit scoring used by `validate-query`.

Heuristics are deterministic — no LLM, no network. We can blast through
many cases fast.
"""

from __future__ import annotations

import pytest

from linkedin_agent.adapters.base import ProspectHit
from linkedin_agent.icp_scoring import (
    CampaignICP,
    grade,
    validate_results,
)


def _hit(name="Test", headline="", location="", url="https://www.linkedin.com/in/test"):
    return ProspectHit(
        linkedin_url=url,
        full_name=name,
        headline=headline,
        location=location,
    )


# ===== individual signals ===================================================

@pytest.mark.unit
@pytest.mark.parametrize("location,expected", [
    ("New York, NY",                 True),
    ("San Francisco Bay Area",       True),
    ("United Kingdom",               True),
    ("London",                       True),
    ("Berlin",                       True),
    ("Madrid, Spain",                True),
    ("United States",                True),
    ("Bengaluru",                    False),
    ("Mumbai",                       False),
    ("Dubai, United Arab Emirates",  False),
    ("Singapore",                    False),
    ("",                              False),
    (None,                            False),
])
def test_geo_match(location, expected):
    g = grade(_hit(location=location, headline="Founder at Acme"))
    assert g.geo_match is expected


@pytest.mark.unit
@pytest.mark.parametrize("headline,expected", [
    ("Founder at Acme",              True),
    ("Co-Founder & CEO at Acme",     True),
    ("CEO of Tilt",                  True),
    ("Owner, Coding Sphere",         True),
    ("Principal Engineer at Google", True),  # "principal" — keepers as currently defined
    ("Senior Engineer at Amazon",    False),
    ("Product Manager at Acme",      False),
    ("",                              False),
])
def test_role_match(headline, expected):
    g = grade(_hit(headline=headline, location="New York, NY"))
    assert g.role_match is expected


@pytest.mark.unit
@pytest.mark.parametrize("headline,expected_clean", [
    ("Founder at Acme",                       True),
    ("CEO + Investor",                        False),   # 'investor' is noise
    ("Founder, Angel Investor at Y Combinator", False),
    ("Founder & VC Partner",                  False),
    ("Founder & Career Coach",                False),
    ("Founder, Sales Consultant",             False),
    ("Founder · Recruiter for tech roles",    False),
    ("Founder",                                True),
])
def test_noise_exclusion(headline, expected_clean):
    g = grade(_hit(headline=headline, location="New York, NY"))
    assert g.noise_excluded is expected_clean


# ===== composite keeper check ===============================================

@pytest.mark.unit
def test_keeper_requires_all_three():
    # All three pass
    g = grade(_hit(headline="Founder at Acme", location="New York, NY"))
    assert g.is_keeper

    # Geo fails
    g = grade(_hit(headline="Founder at Acme", location="Bengaluru"))
    assert not g.is_keeper

    # Role fails
    g = grade(_hit(headline="VP Engineering at Acme", location="New York, NY"))
    assert not g.is_keeper

    # Noise fails
    g = grade(_hit(headline="Founder & Angel Investor", location="New York, NY"))
    assert not g.is_keeper


# ===== validate_results aggregation =========================================

@pytest.mark.unit
def test_validation_passes_at_threshold():
    hits = [
        _hit(name=f"Person {i}", headline="Founder at X", location="New York, NY")
        for i in range(7)
    ] + [
        _hit(name=f"Person {i}", headline="VC at Y", location="Singapore")
        for i in range(3)
    ]
    result = validate_results(hits, threshold=6)
    assert result.keeper_count == 7
    assert result.total == 10
    assert result.passes


@pytest.mark.unit
def test_validation_fails_below_threshold():
    """Real-world: 'we just raised' returned 9 investors + 1 founder. Should
    not pass quality gate."""
    hits = [
        _hit(name="Founder", headline="Founder & CEO at Catalogue", location="New York, NY"),
    ] + [
        _hit(name=f"VC {i}", headline=f"Investor at fund {i}", location="New York, NY")
        for i in range(9)
    ]
    result = validate_results(hits, threshold=6)
    assert result.keeper_count == 1
    assert result.total == 10
    assert not result.passes


# ===== per-campaign overrides ==============================================

@pytest.mark.unit
def test_campaign_overrides_role_required():
    """A campaign targeting Product roles (not Founder) overrides the
    default role pattern."""
    icp = CampaignICP.from_brief_meta({
        "icp_role_required": r"\b(product manager|head of product|vp product)\b",
        "icp_role_excluded": r"\b(intern|junior)\b",
    })
    pm = _hit(headline="Product Manager at Acme", location="New York, NY")
    founder = _hit(headline="Founder at Acme", location="New York, NY")
    assert grade(pm, icp).is_keeper
    assert not grade(founder, icp).is_keeper


@pytest.mark.unit
def test_campaign_overrides_geo_required():
    """A SF-only campaign overrides the default US/EU pattern."""
    icp = CampaignICP.from_brief_meta({
        "icp_geo_required": r"San Francisco|Bay Area",
    })
    sf = _hit(headline="Founder at Acme", location="San Francisco, CA")
    nyc = _hit(headline="Founder at Acme", location="New York, NY")
    assert grade(sf, icp).is_keeper
    assert not grade(nyc, icp).is_keeper


@pytest.mark.unit
def test_notes_are_human_readable():
    """Notes should help the user understand why something failed."""
    g = grade(_hit(headline="Founder & Coach", location="Bengaluru"))
    assert any("geo" in n.lower() for n in g.notes)
    assert any("coach" in n.lower() for n in g.notes)
