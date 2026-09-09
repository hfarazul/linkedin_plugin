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
def test_unverified_writes_stay_disarmed_until_opted_in(monkeypatch) -> None:
    """Configuring a Phantom must not be the same act as arming it.

    An earlier revision enabled every implemented capability, on the reasoning
    that with no fallback left, gating one off does not route around it — it
    just breaks the pipeline. That reasoning holds for reads and fails for
    writes: a read that comes back wrong raises, while a write reaches a real
    person and cannot be taken back. PHANTOMBUSTER_ENABLE_UNVERIFIED was parsed
    for exactly this and then never read, so setting an agent id armed an
    uninspected Phantom on the next cron fire.

    The capability is still reported as implemented-but-unverified; it simply
    will not serve until named.
    """
    monkeypatch.setenv("PHANTOMBUSTER_API_KEY", "k" * 20)
    monkeypatch.setenv("PHANTOMBUSTER_AGENT_AUTO_CONNECT", "456")
    monkeypatch.delenv("PHANTOMBUSTER_ENABLE_UNVERIFIED", raising=False)

    provider = PhantomBusterProvider(_Cfg())
    assert not provider.supports(Capability.CONNECT)
    assert provider.requires_opt_in(Capability.CONNECT)
    assert provider.verification(Capability.CONNECT) == "unverified"

    monkeypatch.setenv("PHANTOMBUSTER_ENABLE_UNVERIFIED", "connect")
    opted_in = PhantomBusterProvider(_Cfg())
    assert opted_in.supports(Capability.CONNECT)
    assert opted_in.verification(Capability.CONNECT) == "unverified"


@pytest.mark.unit
def test_opting_one_write_in_does_not_arm_the_others(monkeypatch) -> None:
    """The flag is per capability. Enabling `connect` after its verification
    task passes must not also enable DMs, whose Phantom is still uninspected."""
    monkeypatch.setenv("PHANTOMBUSTER_API_KEY", "k" * 20)
    monkeypatch.setenv("PHANTOMBUSTER_AGENT_AUTO_CONNECT", "456")
    monkeypatch.setenv("PHANTOMBUSTER_AGENT_MESSAGE_SENDER", "789")
    monkeypatch.setenv("PHANTOMBUSTER_ENABLE_UNVERIFIED", "connect")

    provider = PhantomBusterProvider(_Cfg())
    assert provider.supports(Capability.CONNECT)
    assert not provider.supports(Capability.SEND_DM)


@pytest.mark.unit
def test_unverified_reads_need_no_opt_in(monkeypatch) -> None:
    """Reads keep the old behaviour. Gating one off routes around nothing now
    that PhantomBuster is the only provider, and a malformed read raises rather
    than reaching anybody — so the cost is a broken pipeline for no safety."""
    monkeypatch.setenv("PHANTOMBUSTER_API_KEY", "k" * 20)
    monkeypatch.setenv("PHANTOMBUSTER_AGENT_ACTIVITY_EXTRACTOR", "321")
    monkeypatch.delenv("PHANTOMBUSTER_ENABLE_UNVERIFIED", raising=False)

    provider = PhantomBusterProvider(_Cfg())
    assert provider.supports(Capability.RECENT_POSTS)
    assert not provider.requires_opt_in(Capability.RECENT_POSTS)
    assert provider.verification(Capability.RECENT_POSTS) == "unverified"


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


# ===== identity guard =======================================================
#
# A Phantom runs with its SAVED arguments. An override we send is merged, not
# authoritative, and an argument the Phantom does not recognise is dropped
# silently — it then scrapes whatever it was configured with and writes to the
# same result file. Observed 2026-09-03: a request for
# emmanuelle-habert-1016b0162 returned Anjan B, and every downstream stage
# reported success on the wrong human.

from linkedin_agent.providers.phantombuster import _match_requested


@pytest.mark.unit
def test_matches_by_slug() -> None:
    facts = _match_requested([PROFILE_ROW], "anjan-b-35a884295")
    assert facts is not None and facts.full_name == "Anjan B"


@pytest.mark.unit
def test_matches_by_full_url() -> None:
    assert _match_requested(
        [PROFILE_ROW], "https://www.linkedin.com/in/anjan-b-35a884295/") is not None


