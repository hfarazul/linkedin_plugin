"""Capability router: try the primary provider, fall back only when it is safe.

The routing decision is per capability, never per provider. "PhantomBuster for
discovery, Unipile for sending" is a conclusion the capability table may or may
not produce; it is not a rule wired into the code. If PhantomBuster gains a
capability, declaring it in that provider's `supports()` is the entire change.

The fallback policy is the important part of this module. Silent provider
switching makes production impossible to debug, and for writes it is actively
dangerous: a connection request whose outcome we are unsure of must never be
retried on a second provider, because the first attempt may have succeeded and
the prospect receives two invitations.
"""

from __future__ import annotations

import logging

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
from .capabilities import Capability, is_write

logger = logging.getLogger("linkedin.providers")


# Which failures may fall through to the fallback provider, for READS.
#
#   UnsupportedCapability  yes  -- the designed path; a real product gap
#   MalformedResponse      yes  -- a scrape shape changed; the other may work
#   ProviderTimeout        yes  -- an async job overran; a sync call may not
#   ProviderAuthError      NO   -- misconfiguration; fail loudly and fix it
#   ProviderRateLimited    NO   -- same LinkedIn account behind both providers,
#                                  so falling back deepens the block
#   ActionFailed           NO   -- LinkedIn rejected it; that is an answer
FALLBACK_ON_READ = (UnsupportedCapability, MalformedResponse, ProviderTimeout)

# For WRITES only an explicit "I cannot do this" is safe. Anything else may
# mean the action partially completed.
FALLBACK_ON_WRITE = (UnsupportedCapability,)


class NoProviderAvailable(ProviderError):
    """Neither provider can perform the capability."""


class CapabilityRouter:
    """Routes each capability to the first provider that supports it."""

    def __init__(self, primary: CapabilityProvider,
                 fallback: CapabilityProvider | None = None,
                 *, overrides: dict[Capability, str] | None = None) -> None:
        self.primary = primary
        self.fallback = fallback
        # Pin specific capabilities to a named provider regardless of order.
        # Used for documented gaps so they are visible in configuration rather
        # than implied by whichever provider happens to be listed first.
        self.overrides = overrides or {}

    # ------------------------------------------------------------- routing

    def providers_for(self, capability: Capability) -> list[CapabilityProvider]:
        """Ordered candidates for a capability, honouring overrides."""
        candidates = [p for p in (self.primary, self.fallback) if p is not None]
        pinned = self.overrides.get(capability)
        if pinned:
            match = [p for p in candidates if p.name == pinned]
            if match:
                return match
            logger.warning(
                "capability %s pinned to unknown provider %r; ignoring override",
                capability.value, pinned,
            )
        return [p for p in candidates if p.supports(capability)]

    def owner_of(self, capability: Capability) -> str | None:
        """Which provider will actually serve this capability. Powers the
        `providers` CLI view so routing is inspectable, not guesswork."""
        chain = self.providers_for(capability)
        return chain[0].name if chain else None

    def routing_table(self) -> dict[str, str]:
        return {c.value: (self.owner_of(c) or "NONE") for c in Capability}

    # ------------------------------------------------------------- execute

    def perform(self, capability: Capability, method: str, *args, **kwargs):
        """Run `method` on the first provider that supports `capability`,
        falling back only per the policy above."""
        chain = self.providers_for(capability)
        if not chain:
            raise NoProviderAvailable(
                f"no configured provider supports {capability.value}")

        allowed = FALLBACK_ON_WRITE if is_write(capability) else FALLBACK_ON_READ
        last: Exception | None = None

        for index, provider in enumerate(chain):
            is_last = index == len(chain) - 1
            try:
                result = getattr(provider, method)(*args, **kwargs)
                if index > 0:
                    logger.info("capability %s served by fallback %s",
                                capability.value, provider.name)
                return result
            except Exception as exc:
                last = exc
                if is_last or not isinstance(exc, allowed):
                    # Log before re-raising so the reason a run stopped is in
                    # the record, not just the traceback.
                    if isinstance(exc, ProviderAuthError):
                        logger.error("%s auth failure on %s -- not falling back; "
                                     "fix the credential",
                                     provider.name, capability.value)
                    elif isinstance(exc, ProviderRateLimited):
                        logger.error("%s rate limited on %s -- not falling back; "
                                     "both providers share the LinkedIn account",
                                     provider.name, capability.value)
                    elif isinstance(exc, ActionFailed):
                        logger.warning("%s action failed on %s (code=%s) -- "
                                       "not retrying elsewhere; the write may "
                                       "have partially completed",
                                       provider.name, capability.value,
                                       getattr(exc, "error_code", None))
                    raise
                logger.warning("%s could not serve %s (%s: %s) -- trying %s",
                               provider.name, capability.value,
                               type(exc).__name__, exc, chain[index + 1].name)

        assert last is not None
        raise last

    def close(self) -> None:
        for p in (self.primary, self.fallback):
            if p is not None:
                try:
                    p.close()
                except Exception:  # cleanup must never mask a real error
                    logger.debug("error closing provider %s", p.name, exc_info=True)
