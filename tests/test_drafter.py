"""Tests for the drafter module.

Unit tests stub `_invoke_claude` so we can verify the prompt-building,
output cleaning, and validation logic without actually invoking Claude Code.
"""

from __future__ import annotations

import pytest

from linkedin_agent import drafter
from linkedin_agent.drafter import DrafterError, DrafterInput, INSUFFICIENT


# ===== output cleanup =======================================================

@pytest.mark.unit
def test_clean_output_strips_code_fences():
    assert drafter._clean_output("```\nhello there\n```") == "hello there"
    assert drafter._clean_output("```text\nhello there\n```") == "hello there"


@pytest.mark.unit
def test_clean_output_strips_surrounding_quotes():
    assert drafter._clean_output('"hello there"') == "hello there"
    assert drafter._clean_output("'hello there'") == "hello there"


@pytest.mark.unit
def test_clean_output_strips_whitespace():
    assert drafter._clean_output("\n   hello there   \n") == "hello there"


@pytest.mark.unit
def test_clean_output_preserves_inner_quotes():
    """Don't unwrap quotes that aren't symmetric."""
    assert drafter._clean_output('she said "hi" to me') == 'she said "hi" to me'


# ===== prompt rendering =====================================================

@pytest.mark.unit
def test_render_prompt_includes_brief_and_profile():
    inp = DrafterInput(
        kind="dm1",
        campaign={"name": "AI Dev Pod", "target_icp": "Series A founders", "brief": "We build AI dev pods."},
        prospect={"full_name": "Jane Smith", "first_name": "Jane",
                  "headline": "CTO @ Acme", "company": "Acme", "title": "CTO",
                  "pitch_context": "Mentioned hiring struggles"},
        recent_posts=[{"text": "We're scaling fast", "posted_at": "2026-05-10"}],
        prior_messages=[],
    )
    prompt = drafter.render_prompt(inp)
    # Subagent system prompt body is present
    assert "no spam tells" in prompt.lower() or "spam-detection" in prompt.lower()
    # Context payload is JSON-embedded
    assert "Jane Smith" in prompt
    assert "AI Dev Pod" in prompt
    assert "We build AI dev pods" in prompt
    assert "We're scaling fast" in prompt
    assert "dm1" in prompt


@pytest.mark.unit
def test_render_prompt_includes_prior_messages_for_dm2():
    inp = DrafterInput(
        kind="dm2",
        campaign={"name": "AI Dev Pod", "target_icp": "founders", "brief": "..."},
        prospect={"full_name": "X Y", "first_name": "X", "headline": "", "company": "",
                  "title": "", "pitch_context": ""},
        recent_posts=[],
        prior_messages=[{"direction": "outbound", "body": "First message I sent",
                         "sent_at": "2026-05-10"}],
    )
    prompt = drafter.render_prompt(inp)
    assert "First message I sent" in prompt


# ===== draft() validation ===================================================

@pytest.mark.unit
def test_draft_rejects_empty_output(monkeypatch, db_env):
    """Stubbed claude returns nothing → DrafterError."""
    from linkedin_agent import db
    pid = db.upsert_prospect("https://www.linkedin.com/in/test", full_name="Test User")

    monkeypatch.setattr(drafter, "_invoke_claude", lambda prompt, timeout=90: "")
    with pytest.raises(DrafterError, match="empty"):
        drafter.draft("dm1", pid)


@pytest.mark.unit
def test_draft_rejects_insufficient_context_marker(monkeypatch, db_env):
    """Stubbed claude returns the canonical INSUFFICIENT_CONTEXT marker."""
    from linkedin_agent import db
    pid = db.upsert_prospect("https://www.linkedin.com/in/test", full_name="Test User")

    monkeypatch.setattr(drafter, "_invoke_claude", lambda prompt, timeout=90: INSUFFICIENT)
    with pytest.raises(DrafterError, match="INSUFFICIENT_CONTEXT"):
        drafter.draft("dm1", pid)


@pytest.mark.unit
def test_draft_gives_up_when_all_attempts_oversize(monkeypatch, db_env):
    """connect_note cap is 300 chars. Stub returns oversize 3x → DrafterError
    after exhausting retries."""
    from linkedin_agent import db
    pid = db.upsert_prospect("https://www.linkedin.com/in/test", full_name="Test User")

    oversize = "a" * 400
    monkeypatch.setattr(drafter, "_invoke_claude", lambda prompt, timeout=90: oversize)
    with pytest.raises(DrafterError, match="all 3 drafter attempts failed"):
        drafter.draft("connect_note", pid)


@pytest.mark.unit
def test_draft_returns_cleaned_body_on_success(monkeypatch, db_env):
    """First-attempt success: stub returns a well-formed body of the right
    length. _clean_output strips code fences."""
    from linkedin_agent import db
    pid = db.upsert_prospect("https://www.linkedin.com/in/test", full_name="Test User")

    # ~250 chars: well within dm1 caps (200 min, 600 max)
    body = (
        "hey there, quick question for you — your recent post about "
        "shipping faster than the market hit a nerve. Curious if "
        "you've tried pairing senior engineering with AI tooling on "
        "your v1, or if you're still riding the in-house hiring path."
    )
    monkeypatch.setattr(
        drafter,
        "_invoke_claude",
        lambda prompt, timeout=90: f"```\n{body}\n```",
    )
    result = drafter.draft("dm1", pid)
    assert result == body


