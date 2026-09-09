"""Tests for pipeline tracing.

Two properties carry weight here.

**Redaction**, because a trace is diagnostic output: it gets pasted into
tickets and chat. This codebase has already leaked a Telegram bot token into
git and a LinkedIn session cookie into a terminal. Scrubbing on the way in is
what stops a third.

**Routing visibility**, because a fallback that does not appear in the trace is
a silent provider switch — the thing the router exists to make impossible.
"""

from __future__ import annotations

import pytest

from linkedin_agent import trace


FAKE_TOKEN = "8832515932:AAFvChNSFx_F6LaV955OB4RUC0ErdHjmunA"
FAKE_COOKIE = "AQEDATl5vRMFEInaAAABniK-xkkAAAGgf6W-VlYADTmkac_a5sVWu8Jqh"


# ----------------------------- redaction -------------------------------------

@pytest.mark.unit
def test_secret_keys_are_redacted_by_name() -> None:
    out = trace.redact({"sessionCookie": FAKE_COOKIE, "inboxFilter": "unread"})
    assert FAKE_COOKIE not in str(out)
    assert out["inboxFilter"] == "unread", "non-secrets must survive"


@pytest.mark.unit
@pytest.mark.parametrize("key", [
    "sessionCookie", "session_cookie", "apiKey", "api_key",
    "PHANTOMBUSTER_API_KEY", "authorization", "password", "token",
])
def test_credential_key_spellings(key) -> None:
    out = trace.redact({key: "super-secret-value-12345"})
    assert "super-secret-value" not in str(out)


@pytest.mark.unit
def test_telegram_token_redacted_by_shape_anywhere() -> None:
    """Even under a harmless key name — the value itself is the credential."""
    out = trace.redact({"url": f"https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates"})
    assert FAKE_TOKEN not in str(out)


@pytest.mark.unit
def test_linkedin_cookie_redacted_by_shape() -> None:
    out = trace.redact(f"cookie is {FAKE_COOKIE} here")
    assert FAKE_COOKIE not in out


@pytest.mark.unit
def test_redaction_recurses_into_nested_structures() -> None:
    """Provider payloads nest; a cookie two levels down is just as sensitive."""
    out = trace.redact({"agent": {"args": [{"sessionCookie": FAKE_COOKIE}]}})
    assert FAKE_COOKIE not in str(out)


@pytest.mark.unit
def test_step_note_redacts_on_the_way_in() -> None:
    """A caller must not have to remember to scrub."""
    with trace.run(echo=False) as run:
        with run.step("X") as st:
            st.note("sessionCookie", FAKE_COOKIE)
            st.note("headline", "Software Engineer")
    step = run.steps[0]
    assert FAKE_COOKIE not in str(step.fields)
    assert step.fields["headline"] == "Software Engineer"


@pytest.mark.unit
def test_summary_never_contains_a_secret() -> None:
    with trace.run(echo=False) as run:
        with run.step("X") as st:
            st.note("apiKey", "abcdef123456789012345")
    assert "abcdef123456789012345" not in run.summary()


# ----------------------------- error classification --------------------------

@pytest.mark.unit
@pytest.mark.parametrize("exc,expected", [
    ("UnsupportedCapability", "UNSUPPORTED_CAPABILITY"),
    ("ProviderAuthError", "AUTH_FAILURE"),
    ("ProviderRateLimited", "RATE_LIMIT"),
    ("ProviderTimeout", "TIMEOUT"),
    ("MalformedResponse", "MALFORMED_RESPONSE"),
    ("ActionFailed", "PROVIDER_ACTION_FAILED"),
])
def test_provider_errors_are_named_distinctly(exc, expected) -> None:
    """An unsupported capability and an auth failure look identical in a stack
    trace but mean opposite things — one is the designed path, the other is a
    misconfiguration."""
    import linkedin_agent.providers.base as base
    assert trace.classify_error(getattr(base, exc)("x")) == expected


@pytest.mark.unit
def test_database_and_validation_failures_are_distinguished() -> None:
    import sqlite3
    assert trace.classify_error(sqlite3.OperationalError("no such table")) \
        == "DATABASE_FAILURE"
    assert trace.classify_error(ValueError("bad input")) == "VALIDATION_FAILURE"
    assert trace.classify_error(RuntimeError("?")) == "PROVIDER_EXECUTION_FAILURE"


# ----------------------------- step lifecycle --------------------------------

@pytest.mark.unit
def test_failing_step_records_class_and_reraises() -> None:
    with trace.run(echo=False) as run:
        with pytest.raises(ValueError):
            with run.step("BOOM"):
                raise ValueError("nope")
    step = run.steps[0]
    assert step.status == trace.FAIL
    assert step.error_class == "VALIDATION_FAILURE"
    assert "nope" in step.error


@pytest.mark.unit
def test_steps_are_numbered_and_timed() -> None:
    with trace.run(total=2, echo=False) as run:
        with run.step("ONE"):
            pass
        with run.step("TWO"):
            pass
    assert [s.index for s in run.steps] == [1, 2]
    assert all(s.duration_ms >= 0 for s in run.steps)


@pytest.mark.unit
def test_counts_separate_live_from_stubbed() -> None:
    """A green run made entirely of stubs proves nothing, so the summary must
    make the difference visible."""
    with trace.run(echo=False) as run:
        with run.step("LIVE_ONE"):
            pass
        with run.step("STUB_ONE", mode=trace.STUBBED):
            pass
        with run.step("GAP", mode=trace.NOT_IMPLEMENTED) as st:
            st.status = trace.SKIP
    counts = run.counts()
    assert counts["live"] == 1
    assert counts["mocked"] == 1
    assert counts["not_implemented"] == 1
    assert counts["skipped"] == 1