@pytest.mark.unit
def test_matches_by_provider_id() -> None:
    assert _match_requested(
        [PROFILE_ROW], "ACoAAEd_lPYBmH1En5MdiTMq4m7soEpUXd6QBQ8") is not None


@pytest.mark.unit
def test_wrong_person_is_not_matched() -> None:
    """The actual production bug: a well-formed row for somebody else."""
    assert _match_requested([PROFILE_ROW], "emmanuelle-habert-1016b0162") is None


@pytest.mark.unit
def test_never_matches_by_position() -> None:
    """Slot zero is always populated even when our input was ignored, so
    position must never stand in for identity."""
    other = {**PROFILE_ROW, "linkedinProfileSlug": "someone-else",
             "profileUrl": "https://www.linkedin.com/in/someone-else",
             "linkedinProfileUrn": "ACoOTHER000000000000"}
    assert _match_requested([other], "anjan-b-35a884295") is None


@pytest.mark.unit
def test_finds_the_right_row_in_a_multi_row_result() -> None:
    other = {**PROFILE_ROW, "linkedinProfileSlug": "someone-else",
             "profileUrl": "https://www.linkedin.com/in/someone-else",
             "linkedinProfileUrn": "ACoOTHER000000000000", "firstName": "Someone"}
    facts = _match_requested([other, PROFILE_ROW], "anjan-b-35a884295")
    assert facts is not None and facts.full_name == "Anjan B"


@pytest.mark.unit
def test_fetch_profile_raises_rather_than_returning_the_wrong_person(monkeypatch) -> None:
    from linkedin_agent.providers.base import MalformedResponse
    from linkedin_agent.providers.pb_jobs import JobResult

    class FakeJobs:
        def run(self, agent, args, timeout=None):
            return JobResult(container_id="c1", status="finished",
                             rows=[PROFILE_ROW])   # the WRONG person

        def close(self): pass

    monkeypatch.setenv("PHANTOMBUSTER_API_KEY", "k" * 20)
    monkeypatch.setenv("PHANTOMBUSTER_AGENT_PROFILE_SCRAPER", "123")
    provider = PhantomBusterProvider(_Cfg(), jobs=FakeJobs())
    with pytest.raises(MalformedResponse, match="asked for"):
        provider.fetch_profile("emmanuelle-habert-1016b0162")


# ===== direct-transition guard ==============================================
#
# PhantomBuster returns a profile's current role and ONE other, and that other
# is whatever LinkedIn lists second — not necessarily the role immediately
# before. Observed 2026-09-03 on a real profile: Group CFO at Tikehau from
# Oct 2023 alongside Senior Manager at Deloitte to May 2018, a 65-month gap
# with unknown roles inside it. Calling that "the move from Deloitte to
# Tikehau" states a transition that did not happen, to a real person.

from linkedin_agent.providers.capabilities import (
    Position, is_direct_transition)


def _pos(company, start=None, end=None, precision="month", **kw):
    return Position(company=company, start_date=start, end_date=end,
                    date_precision=precision, **kw)


@pytest.mark.unit
def test_consecutive_roles_are_a_direct_transition() -> None:
    prev = _pos("BDO Luxembourg", "2008-01-01", "2024-05-01")
    cur = _pos("ARCHIMED", "2024-05-01", None, is_current=True)
    assert is_direct_transition(prev, cur) is True


@pytest.mark.unit
def test_the_tikehau_case_is_rejected() -> None:
    """The real profile that exposed this."""
    prev = _pos("Deloitte", "2015-06-01", "2018-05-01")
    cur = _pos("Tikehau Capital", "2023-10-01", None, is_current=True)
    assert is_direct_transition(prev, cur) is False


@pytest.mark.unit
@pytest.mark.parametrize("gap_end,expected", [
    ("2024-05-01", True),    # same month
    ("2024-03-01", True),    # 2-month gap: notice period
    ("2024-02-01", True),    # 3-month gap: boundary, allowed
    ("2024-01-01", False),   # 4-month gap: too long to assert a move
    ("2023-05-01", False),   # a year
])
def test_gap_tolerance(gap_end, expected) -> None:
    prev = _pos("Old Co", "2015-01-01", gap_end)
    cur = _pos("New Co", "2024-05-01", None, is_current=True)
    assert is_direct_transition(prev, cur) is expected


