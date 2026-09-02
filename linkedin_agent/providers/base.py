"""The provider contract and its error taxonomy.

A provider declares which capabilities it supports and implements only those.
`supports()` is the mechanism the router uses to choose, so declaring a
capability you cannot actually perform is the one unforgivable lie here — it
turns a clean fallback into a runtime failure.

The error taxonomy exists so the router can make different decisions for
different failures. Collapsing everything into one exception would force a
single fallback policy, and the right policy genuinely differs: an expired
cookie should stop the run loudly, while a malformed scrape should quietly
fall through to the other provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .capabilities import (
    ActionResult,
    Capability,
    InboundMessage,
    ProfileFacts,
)


# ------------------------------------------------------------------ errors


class ProviderError(RuntimeError):
    """Base for anything a provider can fail with."""

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class UnsupportedCapability(ProviderError):
    """This provider does not implement this capability at all.

    The designed path, not an error condition — it is how a capability-based
    architecture routes around a genuine product gap. Always safe to fall back.
    """


class ProviderAuthError(ProviderError):
    """Credentials rejected, or a LinkedIn session cookie has expired.

    Never fall back on this. A bad cookie is a configuration problem that a
    human must fix, and silently shifting the load onto the other provider
    hides the breakage while consuming its budget.
    """


class ProviderRateLimited(ProviderError):
    """The provider or LinkedIn is throttling us.

    Never fall back by default. Both providers drive the SAME LinkedIn account,
    so the throttle usually lives at the account level — falling back just
    relocates the abuse and deepens the block.
    """


class ProviderTimeout(ProviderError):
    """A request or an async job exceeded its deadline."""


class MalformedResponse(ProviderError):
    """The response arrived but could not be parsed into our DTOs.

    Usually means a Phantom's output shape changed. Safe to fall back for
    reads; must not be treated as a definitive answer.
    """


class ActionFailed(ProviderError):
    """A write reached LinkedIn and LinkedIn rejected it.

    NEVER fall back. The action may have partially completed, and retrying it
    on another provider risks a duplicate invite or DM to a real person.
    `error_code` carries the vendor's reason where one exists — Unipile's
    `already_invited_recently` is what the cooldown system keys on.
    """

    def __init__(self, message: str, *, provider: str | None = None,
                 error_code: str | None = None) -> None:
        super().__init__(message, provider=provider)
        self.error_code = error_code


# ---------------------------------------------------------------- provider


class CapabilityProvider(ABC):
    """One vendor's implementation of some subset of the capabilities.

    Every method returns normalized DTOs. Vendor JSON may be carried along in
    the `raw` field for evidence and debugging, but nothing above this layer is
    permitted to read it.
    """

    name: str = "unknown"

    @abstractmethod
    def supports(self, capability: Capability) -> bool:
        """True only if this provider can actually perform the capability
        right now — including configuration. A provider missing its API key
        supports nothing, and should say so here rather than failing later."""

    # Each method raises UnsupportedCapability unless overridden, so a provider
    # implements exactly what it declares and nothing is silently half-wired.

    def search_people(self, query: str, limit: int = 20) -> list:
        raise UnsupportedCapability(f"{self.name}: search_people", provider=self.name)

    def search_posts(self, keywords: str, *, limit: int = 20,
                     date_posted: str = "past_month",
                     author_keywords: str | None = None) -> list:
        raise UnsupportedCapability(f"{self.name}: search_posts", provider=self.name)

    def fetch_profile(self, identifier: str, *,
                      with_experience: bool = False) -> ProfileFacts | None:
        raise UnsupportedCapability(f"{self.name}: fetch_profile", provider=self.name)

    def get_recent_posts(self, linkedin_url: str, limit: int = 5) -> list:
        raise UnsupportedCapability(f"{self.name}: get_recent_posts", provider=self.name)

    def fetch_inbox(self, limit: int = 50) -> list[InboundMessage]:
        raise UnsupportedCapability(f"{self.name}: fetch_inbox", provider=self.name)

    def check_acceptance(self, identifier: str) -> bool | None:
        """True if now a 1st-degree connection, False if not, None if unknown."""
        raise UnsupportedCapability(f"{self.name}: check_acceptance", provider=self.name)

    def react(self, post, reaction: str = "LIKE") -> ActionResult:
        raise UnsupportedCapability(f"{self.name}: react", provider=self.name)

    def send_connection(self, linkedin_url: str,
                        note: str | None = None) -> ActionResult:
        raise UnsupportedCapability(f"{self.name}: send_connection", provider=self.name)

    def send_dm(self, linkedin_url: str, body: str) -> ActionResult:
        raise UnsupportedCapability(f"{self.name}: send_dm", provider=self.name)

    def close(self) -> None:
        """Optional cleanup."""