@pytest.mark.unit
def test_draft_rejects_invalid_kind(db_env):
    from linkedin_agent import db
    pid = db.upsert_prospect("https://www.linkedin.com/in/test", full_name="Test User")
    with pytest.raises(DrafterError, match="invalid kind"):
        drafter.draft("invalid_kind_name", pid)


@pytest.mark.unit
def test_draft_raises_when_prospect_missing(db_env):
    """Drafter rejects unknown prospect_id."""
    with pytest.raises(DrafterError, match="not found"):
        drafter.draft("dm1", 9999)


# ===== build_input ==========================================================

@pytest.mark.unit
def test_build_input_uses_no_campaign_when_unattached(db_env):
    from linkedin_agent import db
    pid = db.upsert_prospect("https://www.linkedin.com/in/no-camp",
                              full_name="Solo Prospect",
                              headline="independent")
    inp = drafter.build_input("dm1", pid)
    assert inp.campaign["name"] == "(no campaign)"
    assert inp.prospect["full_name"] == "Solo Prospect"
    assert inp.prospect["first_name"] == "Solo"


# ===== retry logic ==========================================================

class _StubInvoker:
    """Returns a queue of responses to _invoke_claude — one per attempt.
    Useful for testing the retry loop without burning real claude -p calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []   # records (prompt) for assertion

    def __call__(self, prompt, timeout=90):
        self.calls.append(prompt)
        if not self.responses:
            raise AssertionError("test stub ran out of responses — drafter retried more than expected")
        return self.responses.pop(0)


@pytest.mark.unit
def test_draft_retries_on_oversize_and_succeeds(monkeypatch, db_env):
    from linkedin_agent import db
    pid = db.upsert_prospect("https://www.linkedin.com/in/test", full_name="Test User")

    # First attempt: 350 chars (over the 300 cap). Second: a clean 200-char body.
    oversize = "X" * 350
    clean = "Specific reference. " * 8 + "Worth a chat?"   # ~180 chars
    assert len(clean) <= drafter.KIND_MAX_CHARS["connect_note"]
    assert len(clean) >= drafter.KIND_MIN_CHARS["connect_note"]

    stub = _StubInvoker([oversize, clean])
    monkeypatch.setattr(drafter, "_invoke_claude", stub)

    result = drafter.draft("connect_note", pid)
    assert result == clean
    assert len(stub.calls) == 2
    # Second prompt should contain the targeted retry hint
    assert "tighter" in stub.calls[1].lower() or "cap" in stub.calls[1].lower()


@pytest.mark.unit
def test_draft_retries_on_too_short_and_succeeds(monkeypatch, db_env):
    from linkedin_agent import db
    pid = db.upsert_prospect("https://www.linkedin.com/in/test", full_name="Test User")

    too_short = "Hi there."       # 9 chars, below 100-char min for connect_note
    clean = "X" * 200             # well within the band
    stub = _StubInvoker([too_short, clean])
    monkeypatch.setattr(drafter, "_invoke_claude", stub)

    result = drafter.draft("connect_note", pid)
    assert result == clean
    assert len(stub.calls) == 2
    assert "minimum" in stub.calls[1].lower() or "substantive" in stub.calls[1].lower()


@pytest.mark.unit
def test_draft_retries_on_empty_output(monkeypatch, db_env):
    from linkedin_agent import db
    pid = db.upsert_prospect("https://www.linkedin.com/in/test", full_name="Test User")

    clean = "X" * 200
    stub = _StubInvoker(["", clean])
    monkeypatch.setattr(drafter, "_invoke_claude", stub)

    result = drafter.draft("connect_note", pid)
    assert result == clean
    assert len(stub.calls) == 2


@pytest.mark.unit
def test_draft_gives_up_after_max_attempts(monkeypatch, db_env):
    from linkedin_agent import db
    pid = db.upsert_prospect("https://www.linkedin.com/in/test", full_name="Test User")

    # All 3 attempts return oversize content
    oversize = "X" * 400
    stub = _StubInvoker([oversize, oversize, oversize])
    monkeypatch.setattr(drafter, "_invoke_claude", stub)

    with pytest.raises(drafter.DrafterError, match="all 3 drafter attempts failed"):
        drafter.draft("connect_note", pid)
    assert len(stub.calls) == 3


@pytest.mark.unit
def test_draft_does_not_retry_on_insufficient_context(monkeypatch, db_env):
    """INSUFFICIENT_CONTEXT is the drafter being honest — no retry."""
    from linkedin_agent import db
    pid = db.upsert_prospect("https://www.linkedin.com/in/test", full_name="Test User")

    stub = _StubInvoker([drafter.INSUFFICIENT, "X" * 200])   # second response should never run
    monkeypatch.setattr(drafter, "_invoke_claude", stub)

    with pytest.raises(drafter.DrafterError, match="INSUFFICIENT_CONTEXT"):
        drafter.draft("connect_note", pid)
    assert len(stub.calls) == 1   # exactly one call, no retry


@pytest.mark.unit
def test_draft_first_attempt_clean_does_not_retry(monkeypatch, db_env):
    """Sanity: don't retry when the first attempt is fine."""
    from linkedin_agent import db
    pid = db.upsert_prospect("https://www.linkedin.com/in/test", full_name="Test User")

    clean = "X" * 200
    stub = _StubInvoker([clean, "extra response that should never run"])
    monkeypatch.setattr(drafter, "_invoke_claude", stub)

    result = drafter.draft("connect_note", pid)
    assert result == clean
    assert len(stub.calls) == 1