@pytest.mark.unit
def test_overlapping_handover_still_counts() -> None:
    """Starting a month before formally leaving is a normal handover."""
    prev = _pos("Old Co", "2015-01-01", "2024-06-01")
    cur = _pos("New Co", "2024-05-01", None, is_current=True)
    assert is_direct_transition(prev, cur) is True


@pytest.mark.unit
def test_year_precision_cannot_establish_a_direct_move() -> None:
    """A year-only end date cannot distinguish a one-month gap from eleven."""
    prev = _pos("Old Co", "2023-01-01", "2024-01-01", precision="year")
    cur = _pos("New Co", "2024-05-01", None, is_current=True, precision="month")
    assert is_direct_transition(prev, cur) is False


@pytest.mark.unit
def test_open_ended_previous_role_is_concurrent_not_prior() -> None:
    """Two roles both marked current are held at the same time — there is no
    move to describe. The Anjan profile is exactly this shape."""
    prev = _pos("dan Lab", "2025-08-01", None, is_current=True)
    cur = _pos("TalkingLands", "2026-02-01", None, is_current=True)
    assert is_direct_transition(prev, cur) is False


@pytest.mark.unit
def test_missing_dates_are_never_a_direct_transition() -> None:
    assert is_direct_transition(_pos("A"), _pos("B", "2024-01-01")) is False
    assert is_direct_transition(_pos("A", "2020-01-01", "2022-01-01"),
                                _pos("B")) is False


# ===== activity extractor: authorship ======================================
#
# The Activity Extractor returns reposts alongside original posts. In a sample
# of 20 real rows, 3 were reposts of other people's content, marked
# action="<Name> reposted this" with authorUrl pointing at the original author.
# The drafter treats these as "something you wrote", so quoting a repost back
# attributes another person's words to the prospect.

OWN_POST_ROW = {          # verbatim shape from agent 5153701994827074
    "action": "Post",
    "authorUrl": "https://www.linkedin.com/in/iamghazi",
    "profileUrl": "https://www.linkedin.com/in/iamghazi/",
    "postContent": "My latest workflow for building a side project with AI",
    "postUrl": "https://www.linkedin.com/feed/update/urn:li:activity:749170470",
    "postDate": "3w",
    "postTimestamp": "2026-08-08T03:59:51.210Z",
    "type": "Text",
}
REPOST_ROW = {
    "action": "Ghazi Sultan reposted this",
    "authorUrl": "https://www.linkedin.com/in/amjadmasad",     # someone else
    "profileUrl": "https://www.linkedin.com/in/iamghazi/",
    "postContent": "I was a pro gamer before I was a founder.",
    "postUrl": "https://www.linkedin.com/feed/update/urn:li:activity:749170471",
    "postTimestamp": "2026-08-01T00:00:00.000Z",
    "type": "Image",
}


class _ActivityJobs:
    def __init__(self, rows):
        self._rows = rows
        self.saved = None

    def run(self, agent, args, timeout=None):
        from linkedin_agent.providers.pb_jobs import JobResult
        self.saved = args
        return JobResult(container_id="c1", status="finished", rows=self._rows)

    def close(self): pass


def _activity_provider(monkeypatch, rows):
    monkeypatch.setenv("PHANTOMBUSTER_API_KEY", "k" * 20)
    monkeypatch.setenv("PHANTOMBUSTER_AGENT_ACTIVITY_EXTRACTOR", "555")
    jobs = _ActivityJobs(rows)
    return PhantomBusterProvider(_Cfg(), jobs=jobs), jobs


@pytest.mark.unit
def test_reposts_are_excluded_by_default(monkeypatch) -> None:
    """The important one: another person's words must not be handed to the
    drafter as the prospect's own."""
    provider, _ = _activity_provider(monkeypatch, [REPOST_ROW, OWN_POST_ROW])
    posts = provider.get_recent_posts("https://www.linkedin.com/in/iamghazi/", 5)
    assert len(posts) == 1
    assert posts[0].text.startswith("My latest workflow")


