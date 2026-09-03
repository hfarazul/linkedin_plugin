"""PB-2/PB-3 — PhantomBuster normalization and capability gating.

Every fixture below is a real row captured from the live account (Profile
Scraper agent 5032056022535397, Inbox Scraper agent 1327575646342095). Invented
fixtures would defeat the point: this project has twice reached a wrong
conclusion from documentation, and these tests exist to pin actual behaviour.
"""

from __future__ import annotations

import pytest

from linkedin_agent.providers.capabilities import Capability
from linkedin_agent.providers.phantombuster import (
    PhantomBusterProvider,
    extract_provider_id,
    inbound_from_row,
    parse_date_range,
    profile_from_row,
    prospect_hit_from_row,
)

# --- real Profile Scraper row (trimmed to relevant fields) -------------------
PROFILE_ROW = {
    "firstName": "Anjan", "lastName": "B",
    "linkedinProfileUrn": "ACoAAEd_lPYBmH1En5MdiTMq4m7soEpUXd6QBQ8",
    "linkedinProfileSlug": "anjan-b-35a884295",
    "profileUrl": "https://www.linkedin.com/in/anjan-b-35a884295/",
    "linkedinHeadline": "Software Engineer",
    "location": "Bengaluru, Karnataka, India",
    "connectionDegree": "2nd",
    "linkedinFollowersCount": "512",
    "linkedinConnectionsCount": "500",
    "linkedinJobTitle": "Software Engineer",
    "linkedinJobDateRange": "Feb 2026 - Present",
    "linkedinJobDescription": "GIS-based products",
    "linkedinJobLocation": "Bengaluru",
    "linkedinCompanyName": "TalkingLands",
    "linkedinCompanyId": "82333944",
    "linkedinCompanyUrl": "https://www.linkedin.com/company/talking-lands/",
    "linkedinCompanyEmployeesCount": "42",
    "linkedinCompanySize": "11-50 employees",
    "linkedinPreviousJobTitle": "Co-Founder",
    "linkedinPreviousJobDateRange": "Aug 2025 - Present",
    "linkedinPreviousJobDescription": "Product Research and Agentic Systems",
    "previousCompanyName": "dan Lab",
    "professionalEmail": "",
}

# --- real Inbox Scraper rows -------------------------------------------------
INBOX_REPLY = {   # prospect sent the last message
    "threadUrl": "https://www.linkedin.com/messaging/thread/2-ZGEyNmI5MmIt.../",
    "linkedInUrls": "https://www.linkedin.com/in/ACoAADEv3hgBnfiUSJ1apzGtyJCtRSFQni_zwMU",
    "message": "Thanks, Zakaur",
    "lastMessageFromEntityUrn":
        "urn:li:msg_messagingParticipant:urn:li:fsd_profile:ACoAADEv3hgBnfiUSJ1apzGtyJCtRSFQni_zwMU",
    "firstnameFrom": "Arif", "lastnameFrom": "Hassan",
    "occupationFrom": "Planning Engineer (E&I)",
    "isLastMessageFromMe": "FALSE", "readStatus": "TRUE",
    "lastMessageDate": "2025-05-31T09:57:02.966Z",
}
INBOX_OURS = {    # WE sent the last message -- the *From fields are ours
    "threadUrl": "https://www.linkedin.com/messaging/thread/2-ZmQzM2QwNzIt.../",
    "linkedInUrls": "https://www.linkedin.com/in/ACoAADdAiegBgVhsC0ond2h1_meyUCrSz9vkIn0",
    "message": "Thank you",
    "lastMessageFromEntityUrn":
        "urn:li:msg_messagingParticipant:urn:li:fsd_profile:ACoAADl5vRMBD-omfcGDSbcx3Y4HO11QLxLXcEk",
    "firstnameFrom": "Zakaur", "lastnameFrom": "Rahman",
    "occupationFrom": "Full Stack Engineer",
    "isLastMessageFromMe": "TRUE", "readStatus": "TRUE",
    "lastMessageDate": "2025-03-24T06:07:33.364Z",
}


