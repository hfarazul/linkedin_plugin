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
import time
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
    is_write,
)
from .pb_jobs import PhantomBusterJobs

logger = logging.getLogger("linkedin.providers.phantombuster")

# What we have actually seen this provider return. Kept separate from what it
# is allowed to serve, because with Unipile removed PhantomBuster is the only
# provider — gating a READ off no longer routes around it, it just breaks the
# pipeline, so unverified reads are enabled and `linkedin providers` shows
# which of them rest on inspected output.
#
# Unverified WRITES are gated: see supports(). They reach real people and
# cannot be taken back, so they stay disarmed until named in
# PHANTOMBUSTER_ENABLE_UNVERIFIED.
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

# A finished container does not guarantee its result.json has landed in S3,
# and that file is agent-scoped, so an early read serves the previous run's
# rows. These bound how long we wait for the write to settle before treating
# a mismatch as a genuine wrong-target failure.
RESULT_SETTLE_ATTEMPTS = 5
RESULT_SETTLE_SECONDS = 4

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
        if not company and company_key == "linkedinCompanyName":
            # Observed blank on a real profile while companyName held "Pawp".
            # A blank here renders as "the work you are doing at ." in an
            # email, so fall back through every field that names the employer.
            company = (row.get("companyName")
                       or row.get("linkedinCompanySlug") or "")
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

    # Current roles first, then newest-first within each group. Positions with
    # no parseable start sort last of their group rather than pretending to be
    # recent.
    #
    # `is_current` has to lead. Sorting on start_date alone put a current role
    # whose date range did not parse ("- Present", a localised month, a blank)
    # behind an ended job that happened to have a readable start, so
    # positions[0] was a former employer. Everything downstream treats
    # positions[0] as where the person works now: build_evidence states it as
    # a VERIFIED_FACT, and the email then tells a stranger they work somewhere
    # they left. An unparseable date is missing information about a job we
    # know is current; it is not evidence that the job is old.
    positions.sort(key=lambda p: (p.is_current, p.start_date or ""), reverse=True)

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
        if capability not in SUPPORTED:
            return False
        # Unverified READS stay on. With Unipile gone there is nothing to route
        # around to, so gating a read off does not make the system safer — it
        # just breaks the pipeline, and a read that returns the wrong shape
        # raises MalformedResponse rather than reaching anybody.
        #
        # Unverified WRITES are the opposite case, and the reason this flag
        # exists. React, connect and send_dm reach real people irreversibly
        # through Phantoms whose output has never been inspected, and a
        # container that finishes is not per-recipient confirmation. Setting an
        # agent id is how you configure one; it should not also be how you arm
        # one. Until its verification task passes, an unverified write requires
        # naming the capability in PHANTOMBUSTER_ENABLE_UNVERIFIED — which is
        # what .env.example and the `providers` hint have always claimed.
        if capability in UNVERIFIED and is_write(capability):
            return capability.value in self._enabled_unverified
        return True

    def requires_opt_in(self, capability: Capability) -> bool:
        """True when this capability is gated behind the opt-in flag.

        Lets `providers` distinguish "no agent id configured" from "configured
        but deliberately disarmed", which are different problems with different
        fixes.
        """
        return capability in UNVERIFIED and is_write(capability)

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

        # A container can report "finished" before its result.json has landed
        # in S3, and that file is agent-scoped — so an early read returns the
        # PREVIOUS run's rows, for whoever was scraped last. Re-read a few
        # times before believing it. Observed 2026-09-03: asking for
        # 'iamghazi' returned 'vincent-picot-555744a', the prior target.
        facts = _match_requested(result.rows, identifier) if result.rows else None
        for _ in range(RESULT_SETTLE_ATTEMPTS):
            if facts is not None:
                break
            time.sleep(RESULT_SETTLE_SECONDS)
            try:
                rows = self.jobs().fetch_rows(agent)
            except Exception:
                break
            if rows:
                result.rows = rows
                facts = _match_requested(rows, identifier)

        if facts is None:
            # The Phantom deduplicates: if it has processed this profile
            # before it exits in seconds with "All leads have been processed",
            # scrapes nothing, and leaves the previous run's result.json. The
            # row is still in the cumulative CSV, so a skip becomes a cache
            # hit instead of a failure.
            try:
                cached = self.jobs().fetch_all_rows(agent)
            except Exception:
                cached = []
            facts = _match_requested(cached, identifier) if cached else None
            if facts is not None:
                logger.info("profile %s served from the agent's cumulative "
                            "results; the Phantom skipped it as already "
                            "processed, so this data may be stale",
                            identifier)

        if not result.rows and facts is None:
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
        #
        # `facts` is whatever the fresh result, the settle retries, or the
        # cumulative CSV produced above — all three are legitimate sources, and
        # only a miss in every one of them is a wrong-target failure.
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

    def get_recent_posts(self, linkedin_url: str, limit: int = 5,
                         *, include_reposts: bool = False) -> list[Post]:
        """The prospect's recent activity, filtered to what they actually wrote.

        The Activity Extractor returns reposts alongside original posts. In a
        sample of 20 rows, 3 were reposts of other people's content, marked
        `action = "<Name> reposted this"` with `authorUrl` pointing at the
        original author rather than the scraped profile.

        Those are excluded by default. The drafter uses these as "something you
        wrote" — quoting a repost back as the prospect's own thinking
        attributes someone else's words to them, which is the same failure as
        claiming a career move that never happened.
        """
        agent = self._require_agent(Capability.RECENT_POSTS)
        # Verified argument names: the Extractor takes the target profile as
        # `spreadsheetUrl`, exactly like the Profile Scraper.
        result = self.jobs().run(agent, {"spreadsheetUrl": linkedin_url,
                                         "numberMaxOfPosts": max(limit, 10)})
        wanted = (linkedin_url or "").rstrip("/").lower()

        def _is_ours(row: dict) -> bool:
            return wanted in (row.get("profileUrl") or "").rstrip("/").lower()

        def _usable(rows) -> bool:
            """A row we can actually build a Post from.

            Matching the profile is not enough. When the Extractor has already
            seen a profile it writes a marker row instead of scraping:
                {"profileUrl": "...", "postUrl": "", "error": "No new results found"}
            That row names the right person, so a profile-only check reads it as
            success and we return no posts while believing the scrape worked.
            """
            return any(_is_ours(r) and r.get("postUrl") for r in rows)

        def _settled(rows) -> bool:
            """True once re-reading result.json cannot tell us anything new.

            The marker row is the agent's final answer, not a half-written
            file, so waiting out the settle loop on it costs ~20s per profile
            and changes nothing.
            """
            return any(_is_ours(r) and r.get("error") for r in rows)

        # Same stale-read race as fetch_profile: wait for rows that actually
        # belong to the profile we asked about.
        for _ in range(RESULT_SETTLE_ATTEMPTS):
            if _usable(result.rows) or _settled(result.rows):
                break
            time.sleep(RESULT_SETTLE_SECONDS)
            try:
                result.rows = self.jobs().fetch_rows(agent)
            except Exception:
                break
        if not _usable(result.rows):
            # Same dedup as the Profile Scraper. The posts are not gone — every
            # one the agent has ever collected is in the cumulative CSV.
            try:
                cached = self.jobs().fetch_all_rows(agent)
            except Exception:
                cached = []
            # That CSV holds every profile this agent has ever scraped, and each
            # of those rows looks "own-authored" against its own profileUrl.
            # Without this filter we would hand back another prospect's posts as
            # this one's.
            cached = [r for r in cached if _is_ours(r)]
            if cached:
                logger.info("activity for %s served from cumulative results "
                            "(the Phantom found nothing new)", linkedin_url)
                result.rows = cached
        posts: list[Post] = []
        for row in result.rows:
            post_url = row.get("postUrl")
            if not post_url:
                continue
            author = (row.get("authorUrl") or "").rstrip("/").lower()
            profile = (row.get("profileUrl") or "").rstrip("/").lower()
            if profile and wanted not in profile:
                continue
            # Own content when the author is the scraped profile. `action`
            # agrees ("Post" vs "... reposted this") and is used as a
            # fallback for rows that omit authorUrl.
            is_own = bool(author) and author in (profile or wanted, wanted)
            if not is_own and str(row.get("action") or "").strip() == "Post":
                is_own = True
            if not is_own and not include_reposts:
                continue
            posts.append(Post(
                post_id=str(post_url),
                url=post_url,
                # The real author, so a caller can always tell whose words
                # these are rather than assuming the prospect's.
                author_url=row.get("authorUrl") or linkedin_url,
                text=(row.get("postContent") or "")[:1000],
                # postDate is relative ("3w"); postTimestamp is ISO.
                posted_at=row.get("postTimestamp") or row.get("postDate"),
            ))
            if len(posts) >= limit:
                break
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
