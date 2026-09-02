"""PB-1 — capability routing and the fallback policy.

The fallback policy is the safety-critical part of the provider layer. Silent
switching makes production undebuggable, and for writes it is genuinely
dangerous: retrying a connection request whose outcome is unknown can send a
second invitation to a real person and trip LinkedIn's anti-spam.

So these tests are less about "does the router work" and more about pinning the
specific cases where it must REFUSE to be helpful.
"""

from __future__ import annotations

import pytest

from linkedin_agent.providers import (
    ActionFailed,
    Capability,
    CapabilityProvider,
    CapabilityRouter,
    MalformedResponse,
    NoProviderAvailable,
    ProviderAuthError,
    ProviderRateLimited,
    ProviderTimeout,
    UnsupportedCapability,
    is_write,
)


class StubProvider(CapabilityProvider):
    """Records calls; raises whatever a test tells it to."""

    def __init__(self, name: str, supported: set, *, raises: Exception | None = None,
                 result: str = "ok") -> None:
        self.name = name
        self._supported = supported
        self._raises = raises
        self._result = result
        self.calls: list[str] = []

    def supports(self, capability: Capability) -> bool:
        return capability in self._supported

    def _run(self, label: str):
        self.calls.append(label)
        if self._raises:
            raise self._raises
        return f"{self.name}:{self._result}"

    def search_people(self, query, limit=20):
        return self._run("search_people")

    def send_connection(self, linkedin_url, note=None):
        return self._run("send_connection")

    def close(self) -> None:
        self.calls.append("close")


READ = Capability.SEARCH_PEOPLE
WRITE = Capability.CONNECT


# ----------------------------- basic routing ---------------------------------

@pytest.mark.unit
def test_primary_serves_when_it_supports() -> None:
    primary = StubProvider("pb", {READ})
    fallback = StubProvider("unipile", {READ})
    router = CapabilityRouter(primary, fallback)
    assert router.perform(READ, "search_people", "q") == "pb:ok"
    assert fallback.calls == []


@pytest.mark.unit
def test_falls_back_when_primary_does_not_support() -> None:
    """The designed path: a genuine product gap, routed around cleanly."""
    primary = StubProvider("pb", set())
    fallback = StubProvider("unipile", {READ})
    router = CapabilityRouter(primary, fallback)
    assert router.perform(READ, "search_people", "q") == "unipile:ok"
    assert primary.calls == []


@pytest.mark.unit
def test_raises_when_nobody_supports() -> None:
    router = CapabilityRouter(StubProvider("pb", set()), StubProvider("u", set()))
    with pytest.raises(NoProviderAvailable):
        router.perform(READ, "search_people", "q")


@pytest.mark.unit
def test_unconfigured_provider_is_skipped_not_called() -> None:
    """supports() must reflect configuration, so the router routes around a
    provider missing its credentials instead of failing mid-operation."""
    primary = StubProvider("pb", set())        # e.g. no API key
    fallback = StubProvider("unipile", {READ})
    router = CapabilityRouter(primary, fallback)
    router.perform(READ, "search_people", "q")
    assert primary.calls == []


# ----------------------------- READ fallback policy --------------------------

@pytest.mark.unit
@pytest.mark.parametrize("exc", [
    UnsupportedCapability("nope"),
    MalformedResponse("shape changed"),
    ProviderTimeout("job overran"),
])
def test_read_falls_back_on_recoverable_errors(exc) -> None:
    primary = StubProvider("pb", {READ}, raises=exc)
    fallback = StubProvider("unipile", {READ})
    router = CapabilityRouter(primary, fallback)
    assert router.perform(READ, "search_people", "q") == "unipile:ok"
    assert primary.calls == ["search_people"]


@pytest.mark.unit
def test_read_does_not_fall_back_on_auth_error() -> None:
    """An expired cookie is a human problem. Falling back hides the breakage
    and burns the other provider's budget."""
    primary = StubProvider("pb", {READ}, raises=ProviderAuthError("cookie expired"))
    fallback = StubProvider("unipile", {READ})
    router = CapabilityRouter(primary, fallback)
    with pytest.raises(ProviderAuthError):
        router.perform(READ, "search_people", "q")
    assert fallback.calls == []


@pytest.mark.unit
def test_read_does_not_fall_back_on_rate_limit() -> None:
    """Both providers drive the SAME LinkedIn account, so the throttle is
    usually account-level. Falling back relocates the abuse and deepens it."""
    primary = StubProvider("pb", {READ}, raises=ProviderRateLimited("429"))
    fallback = StubProvider("unipile", {READ})
    router = CapabilityRouter(primary, fallback)
    with pytest.raises(ProviderRateLimited):
        router.perform(READ, "search_people", "q")
    assert fallback.calls == []