class _Cfg:
    unipile_api_key = None
    unipile_account_id = None
    unipile_dsn = None


# ----------------------------- provider id -----------------------------------

@pytest.mark.unit
def test_provider_id_from_profile_url() -> None:
    assert extract_provider_id(INBOX_REPLY["linkedInUrls"]) == \
        "ACoAADEv3hgBnfiUSJ1apzGtyJCtRSFQni_zwMU"


@pytest.mark.unit
def test_provider_id_from_urn() -> None:
    assert extract_provider_id(INBOX_REPLY["lastMessageFromEntityUrn"]) == \
        "ACoAADEv3hgBnfiUSJ1apzGtyJCtRSFQni_zwMU"


@pytest.mark.unit
def test_provider_id_absent() -> None:
    assert extract_provider_id(None) is None
    assert extract_provider_id("https://www.linkedin.com/in/plain-slug") is None


# ----------------------------- date ranges -----------------------------------

@pytest.mark.unit
def test_month_precision_range() -> None:
    start, end, current, precision = parse_date_range("Feb 2026 - Present")
    assert (start, end, current, precision) == ("2026-02-01", None, True, "month")


@pytest.mark.unit
def test_closed_range() -> None:
    start, end, current, precision = parse_date_range("Aug 2025 - Jan 2026")
    assert start == "2025-08-01"
    assert end == "2026-01-01"
    assert current is False
    assert precision == "month"


@pytest.mark.unit
def test_year_only_range_is_marked_year_precision() -> None:
    """Year precision must not masquerade as month, or a detector could claim
    'started within 180 days' from a date that only names a year."""
    start, _, _, precision = parse_date_range("2021 - 2023")
    assert start == "2021-01-01"
    assert precision == "year"


@pytest.mark.unit
def test_unparseable_range() -> None:
    assert parse_date_range(None) == (None, None, False, "unknown")
    assert parse_date_range("")[3] == "unknown"


# ----------------------------- profile mapping -------------------------------

@pytest.mark.unit
def test_profile_identity_maps_to_existing_provider_id() -> None:
    """The key finding: PB gives us the same ACoAA id our schema already uses,
    so prospect matching needs no new logic."""
    facts = profile_from_row(PROFILE_ROW)
    assert facts.provider_id == "ACoAAEd_lPYBmH1En5MdiTMq4m7soEpUXd6QBQ8"
    assert facts.public_identifier == "anjan-b-35a884295"
    assert facts.full_name == "Anjan B"


@pytest.mark.unit
def test_profile_carries_firmographics_unipile_lacks() -> None:
    """Removes the need for a curated large-employer list in the detectors."""
    facts = profile_from_row(PROFILE_ROW)
    assert facts.company_employee_count == 42
    assert facts.company_size_label == "11-50 employees"


@pytest.mark.unit
def test_two_positions_are_reconstructed_from_flat_fields() -> None:
    facts = profile_from_row(PROFILE_ROW)
    assert len(facts.positions) == 2
    assert {p.company for p in facts.positions} == {"TalkingLands", "dan Lab"}


@pytest.mark.unit
def test_positions_have_usable_month_precision() -> None:
    """Better than Unipile, which returns 1/1/YYYY for most entries."""
    facts = profile_from_row(PROFILE_ROW)
    assert all(p.has_usable_start for p in facts.positions)


@pytest.mark.unit
def test_positions_sorted_newest_first_not_by_field_name() -> None:
    """linkedinPrevious* means 'second listed position', NOT 'chronologically
    previous'. This row proves it: both roles say '- Present', and the
    'previous' one actually STARTED EARLIER. Ordering must come from dates."""
    facts = profile_from_row(PROFILE_ROW)
    assert facts.positions[0].start_date == "2026-02-01"   # TalkingLands
    assert facts.positions[1].start_date == "2025-08-01"   # dan Lab
    assert all(p.is_current for p in facts.positions), "both are concurrent"


@pytest.mark.unit
def test_empty_email_becomes_none() -> None:
    assert profile_from_row(PROFILE_ROW).email is None


