"""Capabilities and the normalized objects providers must produce.

The application below the provider seam — signals, evidence, the drafter, the
transition detectors — must never see vendor JSON. It sees the dataclasses in
this module, and nothing else. That is what lets a detector be written once and
work regardless of which vendor supplied the data.

Capabilities are named after what the *application* needs, not after any
vendor's endpoint or Phantom. `SEARCH_POSTS` is "find people by what they
wrote"; it is not "call POST /linkedin/search with category=posts" and it is
not "the LinkedIn Search Export Phantom in advanced setup". Both of those are
implementations of the same capability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Capability(str, Enum):
    """Every LinkedIn operation the application depends on.

    Derived from an audit of actual call sites, not from a vendor's feature
    list: adapters/unipile_adapter.py, enrichment.py, poll.py and daily.py.
    """

    # --- reads ---------------------------------------------------------
    SEARCH_PEOPLE = "search_people"
    SEARCH_POSTS = "search_posts"           # find people by post CONTENT
    PROFILE = "profile"                     # headline, location, counts, flags
    EXPERIENCE = "experience"               # dated position history
    RECENT_POSTS = "recent_posts"           # a person's own recent activity
    INBOX_READ = "inbox_read"               # inbound messages / replies
    ACCEPTANCE_CHECK = "acceptance_check"   # did a sent invite get accepted

    # --- writes --------------------------------------------------------
    # Writes are separated deliberately: the router must never silently retry
    # one on a second provider, because a partially-completed invite or DM is
    # not idempotent and a retry can double-send to a real person.
    REACT = "react"
    CONNECT = "connect"
    SEND_DM = "send_dm"
    WITHDRAW_INVITE = "withdraw_invite"


WRITE_CAPABILITIES = frozenset({
    Capability.REACT,
    Capability.CONNECT,
    Capability.SEND_DM,
    Capability.WITHDRAW_INVITE,
})


def is_write(capability: Capability) -> bool:
    """Writes reach real people and cannot be safely retried elsewhere."""
    return capability in WRITE_CAPABILITIES


# ----------------------------------------------------------------- DTOs


@dataclass
class Position:
    """One job in someone's history, normalized across providers.

    Vendors disagree on shape and precision, and both disagreements matter:

      Unipile   {"company": "Microsoft", "position": "Co-founder",
                 "start": "1/1/1975", "end": null}
                 -- a list of positions, but often year-only dates

      PhantomBuster  linkedinJobTitle / linkedinJobDateRange / linkedinPrevious*
                 -- flat fields, current + one previous only, but month
                    precision ("Feb 2026 - Present")

    `date_precision` exists because of that difference. A detector asking
    "did this start within 180 days?" cannot answer honestly from a year-only
    date, so precision travels with the value instead of being assumed.
    """

    company: str
    title: str | None = None
    company_id: str | None = None
    company_url: str | None = None
    start_date: str | None = None          # ISO-8601, as precise as known
    end_date: str | None = None            # None does NOT imply current
    is_current: bool = False
    date_precision: str = "unknown"        # "day" | "month" | "year" | "unknown"
    location: str | None = None
    description: str | None = None
    source: str = "unknown"                # provider name, for evidence
    raw: dict = field(default_factory=dict)

    @property
    def has_usable_start(self) -> bool:
        """True when the start date supports a recency window.

        Year-only precision deliberately fails this: 'started in 2026' cannot
        distinguish January from December, and a 180-day window built on it
        would be fiction.
        """
        return bool(self.start_date) and self.date_precision in ("day", "month")


# A gap this long between one role ending and the next beginning means the two
# are not consecutive: something happened in between that we cannot see. Three
# months absorbs ordinary notice periods and gardening leave without licensing
# a claim about a move that never occurred.
MAX_DIRECT_TRANSITION_GAP_MONTHS = 3


def _months_between(earlier: str | None, later: str | None) -> int | None:
    """Whole months from `earlier` to `later`, or None if either is unusable."""
    if not earlier or not later:
        return None
    try:
        ey, em = int(earlier[:4]), int(earlier[5:7])
        ly, lm = int(later[:4]), int(later[5:7])
    except (ValueError, IndexError):
        return None
    return (ly - ey) * 12 + (lm - em)


def _same_employer(a: str | None, b: str | None) -> bool:
    """Whether two position rows name the same company.

    Deliberately conservative: when either side is blank we cannot tell, and
    claiming a move we cannot substantiate is the failure this guards against,
    so an unknown counts as "same" and suppresses the claim.
    """
    if not a or not b:
        return True
    return a.strip().casefold() == b.strip().casefold()


def is_direct_transition(previous: "Position", current: "Position") -> bool:
    """True only when `current` plausibly follows `previous` immediately.

    PhantomBuster returns a profile's current role and ONE other, and that other
    is whatever LinkedIn lists second — not necessarily the role immediately
    before. Observed 2026-09-03: a profile returned Group CFO at Tikehau
    (from Oct 2023) alongside Senior Manager at Deloitte (to May 2018), a gap of
    65 months with unknown roles inside it.

    Describing that as "the move from Deloitte to Tikehau" states a transition
    that did not happen, to a real person, at their real address. Personalized
    outreach earns its reply by being right about someone; being confidently
    wrong about their career is worse than sending nothing.

    Requires dates precise enough to trust: a year-only end date cannot
    distinguish a one-month gap from an eleven-month one.
    """
    # An internal promotion is not a move. Observed 2026-09-03: Reservations
    # Supervisor -> Reservations Manager at one employer, consecutive to the
    # month, which rendered as "the move from your time at Royal Adventure
    # Travel & Tourism to Royal Adventure Travel & Tourism". Same-employer
    # steps are a different signal from a company change, and this function
    # exists to license the sentence about a company change.
    if _same_employer(previous.company, current.company):
        return False
    if previous.date_precision not in ("day", "month"):
        return False
    if current.date_precision not in ("day", "month"):
        return False
    if not previous.end_date or not current.start_date:
        # A previous role with no end date is still open — concurrent, not
        # prior — so there is no move to describe.
        return False
    gap = _months_between(previous.end_date, current.start_date)
    if gap is None:
        return False
    # A small negative gap means overlapping roles, which is a normal handover.
    return -1 <= gap <= MAX_DIRECT_TRANSITION_GAP_MONTHS


@dataclass
class ProfileFacts:
    """A profile, normalized. Superset of what enrichment.py stores today,
    plus the firmographics PhantomBuster returns and Unipile does not."""

    provider_id: str | None = None         # ACoAA... — the cross-vendor key
    linkedin_url: str | None = None
    public_identifier: str | None = None
    full_name: str | None = None
    headline: str | None = None
    location: str | None = None
    network_distance: str | None = None
    follower_count: int | None = None
    connections_count: int | None = None
    mutual_connections_count: int | None = None
    is_premium: bool | None = None
    is_open_profile: bool | None = None
    # Unipile supplies these; PhantomBuster does not. They stay None rather
    # than False so a provider switch cannot overwrite a known value with a
    # guess — enrichment only writes fields that are not None.
    is_creator: bool | None = None
    is_influencer: bool | None = None
    is_relationship: bool | None = None
    pronoun: str | None = None
    email: str | None = None               # PhantomBuster Profile Scraper only
    company_name: str | None = None
    company_employee_count: int | None = None   # PhantomBuster only
    company_size_label: str | None = None       # PhantomBuster only
    positions: list[Position] = field(default_factory=list)
    source: str = "unknown"
    raw: dict = field(default_factory=dict)


@dataclass
class InboundMessage:
    """One inbound message, normalized for poll.py.

    `external_id` is what keeps polling idempotent via the UNIQUE index on
    messages.external_id. Unipile supplies a native message id; PhantomBuster's
    Inbox Scraper does not, so its provider synthesises a deterministic one.
    Either way the application sees a stable string and does not care which.
    """

    external_id: str
    prospect_provider_id: str | None       # sender, for prospect lookup
    body: str
    sent_at: str | None = None
    thread_id: str | None = None
    is_from_me: bool = False
    source: str = "unknown"
    raw: dict = field(default_factory=dict)


@dataclass
class ActionResult:
    """Outcome of a write. Deliberately richer than the bare string the current
    adapters return, because the send funnel needs to distinguish "sent",
    "rejected for a known reason", and "we genuinely do not know".

    `status="unknown"` is the important one: PhantomBuster runs asynchronously
    and may finish without telling us per-recipient what happened. An unknown
    write must never be retried on another provider — that is how you
    double-invite somebody.
    """

    status: str                            # "sent" | "failed" | "unknown"
    provider: str
    external_id: str | None = None
    error_code: str | None = None          # e.g. already_invited_recently
    detail: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def is_definitive(self) -> bool:
        return self.status in ("sent", "failed")
