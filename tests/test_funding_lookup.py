"""Unit tests for linkedin_agent.funding_lookup.

Pure tests — no DB, no CLI. Each test exercises one rule in the scoring
function or one branch in find_founder.
"""

from __future__ import annotations

import pytest

from linkedin_agent.adapters.base import LinkedInAdapter, Post, ProspectHit
from linkedin_agent.funding_lookup import (
    MIN_SCORE,
    _company_variants,
    _is_current_employee,
    _score_candidate,
    check_team,
    find_founder,
    format_hiring_pitch_context,
    format_pitch_context,
)


# ----------------------------- helpers --------------------------------------

def _hit(headline: str, *, name: str = "Test Person",
         url: str = "https://www.linkedin.com/in/test") -> ProspectHit:
    return ProspectHit(linkedin_url=url, full_name=name, headline=headline)


class _StubAdapter(LinkedInAdapter):
    """Minimal adapter that returns a canned list of hits and records the query.

    `hits` can be:
      - a list[ProspectHit]: same list returned for every search
      - a dict[query_substr -> list[ProspectHit]]: returns the list whose key
        is a substring of the query (lets team-check tests vary results per
        sub-query — CTO vs engineer vs founding)
      - an Exception: raises on every search
    """

    def __init__(self, hits):
        self._hits = hits
        self.queries: list[str] = []

    def search(self, query: str, limit: int = 20) -> list[ProspectHit]:
        self.queries.append(query)
        if isinstance(self._hits, Exception):
            raise self._hits
        if isinstance(self._hits, dict):
            for key, hits in self._hits.items():
                if key in query.lower():
                    return hits[:limit]
            return []
        return self._hits[:limit]

    def get_recent_posts(self, linkedin_url: str, limit: int = 5) -> list[Post]:
        return []

    def react(self, post: Post, reaction: str = "LIKE") -> str:
        return ""

    def send_connection(self, linkedin_url: str, note: str | None = None) -> str:
        return ""

    def send_dm(self, linkedin_url: str, body: str) -> str:
        return ""


# ----------------------------- company variants -----------------------------

def test_company_variants_simple() -> None:
    assert _company_variants("Cursor") == ["cursor"]


def test_company_variants_multi_word_generates_hyphen_and_collapsed() -> None:
    variants = _company_variants("Browser Use")
    assert "browser use" in variants
    assert "browser-use" in variants
    assert "browseruse" in variants


def test_company_variants_strips_ai_suffix() -> None:
    variants = _company_variants("Bland AI")
    assert "bland ai" in variants
    assert "bland-ai" in variants
    assert "bland" in variants


def test_company_variants_skips_too_short() -> None:
    # 1-2 char variants would create false-positive substring matches.
    variants = _company_variants("AI")
    assert "ai" not in variants


# ----------------------------- scoring rules --------------------------------

def test_score_strong_attribution() -> None:
    # "CEO @ Acme AI" — single segment with founder/CEO + company.
    # +20 (attribution) + 8 (CEO) = 28
    score, _ = _score_candidate(_hit("CEO @ Acme AI | building agentic accounting"), "Acme AI")
    assert score == 28


def test_score_rejects_no_founder_keyword() -> None:
    score, signals = _score_candidate(_hit("Software Engineer @ Acme"), "Acme")
    assert score == -100
    assert "no founder/CEO keyword" in signals


def test_score_penalizes_cto_in_attribution_segment() -> None:
    # "Co-Founder & CTO @ Acme AI" — attribution segment fires (+20 +5);
    # CTO penalty (-15) applies globally. Total: 10.
    score, signals = _score_candidate(
        _hit("Co-Founder & CTO @ Acme AI"), "Acme AI",
    )
    assert score == 10
    assert "tech role penalty" in signals
    assert "founder role" in signals


def test_score_rejects_pure_engineer() -> None:
    # No founder keyword at all → hard reject.
    score, _ = _score_candidate(_hit("Investor at Sequoia | Building Acme"), "Acme")
    assert score == -100


