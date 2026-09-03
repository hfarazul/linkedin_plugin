"""Capability-based LinkedIn provider layer.

Routing is per capability, not per provider. The application asks for
Capability.EXPERIENCE; which vendor answers is a configuration and capability
question resolved here, and invisible above this seam.
"""

from .base import (
    ActionFailed,
    CapabilityProvider,
    MalformedResponse,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    UnsupportedCapability,
)
from .capabilities import (
    ActionResult,
    Capability,
    InboundMessage,
    Position,
    ProfileFacts,
    is_write,
)
from .pb_jobs import PhantomBusterJobs
from .router import CapabilityRouter, NoProviderAvailable
from .router_adapter import RouterAdapter

__all__ = [
    "ActionFailed", "ActionResult", "Capability", "CapabilityProvider",
    "CapabilityRouter", "InboundMessage", "MalformedResponse",
    "NoProviderAvailable", "Position", "ProfileFacts", "ProviderAuthError",
    "ProviderError", "ProviderRateLimited", "ProviderTimeout",
    "PhantomBusterJobs", "RouterAdapter", "UnsupportedCapability", "is_write",
]


def build_router(cfg, *, primary: str | None = None,
                 fallback: str | None = None,
                 overrides: dict | None = None) -> CapabilityRouter:
    """Construct the router from config.

    Provider names are resolved lazily so that adding PhantomBuster later is a
    single entry here, and so a missing optional dependency cannot break
    startup for someone using only the other provider.
    """
    import os

    primary = primary or os.getenv("LINKEDIN_PRIMARY_PROVIDER", "phantombuster")
    fallback = fallback or os.getenv("LINKEDIN_FALLBACK_PROVIDER") or None

    def _make(name: str | None):
        if not name:
            return None
        if name == "phantombuster":
            from .phantombuster import PhantomBusterProvider
            return PhantomBusterProvider(cfg)
        if name == "unipile":
            # Removed. The connected LinkedIn account was disconnected on
            # Unipile's side (HTTP 401 errors/disconnected_account), so the
            # fallback had stopped serving anything before it was dropped.
            # The module is retained for its tests and for the date-parsing
            # reference, but is no longer routable.
            raise ValueError(
                "the unipile provider has been removed; PhantomBuster is the "
                "only provider. See docs/PHANTOMBUSTER_MIGRATION.md")
        raise ValueError(f"unknown provider {name!r}")

    primary_provider = _make(primary)
    if primary_provider is None:
        raise ValueError("a primary provider is required")
    return CapabilityRouter(primary_provider, _make(fallback),
                            overrides=overrides)