@pytest.mark.unit
def test_profile_row_without_jobs_yields_no_positions() -> None:
    facts = profile_from_row({"firstName": "X", "profileUrl": "https://x/in/y"})
    assert facts.positions == []


# ----------------------------- inbox mapping ---------------------------------

@pytest.mark.unit
def test_inbound_reply_is_identified_by_linkedin_urls() -> None:
    msg = inbound_from_row(INBOX_REPLY)
    assert msg.is_from_me is False
    assert msg.prospect_provider_id == "ACoAADEv3hgBnfiUSJ1apzGtyJCtRSFQni_zwMU"
    assert msg.body == "Thanks, Zakaur"
    assert msg.thread_id == INBOX_REPLY["threadUrl"]


@pytest.mark.unit
def test_prospect_is_never_taken_from_the_From_fields() -> None:
    """The data-corruption trap. When we sent the last message, firstnameFrom
    is OUR name and the URN is OUR id -- mapping either onto the prospect would
    overwrite their record with our own details."""
    msg = inbound_from_row(INBOX_OURS)
    assert msg.is_from_me is True
    # The prospect, from linkedInUrls -- not the sender URN.
    assert msg.prospect_provider_id == "ACoAADdAiegBgVhsC0ond2h1_meyUCrSz9vkIn0"
    sender = extract_provider_id(INBOX_OURS["lastMessageFromEntityUrn"])
    assert msg.prospect_provider_id != sender


@pytest.mark.unit
def test_external_id_is_deterministic_across_rescrapes() -> None:
    """messages.external_id has a UNIQUE index; re-scraping the same last
    message must collide rather than duplicate."""
    assert inbound_from_row(INBOX_REPLY).external_id == \
        inbound_from_row(dict(INBOX_REPLY)).external_id


@pytest.mark.unit
def test_a_new_reply_in_the_same_thread_gets_a_new_id() -> None:
    """Keying on threadUrl alone would suppress every later reply -- the exact
    bug that would make us miss a prospect's second message."""
    later = {**INBOX_REPLY, "lastMessageDate": "2026-01-01T00:00:00.000Z",
             "message": "Following up"}
    assert inbound_from_row(later).external_id != \
        inbound_from_row(INBOX_REPLY).external_id


@pytest.mark.unit
def test_row_without_thread_url_is_skipped() -> None:
    assert inbound_from_row({"message": "orphan"}) is None


@pytest.mark.unit
@pytest.mark.parametrize("flag,expected", [
    ("TRUE", True), ("true", True), (True, True),
    ("FALSE", False), ("false", False), (False, False), (None, False),
])
def test_is_from_me_parses_csv_and_json_booleans(flag, expected) -> None:
    """CSV yields the strings "TRUE"/"FALSE"; JSON yields real booleans."""
    assert inbound_from_row({**INBOX_REPLY, "isLastMessageFromMe": flag}).is_from_me \
        is expected


# ----------------------------- search mapping --------------------------------

@pytest.mark.unit
def test_prospect_hit_mapping() -> None:
    hit = prospect_hit_from_row(PROFILE_ROW)
    assert hit.linkedin_url == PROFILE_ROW["profileUrl"]
    assert hit.provider_id == "ACoAAEd_lPYBmH1En5MdiTMq4m7soEpUXd6QBQ8"
    assert hit.full_name == "Anjan B"


@pytest.mark.unit
def test_prospect_hit_requires_a_url() -> None:
    assert prospect_hit_from_row({"firstName": "X"}) is None


# ----------------------------- capability gating -----------------------------

@pytest.mark.unit
def test_no_api_key_supports_nothing(monkeypatch) -> None:
    monkeypatch.delenv("PHANTOMBUSTER_API_KEY", raising=False)
    p = PhantomBusterProvider(_Cfg())
    assert not p.supports(Capability.PROFILE)