# ----------------------------- WRITE fallback policy -------------------------

@pytest.mark.unit
def test_write_falls_back_only_when_unsupported() -> None:
    primary = StubProvider("pb", set())
    fallback = StubProvider("unipile", {WRITE})
    router = CapabilityRouter(primary, fallback)
    assert router.perform(WRITE, "send_connection", "url") == "unipile:ok"


@pytest.mark.unit
@pytest.mark.parametrize("exc", [
    MalformedResponse("weird output"),
    ProviderTimeout("job never finished"),
    ActionFailed("rejected", error_code="already_invited_recently"),
])
def test_write_never_retries_on_another_provider(exc) -> None:
    """The most important test here.

    A failed or ambiguous write may have partially completed. Retrying it
    elsewhere risks a second invitation or DM to a real prospect, which trips
    anti-spam and burns the relationship. Reads are idempotent; writes are not.
    """
    primary = StubProvider("pb", {WRITE}, raises=exc)
    fallback = StubProvider("unipile", {WRITE})
    router = CapabilityRouter(primary, fallback)
    with pytest.raises(type(exc)):
        router.perform(WRITE, "send_connection", "url")
    assert fallback.calls == [], "a write must never be retried on a second provider"


@pytest.mark.unit
def test_action_failed_preserves_error_code() -> None:
    """The cooldown system keys on this code; losing it loses the protection."""
    primary = StubProvider("pb", {WRITE},
                           raises=ActionFailed("x", error_code="already_invited_recently"))
    router = CapabilityRouter(primary, StubProvider("u", {WRITE}))
    with pytest.raises(ActionFailed) as ei:
        router.perform(WRITE, "send_connection", "url")
    assert ei.value.error_code == "already_invited_recently"


@pytest.mark.unit
def test_every_write_capability_is_classified_as_a_write() -> None:
    """Guard against a future capability being added to the enum but not to
    WRITE_CAPABILITIES, which would silently opt it into read fallback."""
    for cap in (Capability.REACT, Capability.CONNECT,
                Capability.SEND_DM, Capability.WITHDRAW_INVITE):
        assert is_write(cap), f"{cap} must be treated as a write"
    for cap in (Capability.SEARCH_PEOPLE, Capability.PROFILE,
                Capability.EXPERIENCE, Capability.INBOX_READ):
        assert not is_write(cap)


# ----------------------------- overrides + inspection ------------------------

@pytest.mark.unit
def test_override_pins_a_capability_to_a_named_provider() -> None:
    """Documented gaps should be visible in configuration, not implied by
    provider ordering."""
    primary = StubProvider("pb", {READ})
    fallback = StubProvider("unipile", {READ})
    router = CapabilityRouter(primary, fallback,
                              overrides={READ: "unipile"})
    assert router.perform(READ, "search_people", "q") == "unipile:ok"
    assert primary.calls == []


@pytest.mark.unit
def test_unknown_override_is_ignored_not_fatal() -> None:
    primary = StubProvider("pb", {READ})
    router = CapabilityRouter(primary, None, overrides={READ: "typo"})
    assert router.perform(READ, "search_people", "q") == "pb:ok"


@pytest.mark.unit
def test_routing_table_is_inspectable() -> None:
    """`linkedin providers` renders this, so which capabilities are
    PhantomBuster-owned is obvious rather than inferred."""
    router = CapabilityRouter(StubProvider("pb", {READ}),
                              StubProvider("unipile", {WRITE}))
    table = router.routing_table()
    assert table[READ.value] == "pb"
    assert table[WRITE.value] == "unipile"
    assert table[Capability.SEARCH_POSTS.value] == "NONE"


@pytest.mark.unit
def test_close_closes_both_and_survives_failure() -> None:
    class Exploding(StubProvider):
        def close(self):
            raise RuntimeError("boom")

    primary = Exploding("pb", {READ})
    fallback = StubProvider("unipile", {READ})
    CapabilityRouter(primary, fallback).close()
    assert fallback.calls == ["close"], "one provider failing must not skip the other"


# ----------------------------- default methods -------------------------------

@pytest.mark.unit
def test_unimplemented_methods_raise_unsupported() -> None:
    """A provider implements exactly what it declares; anything else must
    announce itself rather than fail obscurely later."""
    class Bare(CapabilityProvider):
        name = "bare"

        def supports(self, capability): return True

    with pytest.raises(UnsupportedCapability):
        Bare().send_dm("url", "body")
    with pytest.raises(UnsupportedCapability):
        Bare().fetch_inbox()