@pytest.mark.unit
def test_reposts_can_be_requested_explicitly(monkeypatch) -> None:
    provider, _ = _activity_provider(monkeypatch, [REPOST_ROW, OWN_POST_ROW])
    posts = provider.get_recent_posts("https://www.linkedin.com/in/iamghazi/", 5,
                                      include_reposts=True)
    assert len(posts) == 2


@pytest.mark.unit
def test_author_url_is_the_real_author_not_the_prospect(monkeypatch) -> None:
    """So a caller can always tell whose words these are."""
    provider, _ = _activity_provider(monkeypatch, [REPOST_ROW])
    posts = provider.get_recent_posts("https://www.linkedin.com/in/iamghazi/", 5,
                                      include_reposts=True)
    assert posts[0].author_url == "https://www.linkedin.com/in/amjadmasad"


@pytest.mark.unit
def test_uses_the_iso_timestamp_not_the_relative_date(monkeypatch) -> None:
    """postDate is '3w'; only postTimestamp can be compared or stored."""
    provider, _ = _activity_provider(monkeypatch, [OWN_POST_ROW])
    posts = provider.get_recent_posts("https://www.linkedin.com/in/iamghazi/", 5)
    assert posts[0].posted_at == "2026-08-08T03:59:51.210Z"


@pytest.mark.unit
def test_targets_the_profile_with_the_verified_argument_name(monkeypatch) -> None:
    """The Extractor takes spreadsheetUrl, not profileUrls. An unrecognised
    key would be merged into the saved argument and silently ignored, and the
    Phantom would scrape whoever it was last pointed at."""
    provider, jobs = _activity_provider(monkeypatch, [OWN_POST_ROW])
    provider.get_recent_posts("https://www.linkedin.com/in/iamghazi/", 3)
    assert jobs.saved["spreadsheetUrl"] == "https://www.linkedin.com/in/iamghazi/"
    assert "profileUrls" not in jobs.saved


@pytest.mark.unit
def test_rows_without_a_post_url_are_skipped(monkeypatch) -> None:
    provider, _ = _activity_provider(monkeypatch, [{"action": "Post",
                                                    "postContent": "orphan"}])
    assert provider.get_recent_posts("https://www.linkedin.com/in/iamghazi/", 5) == []


# ---------------------- dedup: activity served from the CSV ------------------
# The Activity Extractor deduplicates like the Profile Scraper. Observed live
# on 2026-09-03 for iamghazi: the container finished in seconds having logged
# "No new activity found", result.json came back with no rows for the profile,
# and the 20 posts it had collected on an earlier run were only in result.csv.
# Without a fallback the drafter loses every "something you wrote" hook the
# second time a profile is touched.

class _DedupActivityJobs(_ActivityJobs):
    """result.json has nothing for us; the cumulative CSV does."""

    def __init__(self, fresh_rows, cumulative_rows):
        super().__init__(fresh_rows)
        self._cumulative = cumulative_rows
        self.fetch_all_calls = 0

    def fetch_rows(self, agent):
        return self._rows

    def fetch_all_rows(self, agent):
        self.fetch_all_calls += 1
        return self._cumulative


def _dedup_provider(monkeypatch, fresh, cumulative):
    monkeypatch.setenv("PHANTOMBUSTER_API_KEY", "k" * 20)
    monkeypatch.setenv("PHANTOMBUSTER_AGENT_ACTIVITY_EXTRACTOR", "555")
    monkeypatch.setattr("linkedin_agent.providers.phantombuster.time.sleep",
                        lambda _s: None)
    jobs = _DedupActivityJobs(fresh, cumulative)
    return PhantomBusterProvider(_Cfg(), jobs=jobs), jobs


@pytest.mark.unit
def test_empty_result_falls_back_to_the_cumulative_csv(monkeypatch) -> None:
    provider, jobs = _dedup_provider(monkeypatch, [], [OWN_POST_ROW])
    posts = provider.get_recent_posts("https://www.linkedin.com/in/iamghazi/", 5)
    assert jobs.fetch_all_calls == 1
    assert len(posts) == 1
    assert posts[0].text.startswith("My latest workflow")