def test_score_investor_attributed_to_other_co() -> None:
    # "Founder of Acme AI | investor at Sequoia" — attribution segment fires
    # for our company; global "investor at" penalty also fires.
    # +20 +5 - 30 = -5
    score, signals = _score_candidate(
        _hit("Founder of Acme AI | investor at Sequoia"), "Acme AI",
    )
    assert score == -5
    assert "non-founder role penalty" in signals
    assert score < MIN_SCORE


def test_score_no_attribution_when_company_not_in_founder_segment() -> None:
    # "Lovable Ambassador | CEO at 10K Digital" — the Felipe Matos case.
    # Lovable appears in one segment, CEO in another — no attribution.
    # Also "ambassador" triggers the non-founder penalty.
    # Score: 0 (no attribution) - 30 (ambassador) = -30
    score, signals = _score_candidate(
        _hit("CEO at 10K Digital | Lovable Ambassador"), "Lovable",
    )
    assert score < MIN_SCORE
    assert "non-founder role penalty" in signals
    # The +20 attribution bonus must NOT fire.
    assert not any("attributed" in s for s in signals)


def test_score_no_attribution_when_company_mentioned_but_role_different() -> None:
    # "We build AI-powered Notion workspaces | Founder & CEO 🪄" — Tem case.
    # Notion appears in one segment, founder in another. No attribution.
    score, signals = _score_candidate(
        _hit("We build AI-powered Notion workspaces | Founder & CEO 🪄"),
        "Notion",
    )
    assert score < MIN_SCORE
    assert not any("attributed" in s for s in signals)


def test_score_attribution_with_hyphen_variant() -> None:
    # Magnus Müller case: company "Browser Use" but headline says "browser-use".
    score, signals = _score_candidate(
        _hit("Founder of browser-use (YC W25)"), "Browser Use",
    )
    assert score >= MIN_SCORE
    assert any("attributed" in s for s in signals)


def test_score_attribution_with_stripped_suffix() -> None:
    # Isaiah Granet case: company "Bland AI" but headline says "@ Bland".
    score, signals = _score_candidate(
        _hit("Co Founder @ Bland"), "Bland AI",
    )
    assert score >= MIN_SCORE
    assert any("attributed" in s for s in signals)


def test_score_no_match_when_company_absent() -> None:
    # Founder, but company name nowhere in headline.
    score, signals = _score_candidate(_hit("Founder of stealth startup"), "Acme")
    assert score == 0
    assert score < MIN_SCORE
    assert not any("attributed" in s for s in signals)


def test_score_whole_word_match_avoids_substring_false_positive() -> None:
    # "Better" should NOT match inside "betterment" (no word boundary).
    score, _ = _score_candidate(
        _hit("Founder of betterment platforms"), "Better",
    )
    assert score == 0  # no attribution


# ----------------------------- find_founder ---------------------------------

def test_find_founder_returns_top_match() -> None:
    adapter = _StubAdapter([
        _hit("CTO @ Acme AI", name="Tech Guy"),
        _hit("CEO and Co-Founder @ Acme AI", name="Real Founder"),
        _hit("Software Engineer at Acme AI", name="Eng"),
    ])
    match = find_founder(adapter, "Acme AI")
    assert match is not None
    assert match.hit.full_name == "Real Founder"
    assert match.score >= MIN_SCORE


def test_find_founder_returns_below_threshold_when_only_weak() -> None:
    adapter = _StubAdapter([
        _hit("Founder of stealth startup"),
        _hit("CEO of unrelated thing"),
    ])
    match = find_founder(adapter, "Acme AI")
    assert match is not None
    assert match.score < MIN_SCORE


def test_find_founder_returns_None_when_all_rejected() -> None:
    adapter = _StubAdapter([
        _hit("Software Engineer at Acme AI"),
        _hit("Talent acquisition @ Acme AI"),
    ])
    match = find_founder(adapter, "Acme AI")
    assert match is None


