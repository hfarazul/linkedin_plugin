"""Run-scoped pipeline tracing.

A prospect passes through a dozen boundaries — provider calls, normalization,
DB writes, scoring gates, drafting — and until now each logged in its own style
or not at all. Following one prospect end to end meant reading four modules'
log lines and inferring the joins.

This gives every run a correlation id and every stage a uniform record: what it
was, which provider and capability served it, how long it took, what it
produced, and — the part that actually matters when something looks wrong — why
a record was accepted, skipped or rejected.

Two design choices worth stating:

**A module-level current run.** The router and providers record decisions
without a trace object being threaded through every signature. This is a
single-threaded CLI, so a module global is honest and keeps the instrumentation
from leaking into every function's parameters.

**Redaction is centralised and applied on the way in.** A trace is diagnostic
output, which means it gets pasted into tickets and chat. A Phantom's arguments
carry a LinkedIn session cookie; Telegram's API URLs carry the bot token. Both
have already leaked from this codebase once. Values are scrubbed as they are
recorded, so a caller cannot forget to.
"""

from __future__ import annotations

import re
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


def safe_text(value: str) -> str:
    """Replace characters the attached console cannot encode.

    Windows consoles default to cp1252, and a single emoji in a scraped post
    is enough to raise UnicodeEncodeError out of print(). Observed 2026-09-03:
    a prospect whose post contained U+1F4AF killed an otherwise healthy run at
    stage 9, after the profile and activity scrapes had already been paid for.

    Reconfiguring stdout to UTF-8 was tried earlier in this project and is
    worse -- it moves the failure into whatever is reading our output.
    Sanitising per write keeps the loss to the one character that cannot be
    displayed, and only on terminals that cannot display it.
    """
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        value.encode(enc)
        return value
    except (UnicodeEncodeError, LookupError):
        return value.encode(enc, errors="replace").decode(enc, errors="replace")


def say(text: str = "") -> None:
    """print() that cannot kill a run over an unprintable character."""
    print(safe_text(str(text)))



# ------------------------------------------------------------------ redaction

# Keys whose value is a credential regardless of what it looks like.
_SECRET_KEYS = {
    "sessioncookie", "cookie", "liat", "li_at", "password", "token",
    "apikey", "api_key", "secret", "accesstoken", "authorization",
    "xapikey", "bottoken", "telegrambottoken", "phantombusterapikey",
    "unipileapikey", "smtppassword", "emailpassword",
}

# Value shapes that are credentials wherever they appear.
_SECRET_PATTERNS = (
    re.compile(r"/bot\d{6,}:[A-Za-z0-9_-]{20,}"),           # telegram api url
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),          # telegram token
    re.compile(r"\bAQED[A-Za-z0-9_-]{20,}"),                 # linkedin li_at
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"),        # bearer header
)

_REDACTED = "<redacted>"


def _looks_secret(key: str) -> bool:
    normalised = re.sub(r"[^a-z]", "", str(key).lower())
    return normalised in {re.sub(r"[^a-z]", "", k) for k in _SECRET_KEYS}