@pytest.mark.unit
def test_cumulative_csv_does_not_leak_another_prospects_posts(monkeypatch) -> None:
    """The CSV accrues every profile the agent has ever scraped, and each of
    those rows is 'own-authored' relative to its own profileUrl. Returning them
    would attribute a stranger's post to this prospect."""
    other = {
        "profileUrl": "https://www.linkedin.com/in/someone-else/",
        "authorUrl": "https://www.linkedin.com/in/someone-else",
        "action": "Post",
        "postContent": "A different person's post",
        "postUrl": "https://www.linkedin.com/feed/update/urn:li:activity:111",
        "postTimestamp": "2026-08-02T00:00:00.000Z",
    }
    provider, _ = _dedup_provider(monkeypatch, [], [other, OWN_POST_ROW])
    posts = provider.get_recent_posts("https://www.linkedin.com/in/iamghazi/", 5)
    assert [p.text for p in posts] == [OWN_POST_ROW["postContent"][:1000]]


@pytest.mark.unit
def test_no_fallback_when_the_fresh_scrape_already_has_our_rows(monkeypatch) -> None:
    """A cache read costs an extra S3 fetch and returns older data; only take
    it when the fresh result genuinely has nothing for this profile."""
    provider, jobs = _dedup_provider(monkeypatch, [OWN_POST_ROW], [REPOST_ROW])
    provider.get_recent_posts("https://www.linkedin.com/in/iamghazi/", 5)
    assert jobs.fetch_all_calls == 0


# ------------------------- company name fallback -----------------------------
# Observed live for iamghazi: linkedinCompanyName came back "" while
# companyName held "Pawp". The empty value reached the email as
# "the work you are doing at ." and a subject line of " - shipping without
# hiring a team".

@pytest.mark.unit
def test_blank_linkedin_company_name_falls_back_to_company_name() -> None:
    facts = profile_from_row({
        "linkedinJobTitle": "Founder",
        "linkedinJobDateRange": "Feb 2026 - Present",
        "linkedinCompanyName": "",
        "companyName": "Pawp",
    })
    assert facts.positions[0].company == "Pawp"


@pytest.mark.unit
def test_company_slug_is_the_last_resort_not_a_guess() -> None:
    facts = profile_from_row({
        "linkedinJobTitle": "Founder",
        "linkedinJobDateRange": "Feb 2026 - Present",
        "linkedinCompanyName": "",
        "linkedinCompanySlug": "pawp",
    })
    assert facts.positions[0].company == "pawp"


@pytest.mark.unit
def test_a_populated_company_name_is_never_overridden() -> None:
    facts = profile_from_row({
        "linkedinJobTitle": "Founder",
        "linkedinJobDateRange": "Feb 2026 - Present",
        "linkedinCompanyName": "Pawp Inc",
        "companyName": "Pawp",
    })
    assert facts.positions[0].company == "Pawp Inc"


@pytest.mark.unit
def test_the_no_new_results_marker_is_not_mistaken_for_a_scrape(monkeypatch) -> None:
    """Live result.json for an already-seen profile, 2026-09-03. It names the
    right person, so a profile-only check reads it as a successful scrape and
    returns nothing while the 20 real posts sit in the CSV."""
    marker = {"profileUrl": "https://www.linkedin.com/in/iamghazi/",
              "postUrl": "", "error": "No new results found",
              "timestamp": "2026-09-03T11:33:36.343Z"}
    provider, jobs = _dedup_provider(monkeypatch, [marker], [OWN_POST_ROW])
    posts = provider.get_recent_posts("https://www.linkedin.com/in/iamghazi/", 5)
    assert jobs.fetch_all_calls == 1
    assert len(posts) == 1


@pytest.mark.unit
def test_the_marker_short_circuits_the_settle_wait(monkeypatch) -> None:
    """The marker is the agent's final answer. Re-reading result.json five
    times at four seconds apart cannot change it, and every profile in a batch
    would pay that."""
    marker = {"profileUrl": "https://www.linkedin.com/in/iamghazi/",
              "postUrl": "", "error": "No new results found"}
    provider, jobs = _dedup_provider(monkeypatch, [marker], [OWN_POST_ROW])
    calls = []
    monkeypatch.setattr(jobs, "fetch_rows",
                        lambda a: (calls.append(a), [marker])[1])
    provider.get_recent_posts("https://www.linkedin.com/in/iamghazi/", 5)
    assert calls == []


