"""PhantomBuster as a CapabilityProvider.

Field names below come from real runs against the live account (agents
5032056022535397 Profile Scraper, 1327575646342095 Inbox Scraper), not from
documentation. Documentation misled this project twice already.

Capability gating is the important safety property here. `supports()` returns
True only for capabilities whose output shape has actually been inspected.
Auto Connect and Message Sender have never been executed, so declaring them
would let an untested Phantom send invitations and DMs to real prospects on the
first cron fire. They stay off until their verification tasks pass, and can be
switched on individually through config rather than by editing this file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime

from ..adapters.base import Post, ProspectHit
from ..config import Config
from .base import CapabilityProvider, MalformedResponse, UnsupportedCapability
from .capabilities import (
    ActionResult,
    Capability,
    InboundMessage,
    Position,
    ProfileFacts,
)
from .pb_jobs import PhantomBusterJobs

logger = logging.getLogger("linkedin.providers.phantombuster")

# What we have actually seen this provider return. Kept separate from what it
# is allowed to serve, because with Unipile removed PhantomBuster is the only
# provider — gating a capability off no longer routes around it, it just breaks
# the pipeline. So everything implemented is enabled, and the distinction lives
# here so `linkedin providers` can keep showing which capabilities rest on
# inspected output and which are still taken on trust.
VERIFIED = frozenset({
    Capability.PROFILE,
    Capability.EXPERIENCE,
    Capability.INBOX_READ,
    Capability.ACCEPTANCE_CHECK,   # connectionDegree is in Profile Scraper output
})

# Implemented and enabled, but the Phantom's real output has never been seen.
# The writes in this set reach real people irreversibly, and a container that
# finishes is not per-recipient delivery confirmation — which is why writes
# return ActionResult(status="unknown") rather than claiming success.
UNVERIFIED = frozenset({
    Capability.SEARCH_PEOPLE,
    Capability.RECENT_POSTS,
    Capability.REACT,
    Capability.CONNECT,
    Capability.SEND_DM,
})

# No PhantomBuster equivalent found for keyword search over post CONTENT.
# Search Export documents a Content/Posts mode behind "advanced setup", but its
# output shape is unverified and PostHit needs the post body. Declared here so
# `providers` reports it as a real gap rather than an oversight.
UNSUPPORTED = frozenset({Capability.SEARCH_POSTS})

SUPPORTED = VERIFIED | UNVERIFIED

# Env var name -> agent id, so agent ids are configuration, not literals.
AGENT_ENV = {
    Capability.PROFILE:        "PHANTOMBUSTER_AGENT_PROFILE_SCRAPER",
    Capability.EXPERIENCE:     "PHANTOMBUSTER_AGENT_PROFILE_SCRAPER",
    Capability.ACCEPTANCE_CHECK: "PHANTOMBUSTER_AGENT_PROFILE_SCRAPER",
    Capability.INBOX_READ:     "PHANTOMBUSTER_AGENT_INBOX_SCRAPER",
    Capability.SEARCH_PEOPLE:  "PHANTOMBUSTER_AGENT_SEARCH_EXPORT",
    Capability.SEARCH_POSTS:   "PHANTOMBUSTER_AGENT_SEARCH_EXPORT",
    Capability.RECENT_POSTS:   "PHANTOMBUSTER_AGENT_ACTIVITY_EXTRACTOR",
    Capability.REACT:          "PHANTOMBUSTER_AGENT_AUTO_LIKER",
    Capability.CONNECT:        "PHANTOMBUSTER_AGENT_AUTO_CONNECT",
    Capability.SEND_DM:        "PHANTOMBUSTER_AGENT_MESSAGE_SENDER",
}

_PROVIDER_ID_RE = re.compile(r"(ACo[A-Za-z0-9_\-]{10,})")

# "Feb 2026 - Present", "Aug 2025 - Jan 2026", "2021 - 2023"
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MONTH_YEAR_RE = re.compile(r"^([A-Za-z]{3,9})\s+(\d{4})$")
_YEAR_RE = re.compile(r"^(\d{4})$")


def extract_provider_id(value: str | None) -> str | None:
    """Pull an ACoAA… id out of a URL or URN.

    Verified against both shapes PhantomBuster emits:
      linkedInUrls           https://www.linkedin.com/in/ACoAADEv3hgB...
      lastMessageFromEntityUrn  urn:li:...fsd_profile:ACoAADEv3hgB...
    """
    if not value:
        return None
    m = _PROVIDER_ID_RE.search(value)
    return m.group(1) if m else None


def parse_pb_date(token: str | None) -> tuple[str | None, str]:
    """Parse one side of a PhantomBuster date range -> (ISO, precision)."""
    if not token:
        return None, "unknown"
    token = token.strip()
    if token.lower() in ("present", "now", "current"):
        return None, "current"
    m = _MONTH_YEAR_RE.match(token)
    if m:
        month = _MONTHS.get(m.group(1)[:3].lower())
        if month:
            return f"{m.group(2)}-{month:02d}-01", "month"
    m = _YEAR_RE.match(token)
    if m:
        return f"{m.group(1)}-01-01", "year"
    return None, "unknown"


def parse_date_range(value: str | None) -> tuple[str | None, str | None, bool, str]:
    """"Feb 2026 - Present" -> (start, end, is_current, precision).

    Month precision here is better than Unipile's, which emits 1/1/YYYY for
    most entries and cannot support a recency window.
    """
    if not value or not isinstance(value, str):
        return None, None, False, "unknown"
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", value.strip(), maxsplit=1)]
    start_raw = parts[0] if parts else None
    end_raw = parts[1] if len(parts) > 1 else None
    start, precision = parse_pb_date(start_raw)
    end, end_precision = parse_pb_date(end_raw)
    is_current = end_precision == "current" or end_raw is None
    return start, end, is_current, precision


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------- normalization


def profile_from_row(row: dict) -> ProfileFacts:
    """Map one LinkedIn Profile Scraper row onto ProfileFacts.

    The scraper returns flat fields — linkedinJobTitle / linkedinJobDateRange
    and linkedinPrevious* — rather than a positions array, so exactly two
    positions can be reconstructed.

    Note `linkedinPrevious*` means "the second listed position", NOT
    "chronologically previous": a profile with two concurrent roles returns
    both as "- Present". Ordering is therefore decided by start date below,
    never by field name.
    """
    positions: list[Position] = []

    for title_key, range_key, company_key, loc_key, desc_key in (
        ("linkedinJobTitle", "linkedinJobDateRange", "linkedinCompanyName",
         "linkedinJobLocation", "linkedinJobDescription"),
        ("linkedinPreviousJobTitle", "linkedinPreviousJobDateRange",
         "previousCompanyName", "linkedinPreviousJobLocation",
         "linkedinPreviousJobDescription"),
    ):
        company = row.get(company_key)
        title = row.get(title_key)
        if not (company or title):
            continue
        start, end, is_current, precision = parse_date_range(row.get(range_key))
        positions.append(Position(
            company=(company or "").strip(),
            title=title,
            company_id=str(row["linkedinCompanyId"]) if row.get("linkedinCompanyId")
            and company_key == "linkedinCompanyName" else None,
            company_url=row.get("linkedinCompanyUrl")
            if company_key == "linkedinCompanyName" else None,
            start_date=start,
            end_date=end,
            is_current=is_current,
            date_precision=precision,
            location=row.get(loc_key),
            description=row.get(desc_key),
            source="phantombuster",
            raw={k: row.get(k) for k in (title_key, range_key, company_key)},
        ))

    # Sort newest-first so callers can reason chronologically. Positions with
    # no parseable start sort last rather than pretending to be recent.
    positions.sort(key=lambda p: p.start_date or "", reverse=True)

    return ProfileFacts(
        provider_id=extract_provider_id(row.get("linkedinProfileUrn")
                                        or row.get("profileUrl")),
        linkedin_url=row.get("profileUrl") or row.get("linkedinProfileUrl"),
        public_identifier=row.get("linkedinProfileSlug"),
        full_name=" ".join(p for p in (row.get("firstName"), row.get("lastName")) if p)
        or None,
        headline=row.get("linkedinHeadline"),
        location=row.get("location"),
        network_distance=row.get("connectionDegree"),
        follower_count=_int(row.get("linkedinFollowersCount")),
        connections_count=_int(row.get("linkedinConnectionsCount")),
        email=row.get("professionalEmail") or None,
        company_name=row.get("linkedinCompanyName") or row.get("companyName"),
        company_employee_count=_int(row.get("linkedinCompanyEmployeesCount")),
        company_size_label=row.get("linkedinCompanySize"),
        positions=positions,
        source="phantombuster",
        raw=row,
    )


def inbound_from_row(row: dict) -> InboundMessage | None:
    """Map one Inbox Scraper row onto InboundMessage.

    Two traps, both verified against the real output:

    1. The prospect is identified by `linkedInUrls`. The `*From` fields
       describe whoever sent the LAST message, which is us whenever
       `isLastMessageFromMe` is true — using them to identify the prospect
       would overwrite records with our own account's details.
    2. There is no native per-message id, so external_id is derived from
       threadUrl + lastMessageDate. threadUrl alone would suppress every
       subsequent reply in the same conversation.
    """
    thread = row.get("threadUrl")
    if not thread:
        return None
    sent_at = row.get("lastMessageDate")
    external = hashlib.sha256(f"{thread}|{sent_at}".encode()).hexdigest()[:32]
    raw_flag = row.get("isLastMessageFromMe")
    is_from_me = str(raw_flag).strip().lower() in ("true", "1", "yes")
    return InboundMessage(
        external_id=external,
        prospect_provider_id=extract_provider_id(row.get("linkedInUrls")),
        body=row.get("message") or "",
        sent_at=sent_at,
        thread_id=thread,
        is_from_me=is_from_me,
        source="phantombuster",
        raw=row,
    )


def _identity_tokens(value: str | None) -> set[str]:
    """Comparable identity tokens from a URL, slug or provider id."""
    if not value:
        return set()
    text = str(value).strip().rstrip("/").lower()
    tokens = {text}
    if "/" in text:
        tokens.add(text.rsplit("/", 1)[-1])
    provider_id = extract_provider_id(str(value))
    if provider_id:
        tokens.add(provider_id.lower())
    return {t for t in tokens if t}


def _match_requested(rows: list, identifier: str) -> ProfileFacts | None:
    """Return the row that actually corresponds to `identifier`, or None.

    Matching is by public slug or ACoAA id — never by position in the result
    list, because a Phantom that ignored our input still returns a well-formed
    row in slot zero.
    """
    wanted = _identity_tokens(identifier)
    if not wanted:
        return None
    for row in rows:
        candidate = _identity_tokens(row.get("linkedinProfileSlug"))
        candidate |= _identity_tokens(row.get("profileUrl"))
        candidate |= _identity_tokens(row.get("linkedinProfileUrl"))
        candidate |= _identity_tokens(row.get("linkedinProfileUrn"))
        if wanted & candidate:
            return profile_from_row(row)
    return None


def prospect_hit_from_row(row: dict) -> ProspectHit | None:
    """Map a Search Export / Profile Scraper row onto the existing ProspectHit
    so discovery call sites keep working unchanged."""
    url = (row.get("profileUrl") or row.get("linkedinProfileUrl")
           or row.get("profileLink"))
    if not url:
        return None
    name = row.get("fullName") or " ".join(
        p for p in (row.get("firstName"), row.get("lastName")) if p) or None
    return ProspectHit(
        linkedin_url=url,
        full_name=name,
        headline=row.get("linkedinHeadline") or row.get("headline") or row.get("job"),
        company=row.get("linkedinCompanyName") or row.get("companyName"),
        title=row.get("linkedinJobTitle") or row.get("jobTitle"),
        location=row.get("location"),
        provider_id=extract_provider_id(row.get("linkedinProfileUrn") or url),
    )


# ------------------------------------------------------------------ provider


class PhantomBusterProvider(CapabilityProvider):
    name = "phantombuster"
    # Every capability runs a container: launch, boot a browser, scrape, write
    # to S3. Seconds to minutes, so callers in a loop must queue rather than
    # block. See linkedin_agent/research_jobs.py.
    is_async = True

    def __init__(self, cfg: Config, *, jobs: PhantomBusterJobs | None = None) -> None:
        self.cfg = cfg
        self.api_key = os.getenv("PHANTOMBUSTER_API_KEY") or None
        self._jobs = jobs
        self._enabled_unverified = {
            c.strip() for c in
            (os.getenv("PHANTOMBUSTER_ENABLE_UNVERIFIED") or "").split(",")
            if c.strip()
        }

    # ------------------------------------------------------------ capability

    def supports(self, capability: Capability) -> bool:
        # Configuration is still a hard requirement: without an API key or an
        # agent id for this capability there is nothing to call, and saying so
        # here produces a clean "no provider" error instead of a failure
        # halfway through an operation.
        if not self.api_key or not self._agent_id(capability):
            return False
        return capability in SUPPORTED

    def verification(self, capability: Capability) -> str:
        """VERIFIED / UNVERIFIED / UNSUPPORTED — surfaced by `providers`."""
        if capability in VERIFIED:
            return "verified"
        if capability in UNVERIFIED:
            return "unverified"
        return "unsupported"

    def _agent_id(self, capability: Capability) -> str | None:
        env = AGENT_ENV.get(capability)
        return os.getenv(env) if env else None

    def _require_agent(self, capability: Capability) -> str:
        agent = self._agent_id(capability)
        if not agent:
            raise UnsupportedCapability(
                f"phantombuster: no agent configured for {capability.value} "
                f"(set {AGENT_ENV.get(capability)})", provider=self.name)
        return agent

    def jobs(self) -> PhantomBusterJobs:
        if self._jobs is None:
            if not self.api_key:
                raise UnsupportedCapability("phantombuster: no API key",
                                            provider=self.name)
            self._jobs = PhantomBusterJobs(self.api_key)
        return self._jobs

    # ----------------------------------------------------------------- reads

    def fetch_profile(self, identifier: str, *,
                      with_experience: bool = False) -> ProfileFacts | None:
        agent = self._require_agent(Capability.PROFILE)
        url = identifier if identifier.startswith("http") else \
            f"https://www.linkedin.com/in/{identifier}"
        result = self.jobs().run(agent, {"spreadsheetUrl": url})
        if not result.rows:
            return None

        # Identity guard. A Phantom runs with SAVED arguments; an override we
        # send is merged, not authoritative, and an argument the Phantom does
        # not recognise is ignored silently. It then scrapes whatever it was
        # configured with and writes to the same result file — so a request for
        # one person can return another, with every downstream stage reporting
        # success on the wrong human.
        #
        # Observed on 2026-09-03: asking for emmanuelle-habert-1016b0162
        # returned Anjan B, the agent's previously configured target. Nothing
        # in the pipeline noticed, because nothing was comparing.
        facts = _match_requested(result.rows, identifier)
        if facts is None:
            returned = ", ".join(
                str(r.get("linkedinProfileSlug") or r.get("profileUrl") or "?")
                for r in result.rows[:3])
            raise MalformedResponse(
                f"asked for {identifier!r} but the Phantom returned {returned!r}. "
                f"The agent ran with its own saved input rather than ours — "
                f"check that the Profile Scraper accepts a per-run profile URL, "
                f"and that its saved argument is not pinned to a fixed list.",
                provider=self.name)
        return facts

    def check_acceptance(self, identifier: str) -> bool | None:
        facts = self.fetch_profile(identifier)
        if facts is None or not facts.network_distance:
            return None
        # Profile Scraper reports connectionDegree as "1st" / "2nd" / "3rd".
        return str(facts.network_distance).strip().lower().startswith("1")

    def fetch_inbox(self, limit: int = 50) -> list[InboundMessage]:
        agent = self._require_agent(Capability.INBOX_READ)
        result = self.jobs().run(agent, {"numberOfThreadsToScrape": limit})
        out = []
        for row in result.rows:
            msg = inbound_from_row(row)
            if msg is not None:
                out.append(msg)
        return out

    def search_people(self, query: str, limit: int = 20) -> list:
        agent = self._require_agent(Capability.SEARCH_PEOPLE)
        result = self.jobs().run(agent, {"search": query,
                                         "numberOfResultsPerSearch": limit})
        hits = [prospect_hit_from_row(r) for r in result.rows]
        return [h for h in hits if h is not None][:limit]

    def search_posts(self, keywords: str, *, limit: int = 20,
                     date_posted: str = "past_month",
                     author_keywords: str | None = None) -> list:
        # Search Export supports Content/Posts through "advanced setup". The
        # output shape is unverified (task T-3): the documented fields are
        # profile-shaped, and PostHit needs the post BODY as well. Raising
        # rather than guessing keeps a wrong shape from reaching the drafter.
        raise UnsupportedCapability(
            "phantombuster: post/content search not yet verified (task T-3)",
            provider=self.name)

    def get_recent_posts(self, linkedin_url: str, limit: int = 5) -> list[Post]:
        agent = self._require_agent(Capability.RECENT_POSTS)
        result = self.jobs().run(agent, {"profileUrls": linkedin_url,
                                         "numberMaxOfPosts": limit})
        posts: list[Post] = []
        for row in result.rows[:limit]:
            post_url = row.get("postUrl") or row.get("url")
            if not post_url:
                continue
            posts.append(Post(
                post_id=str(row.get("postId") or post_url),
                url=post_url,
                author_url=linkedin_url,
                text=(row.get("postContent") or row.get("text") or "")[:1000],
                posted_at=row.get("postDate") or row.get("date"),
            ))
        return posts

    # ---------------------------------------------------------------- writes

    def _write(self, capability: Capability, arguments: dict) -> ActionResult:
        """Shared write path.

        PhantomBuster reports container-level success, not per-recipient
        outcome. Until tasks T-7/T-8 establish what a failure actually looks
        like, a finished container yields status="unknown" rather than "sent" —
        claiming delivery we cannot see would start the follow-up clock for
        messages that may never have arrived.
        """
        agent = self._require_agent(capability)
        result = self.jobs().run(agent, arguments)
        return ActionResult(
            status="unknown",
            provider=self.name,
            external_id=result.container_id,
            detail="container finished; per-recipient outcome unverified (T-7/T-8)",
            raw={"rows": result.rows[:5]},
        )

    def react(self, post, reaction: str = "LIKE") -> ActionResult:
        url = getattr(post, "url", None) or getattr(post, "post_id", None)
        return self._write(Capability.REACT, {"postUrl": url})

    def send_connection(self, linkedin_url: str,
                        note: str | None = None) -> ActionResult:
        args: dict = {"spreadsheetUrl": linkedin_url}
        if note:
            args["message"] = note[:300]
        return self._write(Capability.CONNECT, args)

    def send_dm(self, linkedin_url: str, body: str) -> ActionResult:
        return self._write(Capability.SEND_DM,
                           {"spreadsheetUrl": linkedin_url, "message": body})

    def close(self) -> None:
        if self._jobs is not None:
            self._jobs.close()
            self._jobs = None
