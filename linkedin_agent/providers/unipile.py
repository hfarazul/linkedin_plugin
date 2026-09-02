"""Unipile as a CapabilityProvider.

Deliberately implemented first, before PhantomBuster. Wrapping the provider we
know works proves the abstraction is genuinely vendor-neutral: if the interface
were quietly shaped around PhantomBuster's async job model, Unipile would not
fit it cleanly and we would find out here rather than after the migration.

This is a thin translation layer over code that already works
(UnipileAdapter, enrichment._ProfileClient, poll._UnipileMessages). It adds no
LinkedIn logic of its own — it maps responses into normalized DTOs and HTTP
failures into the typed error taxonomy.
"""

from __future__ import annotations

import logging
import re

import httpx

from ..adapters.unipile_adapter import UnipileAdapter
from ..config import Config
from .base import (
    ActionFailed,
    CapabilityProvider,
    MalformedResponse,
    ProviderAuthError,
    ProviderRateLimited,
    ProviderTimeout,
)
from .capabilities import (
    ActionResult,
    Capability,
    InboundMessage,
    Position,
    ProfileFacts,
)

logger = logging.getLogger("linkedin.providers.unipile")

SUPPORTED = frozenset({
    Capability.SEARCH_PEOPLE,
    Capability.SEARCH_POSTS,
    Capability.PROFILE,
    Capability.EXPERIENCE,
    Capability.RECENT_POSTS,
    Capability.INBOX_READ,
    Capability.ACCEPTANCE_CHECK,
    Capability.REACT,
    Capability.CONNECT,
    Capability.SEND_DM,
})

# Verified live on 2026-09-01 (P0-1): the profile endpoint returns work history
# only when asked with `linkedin_sections`. The marketing docs say
# `with_sections`, which v1 silently ignores -- that wrong spelling produced a
# response identical to the baseline and read as "no work history available".
EXPERIENCE_PARAM = "linkedin_sections"
EXPERIENCE_VALUE = "experience"

# Unipile date shape is "M/D/YYYY" with the day always 1. A leading "1/1/"
# cannot be distinguished from a genuine January, so it is treated as
# year-precision -- honest rather than optimistic, since a 180-day recency
# window built on a guessed month would be fiction.
_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")

_FIRST_DEGREE = {"FIRST_DEGREE", "DISTANCE_1", "1", 1}


def _parse_date(value) -> tuple[str | None, str]:
    """Return (ISO date, precision)."""
    if not value or not isinstance(value, str):
        return None, "unknown"
    m = _DATE_RE.match(value.strip())
    if not m:
        return value, "unknown"
    month, day, year = int(m.group(1)), int(m.group(2)), m.group(3)
    if month == 1 and day == 1:
        return f"{year}-01-01", "year"
    return f"{year}-{month:02d}-{day:02d}", "month"


def _positions_from_profile(profile: dict) -> list[Position]:
    raw = profile.get("work_experience")
    if not isinstance(raw, list):
        return []
    out: list[Position] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start, precision = _parse_date(item.get("start"))
        end, _ = _parse_date(item.get("end"))
        out.append(Position(
            company=item.get("company") or "",
            title=item.get("position"),
            company_id=str(item["company_id"]) if item.get("company_id") else None,
            start_date=start,
            end_date=end,
            # Unipile omits an explicit current flag; a null end is the signal.
            # Note this can be true of several positions at once for people
            # holding concurrent roles.
            is_current=item.get("end") in (None, ""),
            date_precision=precision,
            location=item.get("location"),
            description=item.get("description"),
            source="unipile",
            raw=item,
        ))
    return out


