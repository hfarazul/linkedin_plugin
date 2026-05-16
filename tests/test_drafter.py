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
def test_draft_rejects_oversize_output_for_connect_note(monkeypatch, db_env):
    """connect_note cap is 300 chars. Stubbed claude returns 400 chars → error."""
    from linkedin_agent import db
    pid = db.upsert_prospect("https://www.linkedin.com/in/test", full_name="Test User")

    oversize = "a" * 400
    monkeypatch.setattr(drafter, "_invoke_claude", lambda prompt, timeout=90: oversize)
    with pytest.raises(DrafterError, match="exceeds 300-char cap"):
        drafter.draft("connect_note", pid)


@pytest.mark.unit
def test_draft_returns_cleaned_body_on_success(monkeypatch, db_env):
    from linkedin_agent import db
    pid = db.upsert_prospect("https://www.linkedin.com/in/test", full_name="Test User")

    monkeypatch.setattr(
        drafter,
        "_invoke_claude",
        lambda prompt, timeout=90: '```\nhey there, quick question for you\n```',
    )
    result = drafter.draft("dm1", pid)
    assert result == "hey there, quick question for you"


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