# ------------------------- internal promotions -------------------------------
# Observed 2026-09-03 on a real profile: Reservations Supervisor -> Reservations
# Manager at one employer, consecutive to the month. The dates said "direct
# transition", so the email rendered "the move from your time at Royal Adventure
# Travel & Tourism to Royal Adventure Travel & Tourism". A promotion is not a
# company move, and this helper exists to license the sentence about one.

@pytest.mark.unit
def test_a_promotion_at_one_employer_is_not_a_move() -> None:
    prev = _pos("Royal Adventure Travel & Tourism", "2018-10-01", "2023-03-01")
    cur = _pos("Royal Adventure Travel & Tourism", "2023-03-01", "2025-06-01")
    assert is_direct_transition(prev, cur) is False


@pytest.mark.unit
def test_employer_comparison_ignores_case_and_padding() -> None:
    prev = _pos("  acme labs ", "2022-01-01", "2023-01-01")
    cur = _pos("ACME Labs", "2023-02-01", None, is_current=True)
    assert is_direct_transition(prev, cur) is False


@pytest.mark.unit
def test_a_missing_company_name_suppresses_the_claim() -> None:
    """We cannot tell whether it is a move, so we do not assert one."""
    prev = _pos("", "2022-01-01", "2023-01-01")
    cur = _pos("Acme", "2023-02-01", None, is_current=True)
    assert is_direct_transition(prev, cur) is False


@pytest.mark.unit
def test_a_current_role_outranks_an_ended_one_with_a_readable_date() -> None:
    """positions[0] is "where they work now" to everything downstream.

    Sorting on start_date alone put a current role whose date range did not
    parse behind an ended job that happened to have a readable start, so
    positions[0] was a former employer. build_evidence then states that as a
    VERIFIED_FACT and the email tells a stranger they work somewhere they left.

    An unparseable date is missing information about a job we know is current.
    It is not evidence that the job is old.
    """
    from linkedin_agent.providers.phantombuster import profile_from_row

    row = dict(PROFILE_ROW)
    row["linkedinJobTitle"] = "Chief Executive Officer"
    row["linkedinCompanyName"] = "Currently Here"
    row["linkedinJobDateRange"] = "Present"          # no parseable start
    row["linkedinPreviousJobTitle"] = "Analyst"
    row["previousCompanyName"] = "Left In 2019"
    row["linkedinPreviousJobDateRange"] = "Jan 2015 - Mar 2019"

    facts = profile_from_row(row)

    assert facts.positions[0].company == "Currently Here"
    assert facts.positions[0].is_current
    assert facts.positions[1].company == "Left In 2019"


@pytest.mark.unit
def test_two_current_roles_still_order_by_start_date() -> None:
    """Within the current group, newest first — the previous behaviour."""
    from linkedin_agent.providers.phantombuster import profile_from_row

    row = dict(PROFILE_ROW)
    row["linkedinCompanyName"] = "Older Current"
    row["linkedinJobDateRange"] = "Feb 2020 - Present"
    row["previousCompanyName"] = "Newer Current"
    row["linkedinPreviousJobDateRange"] = "Aug 2025 - Present"

    facts = profile_from_row(row)

    assert [p.company for p in facts.positions] == ["Newer Current", "Older Current"]
    assert all(p.is_current for p in facts.positions)


@pytest.mark.unit
def test_two_ended_roles_still_order_by_start_date() -> None:
    """And within the ended group, unchanged as well — no current role to lead."""
    from linkedin_agent.providers.phantombuster import profile_from_row

    row = dict(PROFILE_ROW)
    row["linkedinCompanyName"] = "Older Job"
    row["linkedinJobDateRange"] = "Jan 2012 - Jan 2015"
    row["previousCompanyName"] = "Newer Job"
    row["linkedinPreviousJobDateRange"] = "Feb 2018 - Mar 2021"

    facts = profile_from_row(row)

    assert [p.company for p in facts.positions] == ["Newer Job", "Older Job"]
    assert not any(p.is_current for p in facts.positions)
