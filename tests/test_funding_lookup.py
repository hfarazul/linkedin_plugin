"""Unit tests for linkedin_agent.funding_lookup.

Pure tests — no DB, no CLI. Each test exercises one rule in the scoring
function or one branch in find_founder.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from linkedin_agent.adapters.base import LinkedInAdapter, Post, ProspectHit
from linkedin_agent.funding_lookup import (
    MIN_SCORE,
    FounderMatch,
    _score_candidate,
    find_founder,
    format_pitch_context,
)


# ----------------------------- helpers --------------------------------------

def _hit(headline: str, *, name: str = "Test Person",
         url: str = "https://www.linkedin.com/in/test") -> ProspectHit:
    return ProspectHit(linkedin_url=url, full_name=name, headline=headline)


class _StubAdapter(LinkedInAdapter):
    """Minimal adapter that returns a canned list of hits and records the query."""

    def __init__(self, hits: list[ProspectHit] | Exception):
        self._hits = hits
        self.queries: list[str] = []

    def search(self, query: str, limit: int = 20) -> list[ProspectHit]:
        self.queries.append(query)
        if isinstance(self._hits, Exception):
            raise self._hits
        return self._hits[:limit]

    # Unused abstract methods for testing — return empty / raise.
    def get_recent_posts(self, linkedin_url: str, limit: int = 5) -> list[Post]:
        return []

    def react(self, post: Post, reaction: str = "LIKE") -> str:
        return ""

    def send_connection(self, linkedin_url: str, note: str | None = None) -> str:
        return ""

    def send_dm(self, linkedin_url: str, body: str) -> str:
        return ""


# ----------------------------- scoring rules --------------------------------

def test_score_candidate_strong_match() -> None:
    score, _ = _score_candidate(_hit("CEO @ Acme AI | building agentic accounting"), "Acme AI")
    # company name (+20) + CEO (+8) = 28
    assert score >= 28


def test_score_candidate_rejects_no_founder_role() -> None:
    score, signals = _score_candidate(_hit("Software Engineer @ Acme"), "Acme")
    assert score == -100
    assert "no founder/CEO keyword" in signals


def test_score_candidate_penalizes_cto() -> None:
    # CTO of Acme AI: founder/CEO check fails on "cto" alone — so this would
    # actually be rejected outright (CTO doesn't contain "founder" or "ceo").
    # But CTO + Founder in the headline: company (+20) + founder (+5) + tech (-15) = 10
    score, signals = _score_candidate(
        _hit("Co-Founder & CTO @ Acme AI"), "Acme AI",
    )
    assert score == 10
    assert "tech role penalty" in signals
    assert "founder role" in signals
    assert "company name in headline" in signals


def test_score_candidate_rejects_investor() -> None:
    # "Investor at Sequoia | Building Acme as side project" — has CEO/Founder? No.
    # So it's rejected at the founder-keyword gate (-100), not by the
    # investor penalty. The penalty path is for when someone is *both* a
    # founder *and* an investor — rare but covered below.
    score, signals = _score_candidate(
        _hit("Investor at Sequoia | Building Acme as side project"),
        "Acme",
    )
    assert score == -100


def test_score_candidate_investor_with_founder_keyword_penalized() -> None:
    # Founder of Acme AI but also "investor at" Sequoia in headline:
    # company (+20) + founder (+5) - non-founder (-30) = -5
    score, signals = _score_candidate(
        _hit("Founder of Acme AI | investor at Sequoia"),
        "Acme AI",
    )
    assert score == -5
    assert "non-founder role penalty" in signals
    assert score < MIN_SCORE


def test_score_candidate_no_company_in_headline() -> None:
    # Founder, but company name absent — just +5 founder bonus.
    score, _ = _score_candidate(_hit("Founder of stealth startup"), "Acme")
    assert score == 5
    assert score < MIN_SCORE


# ----------------------------- find_founder ---------------------------------

def test_find_founder_returns_top_match() -> None:
    adapter = _StubAdapter([
        _hit("CTO @ Acme AI", name="Tech Guy"),               # score 10 (founder+company-tech)
        _hit("CEO and Co-Founder @ Acme AI", name="Founder"),  # score 33 (founder+ceo+company)
        _hit("Software Engineer at Acme AI", name="Eng"),     # rejected
    ], )
    match = find_founder(adapter, "Acme AI")
    assert match is not None
    assert match.hit.full_name == "Founder"
    assert match.score >= MIN_SCORE


def test_find_founder_returns_below_threshold_match_when_only_weak_candidates() -> None:
    # find_founder returns the top non-rejected candidate; threshold check
    # lives in the CLI so it can show the top score in the error message.
    adapter = _StubAdapter([
        _hit("Founder of stealth startup"),  # score 5 — below threshold
        _hit("CEO of unrelated thing"),      # score 8 — below threshold
    ])
    match = find_founder(adapter, "Acme AI")
    assert match is not None
    assert match.score < MIN_SCORE


def test_find_founder_returns_None_when_all_rejected() -> None:
    adapter = _StubAdapter([
        _hit("Software Engineer at Acme AI"),
        _hit("Recruiter @ Acme AI"),  # no founder keyword
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
    # The spec says the query is f'"{company}" founder' — phrase-search the
    # company name so Unipile's classic search treats it as a unit.
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