class UnipileProvider(CapabilityProvider):
    name = "unipile"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._configured = bool(cfg.unipile_api_key and cfg.unipile_account_id
                                and cfg.unipile_dsn)
        self._adapter: UnipileAdapter | None = None
        self._http: httpx.Client | None = None

    def supports(self, capability: Capability) -> bool:
        # An unconfigured provider supports nothing. Saying so here lets the
        # router route around it cleanly instead of failing mid-operation.
        return self._configured and capability in SUPPORTED

    # ----------------------------------------------------------- internals

    def _api(self) -> UnipileAdapter:
        if self._adapter is None:
            self._adapter = UnipileAdapter(self.cfg)
        return self._adapter

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=f"https://{self.cfg.unipile_dsn}/api/v1",
                headers={"X-API-KEY": self.cfg.unipile_api_key,
                         "accept": "application/json"},
                timeout=30.0,
            )
        return self._http

    def _translate(self, exc: Exception) -> Exception:
        """Map transport and HTTP failures onto the taxonomy the router
        dispatches on. Getting this wrong changes the fallback decision, so
        auth and rate-limit are matched explicitly rather than lumped in."""
        if isinstance(exc, httpx.TimeoutException):
            return ProviderTimeout(str(exc), provider=self.name)
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            if code in (401, 403):
                return ProviderAuthError(f"HTTP {code}", provider=self.name)
            if code == 429:
                return ProviderRateLimited(f"HTTP {code}", provider=self.name)
            if code == 422:
                body = ""
                try:
                    body = exc.response.json().get("type", "") or exc.response.text[:200]
                except Exception:
                    body = exc.response.text[:200]
                return ActionFailed(f"HTTP 422: {body}", provider=self.name,
                                    error_code=body or None)
            return MalformedResponse(f"HTTP {code}", provider=self.name)
        if isinstance(exc, httpx.HTTPError):
            return ProviderTimeout(str(exc), provider=self.name)
        return exc

    # --------------------------------------------------------------- reads

    def search_people(self, query: str, limit: int = 20) -> list:
        try:
            return self._api().search(query, limit=limit)
        except Exception as e:
            raise self._translate(e) from e

    def search_posts(self, keywords: str, *, limit: int = 20,
                     date_posted: str = "past_month",
                     author_keywords: str | None = None) -> list:
        try:
            return self._api().search_posts(
                keywords, limit=limit, date_posted=date_posted,
                author_keywords=author_keywords)
        except Exception as e:
            raise self._translate(e) from e

    def get_recent_posts(self, linkedin_url: str, limit: int = 5) -> list:
        try:
            return self._api().get_recent_posts(linkedin_url, limit=limit)
        except Exception as e:
            raise self._translate(e) from e

    def fetch_profile(self, identifier: str, *,
                      with_experience: bool = False) -> ProfileFacts | None:
        params: dict = {"account_id": self.cfg.unipile_account_id}
        if with_experience:
            params[EXPERIENCE_PARAM] = EXPERIENCE_VALUE
        try:
            r = self._client().get(f"/users/{identifier}", params=params)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            profile = r.json()
        except Exception as e:
            raise self._translate(e) from e

        if not isinstance(profile, dict):
            raise MalformedResponse("profile was not an object", provider=self.name)

        return ProfileFacts(
            provider_id=profile.get("provider_id"),
            public_identifier=profile.get("public_identifier"),
            full_name=" ".join(
                p for p in (profile.get("first_name"), profile.get("last_name")) if p
            ) or None,
            headline=profile.get("headline"),
            location=profile.get("location"),
            network_distance=profile.get("network_distance"),
            follower_count=profile.get("follower_count"),
            connections_count=profile.get("connections_count"),
            is_premium=profile.get("is_premium"),
            is_open_profile=profile.get("is_open_profile"),
            positions=_positions_from_profile(profile) if with_experience else [],
            source=self.name,
            raw=profile,
        )

    def check_acceptance(self, identifier: str) -> bool | None:
        facts = self.fetch_profile(identifier)
        if facts is None:
            return None
        return facts.network_distance in _FIRST_DEGREE

    def fetch_inbox(self, limit: int = 50) -> list[InboundMessage]:
        try:
            r = self._client().get("/messages", params={
                "account_id": self.cfg.unipile_account_id, "limit": limit})
            r.raise_for_status()
            items = r.json().get("items", [])
        except Exception as e:
            raise self._translate(e) from e

        out: list[InboundMessage] = []
        for m in items:
            external = m.get("id") or m.get("provider_id")
            if not external:
                continue
            out.append(InboundMessage(
                external_id=str(external),
                prospect_provider_id=m.get("sender_id"),
                body=m.get("text") or "",
                sent_at=m.get("timestamp") or m.get("date"),
                thread_id=m.get("chat_id"),
                is_from_me=bool(m.get("is_sender")),
                source=self.name,
                raw=m,
            ))
        return out

    # -------------------------------------------------------------- writes

    def react(self, post, reaction: str = "LIKE") -> ActionResult:
        try:
            result = self._api().react(post, reaction=reaction)
        except Exception as e:
            raise self._translate(e) from e
        return ActionResult(status="sent", provider=self.name, external_id=str(result))

    def send_connection(self, linkedin_url: str,
                        note: str | None = None) -> ActionResult:
        try:
            result = self._api().send_connection(linkedin_url, note=note)
        except Exception as e:
            raise self._translate(e) from e
        return ActionResult(status="sent", provider=self.name, external_id=str(result))

    def send_dm(self, linkedin_url: str, body: str) -> ActionResult:
        try:
            result = self._api().send_dm(linkedin_url, body)
        except Exception as e:
            raise self._translate(e) from e
        return ActionResult(status="sent", provider=self.name, external_id=str(result))

    def close(self) -> None:
        if self._adapter is not None:
            self._adapter.close()
            self._adapter = None
        if self._http is not None:
            self._http.close()
            self._http = None