@pytest.mark.unit
def test_run_ids_are_unique_and_short() -> None:
    with trace.run(echo=False) as a:
        pass
    with trace.run(echo=False) as b:
        pass
    assert a.run_id != b.run_id
    assert len(a.run_id) == 8


# ----------------------------- routing visibility ----------------------------

@pytest.mark.unit
def test_router_records_which_provider_served_a_capability() -> None:
    from linkedin_agent.providers import Capability
    from linkedin_agent.providers.router import CapabilityRouter

    class P:
        name = "phantombuster"

        def supports(self, c):
            return True

        def fetch_profile(self, ident, **k):
            return "ok"

        def close(self):
            pass

    with trace.run(echo=False) as run:
        with run.step("ENRICH"):
            CapabilityRouter(P(), None).perform(
                Capability.PROFILE, "fetch_profile", "x")
    assert run.steps[0].provider == "phantombuster"
    assert run.steps[0].capability == "profile"


@pytest.mark.unit
def test_fallback_is_visible_not_silent() -> None:
    """The whole point of the instrumentation: a fallback must not look like a
    clean first-try success."""
    from linkedin_agent.providers import Capability, UnsupportedCapability
    from linkedin_agent.providers.router import CapabilityRouter

    class Primary:
        name = "phantombuster"

        def supports(self, c):
            return True

        def fetch_profile(self, ident, **k):
            raise UnsupportedCapability("not built yet")

        def close(self):
            pass

    class Fallback(Primary):
        name = "unipile"

        def fetch_profile(self, ident, **k):
            return "ok"

    with trace.run(echo=False) as run:
        with run.step("ENRICH"):
            CapabilityRouter(Primary(), Fallback()).perform(
                Capability.PROFILE, "fetch_profile", "x")

    step = run.steps[0]
    assert step.provider == "unipile"
    assert step.fallback_from == "phantombuster"
    attempts = step.fields.get("attempts", [])
    assert any("UNSUPPORTED_CAPABILITY" in a for a in attempts)
    assert any("fell back to unipile" in a for a in attempts)


@pytest.mark.unit
def test_refused_fallback_is_recorded_before_raising() -> None:
    """An auth failure must not fall back — and the trace must say so, rather
    than the run simply ending."""
    from linkedin_agent.providers import Capability, ProviderAuthError
    from linkedin_agent.providers.router import CapabilityRouter

    class Primary:
        name = "phantombuster"

        def supports(self, c):
            return True

        def fetch_profile(self, ident, **k):
            raise ProviderAuthError("cookie expired")

        def close(self):
            pass

    class Fallback(Primary):
        name = "unipile"

        def fetch_profile(self, ident, **k):
            return "ok"

    with trace.run(echo=False) as run:
        with pytest.raises(ProviderAuthError):
            with run.step("ENRICH"):
                CapabilityRouter(Primary(), Fallback()).perform(
                    Capability.PROFILE, "fetch_profile", "x")

    attempts = run.steps[0].fields.get("attempts", [])
    assert any("AUTH_FAILURE" in a and "raised" in a for a in attempts)


@pytest.mark.unit
def test_tracing_never_breaks_routing_when_no_run_is_open() -> None:
    """Production has no trace open. Instrumentation must be inert there."""
    from linkedin_agent.providers import Capability
    from linkedin_agent.providers.router import CapabilityRouter

    class P:
        name = "unipile"

        def supports(self, c):
            return True

        def fetch_profile(self, ident, **k):
            return "ok"

        def close(self):
            pass

    assert trace.current() is None
    assert CapabilityRouter(P(), None).perform(
        Capability.PROFILE, "fetch_profile", "x") == "ok"


# --------------------------- console encoding --------------------------------
# A prospect's post containing U+1F4AF raised UnicodeEncodeError out of print()
# on a cp1252 Windows console and killed the whole run at stage 9 -- after the
# profile and activity scrapes had already been paid for. Observed 2026-09-03
# on a real profile.

class _Cp1252Stdout:
    """A console that rejects what cp1252 cannot represent, as Windows does."""

    encoding = "cp1252"

    def __init__(self):
        self.written = []

    def write(self, text):
        text.encode("cp1252")     # raises exactly as the real console does
        self.written.append(text)
        return len(text)

    def flush(self):
        pass


@pytest.mark.unit
def test_unprintable_characters_do_not_reach_the_console(monkeypatch) -> None:
    monkeypatch.setattr("linkedin_agent.trace.sys.stdout", _Cp1252Stdout())
    out = trace.safe_text("great launch \U0001f4af congrats")
    out.encode("cp1252")          # the assertion: this must not raise
    assert "great launch" in out


@pytest.mark.unit
def test_encodable_text_is_returned_untouched(monkeypatch) -> None:
    """A UTF-8 terminal must lose nothing -- the emoji should still print."""
    class _Utf8:
        encoding = "utf-8"
    monkeypatch.setattr("linkedin_agent.trace.sys.stdout", _Utf8())
    assert trace.safe_text("great launch \U0001f4af") == "great launch \U0001f4af"


@pytest.mark.unit
def test_a_step_survives_an_unprintable_field(monkeypatch, capsys) -> None:
    """The run must continue, not just the string be cleaned."""
    sink = _Cp1252Stdout()
    monkeypatch.setattr("linkedin_agent.trace.sys.stdout", sink)
    run = trace.RunTrace(echo=True)
    with run.step("ACTIVITY_RETRIEVAL") as st:
        st.note("post_0", "shipped it \U0001f4af")
    assert run.steps[-1].status == trace.OK
    assert any("post_0" in w for w in sink.written)
