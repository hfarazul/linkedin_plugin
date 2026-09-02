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
    is_premium: bool | None = None
    is_open_profile: bool | None = None
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