def test_find_founder_returns_None_on_no_hits() -> None:
    adapter = _StubAdapter([])
    match = find_founder(adapter, "Acme AI")
    assert match is None


def test_find_founder_handles_search_exception() -> None:
    adapter = _StubAdapter(RuntimeError("network broke"))
    match = find_founder(adapter, "Acme AI")
    assert match is None


def test_find_founder_passes_company_in_query() -> None:
    adapter = _StubAdapter([])
    find_founder(adapter, "Acme AI")
    assert adapter.queries == ['"Acme AI" founder']


# ----------------------------- pitch_context --------------------------------

def test_format_pitch_context_full() -> None:
    out = format_pitch_context(
        "Acme AI", "seed", "$2.5M", "Sequoia, a16z",
        "AI agent for SMB accounting",
    )
    assert out == (
        "Recently closed seed $2.5M from Sequoia, a16z. "
        "Building Acme AI — AI agent for SMB accounting."
    )


def test_format_pitch_context_company_only() -> None:
    out = format_pitch_context("Acme AI", None, None, None, None)
    assert out == "Recently funded. Building Acme AI."


def test_format_pitch_context_round_only() -> None:
    out = format_pitch_context("Acme AI", "seed", None, None, None)
    assert out == "Recently closed seed. Building Acme AI."


def test_format_pitch_context_amount_only() -> None:
    out = format_pitch_context("Acme AI", None, "$2M", None, None)
    assert out == "Recently closed $2M. Building Acme AI."


def test_format_pitch_context_investors_no_round() -> None:
    out = format_pitch_context("Acme AI", None, None, "Sequoia", None)
    assert out == "Recently funded from Sequoia. Building Acme AI."


# ----------------------------- format_hiring_pitch_context -----------------

def test_format_hiring_pitch_context_full() -> None:
    out = format_hiring_pitch_context(
        "Acme AI", "first engineer", "today",
        "AI-native accounting for SMBs",
    )
    assert out == (
        "Hiring first engineer (posted today). "
        "Building Acme AI — AI-native accounting for SMBs."
    )


def test_format_hiring_pitch_context_role_only() -> None:
    out = format_hiring_pitch_context("Acme AI", "first engineer", None, None)
    assert out == "Hiring first engineer. Building Acme AI."


def test_format_hiring_pitch_context_minimal() -> None:
    out = format_hiring_pitch_context("Acme AI", None, None, None)
    assert out == "Hiring engineering. Building Acme AI."


def test_format_hiring_pitch_context_posted_without_role_drops_posted() -> None:
    # No role → can't say "Hiring X (posted Y)" coherently. The "(posted Y)"
    # piece is anchored to the role; without a role we fall back to the
    # generic phrase and drop the posted date silently.
    out = format_hiring_pitch_context("Acme AI", None, "yesterday", None)
    assert out == "Hiring engineering. Building Acme AI."


def test_format_hiring_pitch_context_role_with_description() -> None:
    out = format_hiring_pitch_context(
        "Acme AI", "senior engineer", None, "B2B SaaS for retailers",
    )
    assert out == "Hiring senior engineer. Building Acme AI — B2B SaaS for retailers."


# ----------------------------- _is_current_employee -------------------------

def test_current_employee_lead_position() -> None:
    # Company in first segment, no past markers → current.
    assert _is_current_employee("cofounder @ hardline | construction ops", ["hardline"])


def test_current_employee_with_only_past_marker_after() -> None:
    # Headline like "CEO at Acme | Previously: Stanford" — Acme is current
    # (appears BEFORE the past marker).
    assert _is_current_employee(
        "ceo at acme | previously: stanford ms", ["acme"],
    )


