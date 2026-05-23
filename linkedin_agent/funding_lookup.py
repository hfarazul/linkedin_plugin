"""Founder lookup for the funding-import CLI.

Given a company name (typically from a Crunchbase News funding announcement),
search LinkedIn for the most likely founder/CEO and score the candidates by
how confidently they look like the right person.

Designed to be conservative: a wrong-person import poisons a campaign far more
than a missed company. The threshold (MIN_SCORE = 20) effectively requires the
company name to appear in the headline AND no disqualifying role keywords.

This module is pure: it takes a LinkedInAdapter and a company name, returns a
FounderMatch. The CLI applies the threshold and writes to the DB.
"""

from __future__ import annotations

from dataclasses import dataclass

from .adapters.base import LinkedInAdapter, ProspectHit


# Headlines must contain at least one of these or the candidate is rejected
# outright. "owner" is intentionally omitted — too noisy in practice.
FOUNDER_KEYWORDS = ("founder", "co-founder", "cofounder", "ceo", "chief executive")

# A tech-role headline costs 15 points. Penalty (rather than rejection)
# because some non-tech founders also call themselves "AI engineer" loosely;
# the +20 company-name signal can still outweigh.
TECH_ROLE_KEYWORDS = (
    "cto", "chief technology", "vp engineering", "vp eng",
    "head of engineering", "head of tech", "software engineer",
    "senior engineer", "lead engineer", "principal engineer", "staff engineer",
    "tech lead", "ml engineer", "ai engineer", "data engineer",
    "devops engineer", "full-stack developer", "fullstack developer",
    "backend developer", "frontend developer", "android developer", "ios developer",
)

# Hard "this is the wrong kind of person" signals. -30 means even a perfect
# company-name + founder-role match can't clear the threshold.
NOT_FOUNDER_KEYWORDS = (
    "investor at", "venture partner", "general partner", "managing partner",
    "vc partner", "principal at", "associate at", "scout at",
    "recruiter", "talent acquisition", "headhunter",
    "journalist", "reporter", "writer at",
)

MIN_SCORE = 20


@dataclass
class FounderMatch:
    hit: ProspectHit
    score: int
    signals: list[str]


def _score_candidate(hit: ProspectHit, company: str) -> tuple[int, list[str]]:
    """Score one candidate. Returns (score, signals).

    Returns (-100, ["no founder/CEO keyword"]) for candidates that don't have
    any founder/CEO marker in the headline — these are filtered out before
    sorting so they can never be returned as the top match.
    """
    headline = (hit.headline or "").lower()
    company_lower = company.lower()
    signals: list[str] = []

    if not any(kw in headline for kw in FOUNDER_KEYWORDS):
        return -100, ["no founder/CEO keyword"]

    score = 0

    if company_lower in headline:
        score += 20
        signals.append("company name in headline")

    if "ceo" in headline or "chief executive" in headline:
        score += 8
        signals.append("CEO role")

    if any(kw in headline for kw in ("founder", "co-founder", "cofounder")):
        score += 5
        signals.append("founder role")

    if any(kw in headline for kw in TECH_ROLE_KEYWORDS):
        score -= 15
        signals.append("tech role penalty")

    if any(kw in headline for kw in NOT_FOUNDER_KEYWORDS):
        score -= 30
        signals.append("non-founder role penalty")

    return score, signals


def find_founder(
    adapter: LinkedInAdapter,
    company: str,
    *,
    limit: int = 10,
) -> FounderMatch | None:
    """Search LinkedIn for the founder of `company` and return the
    highest-scoring candidate that survives the founder-keyword filter.

    Returns None if the search returned nothing, errored, or every candidate
    was rejected for lacking a founder/CEO keyword. The caller is responsible
    for the score >= MIN_SCORE threshold check — keeping that decision in the
    CLI lets us also show the top score when it's below threshold ("top score
    9, threshold 20").
    """
    try:
        hits = adapter.search(f'"{company}" founder', limit=limit)
    except Exception:
        return None

    if not hits:
        return None

    scored: list[FounderMatch] = []
    for h in hits:
        score, signals = _score_candidate(h, company)
        if score == -100:
            continue
        scored.append(FounderMatch(hit=h, score=score, signals=signals))

    if not scored:
        return None

    scored.sort(key=lambda m: m.score, reverse=True)
    return scored[0]


def format_pitch_context(
    company: str,
    round_type: str | None,
    amount: str | None,
    investors: str | None,
    description: str | None,
) -> str:
    """Build the pitch_context string the drafter will reference.

    Examples:
      All fields:   "Recently closed seed $2.5M from Sequoia, a16z.
                     Building Acme AI — AI agent for SMB accounting."
      Round only:   "Recently closed seed. Building Acme AI."
      Just company: "Recently funded. Building Acme AI."
    """
    funding_phrase = "Recently funded"
    if round_type or amount:
        parts = [p for p in (round_type, amount) if p]
        funding_phrase = f"Recently closed {' '.join(parts)}"
    if investors:
        funding_phrase += f" from {investors}"
    base = f"{funding_phrase}. Building {company}"
    if description:
        base += f" — {description}"
    return base + "."