def redact(value: Any, *, key: str | None = None) -> Any:
    """Scrub credentials out of anything before it is recorded or printed.

    Recurses into dicts and lists because provider payloads nest, and a cookie
    two levels down is exactly as sensitive as one at the top.
    """
    if key is not None and _looks_secret(key):
        length = len(str(value)) if value is not None else 0
        return f"<redacted, {length} chars>"
    if isinstance(value, dict):
        return {k: redact(v, key=k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        out = value
        for pattern in _SECRET_PATTERNS:
            out = pattern.sub(_REDACTED, out)
        return out
    return value


# -------------------------------------------------------------- error classes

def classify_error(exc: BaseException) -> str:
    """Name the failure so a reader knows whether to retry, reconfigure, or
    look at our own code. Deliberately distinguishes the cases the router
    routes on — an unsupported capability and an auth failure look identical
    in a stack trace but mean opposite things."""
    from .providers.base import (
        ActionFailed, MalformedResponse, ProviderAuthError,
        ProviderRateLimited, ProviderTimeout, UnsupportedCapability,
    )
    if isinstance(exc, UnsupportedCapability):
        return "UNSUPPORTED_CAPABILITY"
    if isinstance(exc, ProviderAuthError):
        return "AUTH_FAILURE"
    if isinstance(exc, ProviderRateLimited):
        return "RATE_LIMIT"
    if isinstance(exc, ProviderTimeout):
        return "TIMEOUT"
    if isinstance(exc, MalformedResponse):
        return "MALFORMED_RESPONSE"
    if isinstance(exc, ActionFailed):
        return "PROVIDER_ACTION_FAILED"
    name = type(exc).__name__
    if "Integrity" in name or "OperationalError" in name or "sqlite" in name.lower():
        return "DATABASE_FAILURE"
    if isinstance(exc, (ValueError, AssertionError)):
        return "VALIDATION_FAILURE"
    return "PROVIDER_EXECUTION_FAILURE"


# ------------------------------------------------------------------- records

OK, FAIL, SKIP, MOCK = "OK", "FAIL", "SKIP", "MOCK"

# How a stage was executed, so a summary can distinguish real evidence from
# scaffolding. A green run made entirely of stubs proves nothing.
LIVE, MOCKED, STUBBED, NOT_IMPLEMENTED = "LIVE", "MOCKED", "STUBBED", "NOT_IMPLEMENTED"


@dataclass
class Step:
    index: int
    name: str
    status: str = OK
    mode: str = LIVE
    provider: str | None = None
    capability: str | None = None
    duration_ms: float = 0.0
    fields: dict = field(default_factory=dict)
    reason: str | None = None
    error_class: str | None = None
    error: str | None = None
    fallback_from: str | None = None

    def note(self, key: str, value: Any) -> None:
        """Record one output field. Redacted on the way in."""
        self.fields[key] = redact(value, key=key)

    def why(self, reason: str) -> None:
        """Why this record was accepted, skipped or rejected."""
        self.reason = reason


@dataclass
class RunTrace:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    total: int | None = None
    steps: list[Step] = field(default_factory=list)
    echo: bool = True
    started_at: float = field(default_factory=time.monotonic)

    # ------------------------------------------------------------- recording

    @contextmanager
    def step(self, name: str, *, mode: str = LIVE,
             capability: str | None = None) -> Iterator[Step]:
        current = Step(index=len(self.steps) + 1, name=name, mode=mode,
                       capability=capability)
        self.steps.append(current)
        counter = f"{current.index:02d}/{self.total:02d}" if self.total else f"{current.index:02d}"
        if self.echo:
            say(f"\n[{counter}] {name}" + (f"  ({mode})" if mode != LIVE else ""))
        start = time.monotonic()
        try:
            yield current
        except Exception as exc:
            current.duration_ms = (time.monotonic() - start) * 1000
            current.status = FAIL
            current.error_class = classify_error(exc)
            current.error = redact(str(exc))[:400]
            if self.echo:
                self._echo(current)
            raise
        current.duration_ms = (time.monotonic() - start) * 1000
        if self.echo:
            self._echo(current)

    def _echo(self, step: Step) -> None:
        for key, value in step.fields.items():
            rendered = value if isinstance(value, str) else repr(value)
            if len(str(rendered)) > 160:
                rendered = str(rendered)[:157] + "..."
            say(f"     {key}={rendered}")
        if step.provider:
            line = f"     provider={step.provider}"
            if step.capability:
                line += f" capability={step.capability}"
            if step.fallback_from:
                line += f"  (FELL BACK from {step.fallback_from})"
            say(line)
        if step.reason:
            say(f"     reason: {step.reason}")
        mark = {OK: "OK", FAIL: "FAILED", SKIP: "SKIPPED", MOCK: "MOCK"}[step.status]
        if step.status == FAIL:
            say(f"  x  {mark}  [{step.error_class}] {step.error}")
        else:
            say(f"  -  {mark}  ({step.duration_ms:.0f}ms)")

    # ------------------------ called by the router, not by pipeline code ----

    def record_attempt(self, capability: str, provider: str, *,
                       error_class: str, detail: str,
                       decision: str) -> None:
        """A provider tried and failed. Recorded on the open step.

        Without this the trace would show only the provider that eventually
        answered, and a fallback would look like a clean first-try success —
        exactly the silent switching that makes production undebuggable.
        """
        if not self.steps:
            return
        current = self.steps[-1]
        attempts = current.fields.setdefault("attempts", [])
        attempts.append(f"{provider} -> {error_class}: {redact(detail)[:120]} "
                        f"[{decision}]")

    def record_route(self, capability: str, provider: str,
                     *, fallback_from: str | None = None) -> None:
        """The router reports which provider actually served a capability.

        Recorded on the step that is currently open, so routing shows up
        against the stage that triggered it rather than in a separate stream.
        """
        if not self.steps:
            return
        current = self.steps[-1]
        current.provider = provider
        current.capability = capability
        if fallback_from:
            current.fallback_from = fallback_from

    # -------------------------------------------------------------- summary

    def failed(self) -> list[Step]:
        return [s for s in self.steps if s.status == FAIL]

    def counts(self) -> dict:
        return {
            "live": sum(1 for s in self.steps if s.mode == LIVE and s.status != SKIP),
            "mocked": sum(1 for s in self.steps if s.mode in (MOCKED, STUBBED)),
            "not_implemented": sum(1 for s in self.steps if s.mode == NOT_IMPLEMENTED),
            "skipped": sum(1 for s in self.steps if s.status == SKIP),
            "failed": len(self.failed()),
        }

    def summary(self, *, title: str = "E2E TEST SUMMARY",
                header: dict | None = None) -> str:
        bar = "=" * 64
        out = [bar, title, bar, f"Run ID: {self.run_id}"]
        for key, value in (header or {}).items():
            out.append(f"{key}: {value}")
        out.append("")
        for step in self.steps:
            glyph = {OK: "[ok]  ", FAIL: "[FAIL]", SKIP: "[skip]", MOCK: "[mock]"}[step.status]
            tag = "" if step.mode == LIVE else f"  ({step.mode})"
            line = f"{glyph} {step.name}{tag}"
            if step.provider:
                line += f"  via {step.provider}"
            out.append(line)
            if step.status == FAIL:
                out.append(f"        [{step.error_class}] {step.error}")
            elif step.reason and step.status in (SKIP, MOCK):
                out.append(f"        {step.reason}")
        counts = self.counts()
        elapsed = time.monotonic() - self.started_at
        out += [
            "",
            f"Total time: {elapsed:.1f}s",
            f"Live stages: {counts['live']}",
            f"Mocked/stubbed stages: {counts['mocked']}",
            f"Not implemented: {counts['not_implemented']}",
            f"Failed stages: {counts['failed']}",
        ]
        return "\n".join(out)


# ------------------------------------------------------------- current run

_current: RunTrace | None = None


def current() -> RunTrace | None:
    return _current


@contextmanager
def run(total: int | None = None, *, run_id: str | None = None,
        echo: bool = True) -> Iterator[RunTrace]:
    """Open a traced run. Instrumented code finds it via trace.current()."""
    global _current
    previous = _current
    trace = RunTrace(run_id=run_id or uuid.uuid4().hex[:8], total=total, echo=echo)
    _current = trace
    if echo:
        say(f"[RUN {trace.run_id}]")
    try:
        yield trace
    finally:
        _current = previous
