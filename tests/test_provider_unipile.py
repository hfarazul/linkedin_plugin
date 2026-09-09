"""PB-1 — UnipileProvider: normalization and error translation.

Fixtures use the exact response shape captured live during P0-1 against
api62.unipile.com, not an invented one.

The date-precision logic carries the most weight here. Unipile returns
"M/D/YYYY" with the day always 1, so "1/1/2015" is indistinguishable from a
genuine January. Treating that as month-precision would let a detector claim
"started within 180 days" from what is really only a year — a fabricated
recency signal attached to a real person's name.
"""

from __future__ import annotations

import httpx
import pytest

from linkedin_agent.providers.base import (
    ActionFailed,
    MalformedResponse,
    ProviderAuthError,
    ProviderRateLimited,
    ProviderTimeout,
)
from linkedin_agent.providers.capabilities import Capability
from linkedin_agent.providers.unipile import (
    UnipileProvider,
    _parse_date,
    _positions_from_profile,
)


class _Cfg:
    """Minimal stand-in for Config."""
    unipile_api_key = "k" * 20
    unipile_account_id = "a" * 20
    unipile_dsn = "api62.unipile.com:19261"


# Captured live from williamhgates during P0-1.
GATES_PROFILE = {
    "provider_id": "ACoAAA8BYqEBmT-yvJ8XLZ9lS7v0aWKQx1yZabc",
    "public_identifier": "williamhgates",
    "first_name": "Bill", "last_name": "Gates",
    "headline": "Co-chair, Gates Foundation",
    "location": "Seattle, Washington, United States",
    "network_distance": "SECOND_DEGREE",
    "follower_count": 37000000,
    "work_experience": [
        {"company_id": "8736", "company": "Gates Foundation",
         "position": "Co-chair", "start": "1/1/2000", "end": None, "skills": []},
        {"company_id": "19141006", "company": "Breakthrough Energy ",
         "position": "Founder", "start": "1/1/2015", "end": None, "skills": []},
        {"company_id": "1035", "company": "Microsoft",
         "position": "Co-founder", "start": "1/1/1975", "end": None, "skills": []},
    ],
}

# Captured live from reidhoffman — month-level precision does occur.
HOFFMAN_POSITIONS = [
    {"company": "Inflection AI", "position": "Co-Founder",
     "start": "3/1/2022", "end": None},
    {"company": "Greylock", "position": "Partner",
     "start": "11/1/2009", "end": "8/1/2017"},
]


# ----------------------------- date parsing ----------------------------------

@pytest.mark.unit
def test_month_precision_is_recognised() -> None:
    assert _parse_date("3/1/2022") == ("2022-03-01", "month")
    assert _parse_date("11/1/2009") == ("2009-11-01", "month")


@pytest.mark.unit
def test_january_first_is_treated_as_year_only() -> None:
    """The honest reading. LinkedIn emits 1/1/YYYY when only a year is known,
    and it is indistinguishable from an actual January start."""
    assert _parse_date("1/1/2015") == ("2015-01-01", "year")


@pytest.mark.unit
def test_missing_and_unparseable_dates() -> None:
    assert _parse_date(None) == (None, "unknown")
    assert _parse_date("") == (None, "unknown")
    assert _parse_date("sometime in 2021") == ("sometime in 2021", "unknown")
    assert _parse_date(12345) == (None, "unknown")


@pytest.mark.unit
def test_year_precision_cannot_support_a_recency_window() -> None:
    """has_usable_start is what stops a detector inventing recency."""
    gates = _positions_from_profile(GATES_PROFILE)
    assert all(p.date_precision == "year" for p in gates)
    assert not any(p.has_usable_start for p in gates)

    hoffman = _positions_from_profile({"work_experience": HOFFMAN_POSITIONS})
    assert all(p.has_usable_start for p in hoffman)


# ----------------------------- position mapping ------------------------------

@pytest.mark.unit
def test_positions_are_normalized() -> None:
    positions = _positions_from_profile(GATES_PROFILE)
    assert len(positions) == 3
    ms = [p for p in positions if p.company == "Microsoft"][0]
    assert ms.title == "Co-founder"
    assert ms.company_id == "1035"
    assert ms.start_date == "1975-01-01"
    assert ms.source == "unipile"
    assert ms.raw["position"] == "Co-founder"


@pytest.mark.unit
def test_null_end_date_means_current() -> None:
    positions = _positions_from_profile(GATES_PROFILE)
    assert all(p.is_current for p in positions)


@pytest.mark.unit
def test_concurrent_current_roles_are_preserved() -> None:
    """Gates holds three open-ended roles at once. A detector cannot assume
    positions[1] is 'the previous employer' -- this fixture is the guard."""
    current = [p for p in _positions_from_profile(GATES_PROFILE) if p.is_current]
    assert len(current) == 3


@pytest.mark.unit
def test_ended_position_is_not_current() -> None:
    positions = _positions_from_profile({"work_experience": HOFFMAN_POSITIONS})
    greylock = [p for p in positions if p.company == "Greylock"][0]
    assert greylock.is_current is False
    assert greylock.end_date == "2017-08-01"


@pytest.mark.unit
def test_missing_experience_returns_empty() -> None:
    assert _positions_from_profile({"headline": "x"}) == []
    assert _positions_from_profile({"work_experience": None}) == []


# ----------------------------- capability declaration ------------------------

@pytest.mark.unit
def test_unconfigured_provider_supports_nothing() -> None:
    class Empty:
        unipile_api_key = None
        unipile_account_id = None
        unipile_dsn = None

    p = UnipileProvider(Empty())
    assert not p.supports(Capability.PROFILE)
    assert not p.supports(Capability.SEARCH_PEOPLE)


@pytest.mark.unit
def test_configured_provider_declares_its_capabilities() -> None:
    p = UnipileProvider(_Cfg())
    for cap in (Capability.SEARCH_PEOPLE, Capability.SEARCH_POSTS,
                Capability.PROFILE, Capability.EXPERIENCE,
                Capability.INBOX_READ, Capability.CONNECT):
        assert p.supports(cap), cap
    # Not implemented against Unipile today.
    assert not p.supports(Capability.WITHDRAW_INVITE)


# ----------------------------- error translation -----------------------------

def _status_error(code: int, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/x")
    response = httpx.Response(code, text=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.unit
@pytest.mark.parametrize("code,expected", [
    (401, ProviderAuthError),
    (403, ProviderAuthError),
    (429, ProviderRateLimited),
    (500, MalformedResponse),
])
def test_http_errors_map_to_the_taxonomy(code, expected) -> None:
    """The mapping decides the fallback outcome, so it is pinned explicitly."""
    p = UnipileProvider(_Cfg())
    assert isinstance(p._translate(_status_error(code)), expected)


@pytest.mark.unit
def test_422_becomes_action_failed_with_its_code() -> None:
    """422 already_invited_recently is what the cooldown system reads."""
    p = UnipileProvider(_Cfg())
    err = _status_error(422, '{"type": "errors/already_invited_recently"}')
    out = p._translate(err)
    assert isinstance(out, ActionFailed)
    assert out.error_code == "errors/already_invited_recently"


@pytest.mark.unit
def test_timeout_maps_to_provider_timeout() -> None:
    p = UnipileProvider(_Cfg())
    assert isinstance(p._translate(httpx.ReadTimeout("slow")), ProviderTimeout)


@pytest.mark.unit
def test_unknown_exceptions_pass_through_unchanged() -> None:
    """Don't dress up a genuine bug as a provider failure."""
    p = UnipileProvider(_Cfg())
    original = ValueError("a real bug")
    assert p._translate(original) is original