@pytest.mark.unit
def test_verified_capability_requires_a_configured_agent(monkeypatch) -> None:
    monkeypatch.setenv("PHANTOMBUSTER_API_KEY", "k" * 20)
    monkeypatch.delenv("PHANTOMBUSTER_AGENT_PROFILE_SCRAPER", raising=False)
    assert not PhantomBusterProvider(_Cfg()).supports(Capability.PROFILE)

    monkeypatch.setenv("PHANTOMBUSTER_AGENT_PROFILE_SCRAPER", "123")
    assert PhantomBusterProvider(_Cfg()).supports(Capability.PROFILE)


@pytest.mark.unit
def test_writes_are_enabled_but_reported_as_unverified(monkeypatch) -> None:
    """Changed when Unipile was removed.

    While a fallback existed, gating an unverified capability OFF routed
    around it. With PhantomBuster the only provider, gating it off does not
    route around anything — it just breaks the pipeline. So writes are enabled,
    and the fact that their output has never been inspected is surfaced through
    verification() and the `providers` view instead of by refusing to serve.

    The safety property moved rather than disappeared: an unverified write
    still returns ActionResult(status="unknown") rather than claiming delivery.
    """
    monkeypatch.setenv("PHANTOMBUSTER_API_KEY", "k" * 20)
    monkeypatch.setenv("PHANTOMBUSTER_AGENT_AUTO_CONNECT", "456")
    provider = PhantomBusterProvider(_Cfg())
    assert provider.supports(Capability.CONNECT)
    assert provider.verification(Capability.CONNECT) == "unverified"


@pytest.mark.unit
def test_verified_capabilities_are_reported_as_such(monkeypatch) -> None:
    monkeypatch.setenv("PHANTOMBUSTER_API_KEY", "k" * 20)
    monkeypatch.setenv("PHANTOMBUSTER_AGENT_PROFILE_SCRAPER", "123")
    provider = PhantomBusterProvider(_Cfg())
    assert provider.verification(Capability.PROFILE) == "verified"


@pytest.mark.unit
def test_post_search_has_no_phantombuster_equivalent(monkeypatch) -> None:
    """A real product gap, not an oversight. With no fallback left, the
    capability is simply unroutable and `providers` must say so."""
    monkeypatch.setenv("PHANTOMBUSTER_API_KEY", "k" * 20)
    monkeypatch.setenv("PHANTOMBUSTER_AGENT_SEARCH_EXPORT", "789")
    provider = PhantomBusterProvider(_Cfg())
    assert not provider.supports(Capability.SEARCH_POSTS)
    assert provider.verification(Capability.SEARCH_POSTS) == "unsupported"


@pytest.mark.unit
def test_post_search_reports_unsupported_rather_than_guessing(monkeypatch) -> None:
    """Search Export documents Content/Posts support, but the output shape is
    unverified and PostHit needs the post BODY. Raising beats returning a
    wrong shape to the drafter."""
    from linkedin_agent.providers.base import UnsupportedCapability
    monkeypatch.setenv("PHANTOMBUSTER_API_KEY", "k" * 20)
    with pytest.raises(UnsupportedCapability, match="T-3"):
        PhantomBusterProvider(_Cfg()).search_posts("founder")


# ----------------------------- write semantics -------------------------------

@pytest.mark.unit
def test_write_returns_unknown_not_sent(monkeypatch) -> None:
    """A finished container is not per-recipient delivery confirmation.
    Claiming "sent" would start the follow-up clock for messages that may
    never have arrived."""
    class FakeJobs:
        def run(self, agent, args, timeout=None):
            from linkedin_agent.providers.pb_jobs import JobResult
            return JobResult(container_id="c1", status="finished", rows=[])

        def close(self): pass

    monkeypatch.setenv("PHANTOMBUSTER_API_KEY", "k" * 20)
    monkeypatch.setenv("PHANTOMBUSTER_AGENT_MESSAGE_SENDER", "789")
    p = PhantomBusterProvider(_Cfg(), jobs=FakeJobs())
    result = p.send_dm("https://www.linkedin.com/in/x", "hello")
    assert result.status == "unknown"
    assert result.is_definitive is False
    assert result.provider == "phantombuster"