def test_current_employee_company_after_past_marker() -> None:
    # Zachary Crockett's case — Hardline is mentioned only in a "Previously"
    # block. Must not be treated as current.
    assert not _is_current_employee(
        "sr. engineering manager at sofar ocean. previously: founder & cto at "
        "particle and hardline",
        ["hardline"],
    )


def test_current_employee_ex_prefix() -> None:
    # "Ex-CTO at Acme" → past role.
    assert not _is_current_employee("ex-cto at acme | building beta", ["acme"])


def test_current_employee_company_absent() -> None:
    assert not _is_current_employee("ceo at unrelated", ["acme"])


# ----------------------------- check_team -----------------------------------

def test_check_team_no_employees() -> None:
    adapter = _StubAdapter([])
    result = check_team(adapter, "Acme")
    assert result.disqualification is None
    assert result.cto_found is False
    assert result.builder_engineers == []
    assert result.employees_seen == 0


def test_check_team_finds_cto_disqualifies() -> None:
    adapter = _StubAdapter({
        "cto": [_hit("Co-Founder & CTO @ Acme", name="Tech Cofounder")],
        "engineer": [],
        "founding": [],
    })
    result = check_team(adapter, "Acme")
    assert result.disqualification is not None
    assert "Tech Cofounder" in result.disqualification
    assert result.cto_found is True


def test_check_team_two_engineers_disqualifies() -> None:
    adapter = _StubAdapter({
        "engineer": [
            _hit("Software Engineer @ Acme",
                 name="Eng One", url="https://www.linkedin.com/in/e1"),
            _hit("ML Engineer @ Acme",
                 name="Eng Two", url="https://www.linkedin.com/in/e2"),
        ],
    })
    result = check_team(adapter, "Acme")
    assert result.disqualification is not None
    assert "eng team (2" in result.disqualification


def test_check_team_one_engineer_passes_with_warning() -> None:
    adapter = _StubAdapter({
        "engineer": [
            _hit("Senior Engineer @ Acme",
                 name="Sole Eng", url="https://www.linkedin.com/in/e1"),
        ],
    })
    result = check_team(adapter, "Acme")
    assert result.disqualification is None
    assert result.builder_engineers == ["Sole Eng"]
    assert result.employees_seen == 1


def test_check_team_excludes_founder() -> None:
    founder_url = "https://www.linkedin.com/in/founder"
    adapter = _StubAdapter({
        "engineer": [
            _hit("Founder & CEO @ Acme | also engineer at heart",
                 name="The Founder", url=founder_url),
        ],
    })
    result = check_team(adapter, "Acme", founder_url=founder_url)
    # Founder must not be double-counted as an employee.
    assert result.employees_seen == 0
    assert result.disqualification is None


def test_check_team_past_employee_doesnt_count() -> None:
    adapter = _StubAdapter({
        "cto": [
            _hit("Sr. Eng Manager at Other Co. Previously: Founder & CTO at Acme",
                 name="Past CTO", url="https://www.linkedin.com/in/past"),
        ],
    })
    result = check_team(adapter, "Acme")
    assert result.disqualification is None
    assert result.cto_found is False


def test_check_team_sales_engineer_doesnt_count_as_builder() -> None:
    adapter = _StubAdapter({
        "engineer": [
            _hit("Sales Engineer @ Acme | helping customers",
                 name="Sales Eng", url="https://www.linkedin.com/in/sales"),
            _hit("Customer Engineer @ Acme",
                 name="CS Eng", url="https://www.linkedin.com/in/cs"),
        ],
    })
    result = check_team(adapter, "Acme")
    # 2 results seen but neither is a builder; no disqualification.
    assert result.disqualification is None
    assert result.builder_engineers == []
    assert result.employees_seen == 2


def test_check_team_search_exception_doesnt_propagate() -> None:
    adapter = _StubAdapter(RuntimeError("network broke"))
    # Should not raise — each sub-query failure swallowed.
    result = check_team(adapter, "Acme")
    assert result.disqualification is None
    assert result.employees_seen == 0
