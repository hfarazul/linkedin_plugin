"""Legacy LinkedInAdapter interface, served by the capability router.

`cli.py`, `daily.py` and `bot_daemon.py` call `adapter.search(...)`,
`adapter.react(...)` and friends in a dozen places. Rewriting all of them to
call `router.perform(Capability.X, "method", ...)` would be a large diff
through the most load-bearing code in the project, for no behavioural gain.

This adapts one interface to the other instead. Existing call sites keep
working unchanged, while every call underneath goes through capability routing,
the fallback policy, and pipeline tracing.

The adaptation is not free of impedance: the old interface returns bare
strings from writes, the new one returns ActionResult with a status that can be
"unknown". That difference is real and is preserved rather than flattened —
see `_result_string`.
"""

from __future__ import annotations

import logging

from ..adapters.base import LinkedInAdapter, Post, PostHit, ProspectHit
from .capabilities import ActionResult, Capability
from .router import CapabilityRouter

logger = logging.getLogger("linkedin.providers.adapter")


class RouterAdapter(LinkedInAdapter):
    """Presents a CapabilityRouter as the legacy adapter."""

    def __init__(self, router: CapabilityRouter) -> None:
        self.router = router

    # ------------------------------------------------------------- reads

    def search(self, query: str, limit: int = 20) -> list[ProspectHit]:
        return self.router.perform(
            Capability.SEARCH_PEOPLE, "search_people", query, limit)

    def search_posts(self, keywords: str, *, limit: int = 20,
                     date_posted: str = "past_month",
                     author_keywords: str | None = None) -> list[PostHit]:
        return self.router.perform(
            Capability.SEARCH_POSTS, "search_posts", keywords,
            limit=limit, date_posted=date_posted,
            author_keywords=author_keywords)

    def get_recent_posts(self, linkedin_url: str, limit: int = 5, *,
                         include_reposts: bool = False) -> list[Post]:
        # Passed only when set, so a provider that has never heard of reposts
        # is called exactly as before.
        extra = {"include_reposts": True} if include_reposts else {}
        return self.router.perform(
            Capability.RECENT_POSTS, "get_recent_posts", linkedin_url, limit,
            **extra)

    # ------------------------------------------------------------ writes

    def react(self, post: Post, reaction: str = "LIKE") -> str:
        return _result_string(
            self.router.perform(Capability.REACT, "react", post, reaction),
            "react")

    def send_connection(self, linkedin_url: str, note: str | None = None) -> str:
        return _result_string(
            self.router.perform(Capability.CONNECT, "send_connection",
                                linkedin_url, note),
            "connect")

    def send_dm(self, linkedin_url: str, body: str) -> str:
        return _result_string(
            self.router.perform(Capability.SEND_DM, "send_dm",
                                linkedin_url, body),
            "dm")

    def close(self) -> None:
        self.router.close()


# Marker for a write that was dispatched but whose delivery nobody observed.
# The send funnel keys off this to decide whether the follow-up clock may
# start, so the prefix is a contract between this module and
# bot_daemon.send_draft_via_adapter rather than a log-only string.
UNCONFIRMED_PREFIX = "dispatched:unconfirmed:"


def is_unconfirmed(api_result) -> bool:
    """True when a write reached the provider but delivery was never confirmed."""
    return isinstance(api_result, str) and api_result.startswith(UNCONFIRMED_PREFIX)


def _result_string(result, label: str) -> str:
    """Collapse an ActionResult into the string the old interface expects,
    without pretending an unconfirmed write succeeded.

    PhantomBuster reports that a container finished, not that a particular
    recipient received anything. Callers store this string in the actions log,
    so an unconfirmed send has to be legible there rather than indistinguishable
    from a confirmed one — otherwise the audit trail claims delivery we never
    observed.
    """
    if not isinstance(result, ActionResult):
        return str(result)
    if result.status == "unknown":
        logger.warning(
            "%s dispatched via %s but delivery is unconfirmed "
            "(container finished; no per-recipient outcome)",
            label, result.provider)
        return (f"{UNCONFIRMED_PREFIX}{result.provider}:"
                f"{result.external_id or '?'}")
    if result.status == "failed":
        return f"failed:{result.error_code or 'unknown'}"
    return result.external_id or "sent"
