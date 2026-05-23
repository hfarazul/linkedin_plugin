"""Founder lookup for the funding-import CLI.

Given a company name (typically from a Crunchbase News funding announcement),
search LinkedIn for the most likely founder/CEO and score the candidates by
how confidently they look like the right person.

Designed to be conservative: a wrong-person import poisons a campaign far more
than a missed company. The threshold (MIN_SCORE = 20) effectively requires the
candidate to claim founder/CEO of *our* company in a single headline segment.

This module is pure: it takes a LinkedInAdapter and a company name, returns a
FounderMatch. The CLI applies the threshold and writes to the DB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .adapters.base import LinkedInAdapter, ProspectHit


# Headlines must contain at least one of these or the candidate is rejected
# outright. "owner" is intentionally omitted — too noisy in practice.
FOUNDER_KEYWORDS = ("founder", "co-founder", "cofounder", "ceo", "chief executive")

# A tech-role headline costs 15 points. Penalty (rather than rejection)
# because some non-tech founders also call themselves "AI engineer" loosely;
# the attribution signal can still outweigh.
TECH_ROLE_KEYWORDS = (
    "cto", "chief technology", "vp engineering", "vp eng",
    "head of engineering", "head of tech", "software engineer",
    "senior engineer", "lead engineer", "principal engineer", "staff engineer",
    "tech lead", "ml engineer", "ai engineer", "data engineer",
    "devops engineer", "full-stack developer", "fullstack developer",
    "backend developer", "frontend developer", "android developer", "ios developer",
)

# Hard "this is the wrong kind of person" signals. -30 means even a perfect
# attribution can't quite clear the threshold.
NOT_FOUNDER_KEYWORDS = (
    "investor at", "venture partner", "general partner", "managing partner",
    "vc partner", "principal at", "associate at", "scout at",
    "recruiter", "talent acquisition", "headhunter",
    "journalist", "reporter", "writer at",
    # "ambassador" / "partner da" — common in promo profiles that mention a
    # company without being its founder.
    "ambassador", "partner da", "partner at",
)

MIN_SCORE = 20

# Common corporate suffixes founders drop when writing their headlines.
# "Bland AI" → "Bland", "Foo Labs" → "Foo", etc. Stripping these gives us a
# more permissive variant to match against.
_STRIPPABLE_SUFFIXES = (" ai", " labs", " inc", " co", " technologies", " hq")

# LinkedIn headline separators. Split on these to isolate individual claims —
# attribution scoring only counts claims where founder/CEO + company appear in
# the same segment.
_SEGMENT_SPLIT = re.compile(r"[|·•]|\s\-\s")


@dataclass
class FounderMatch:
    hit: ProspectHit
    score: int
    signals: list[str]


def _company_variants(company: str) -> list[str]:
    """Build lowercase string variants of the company name to try when
    matching against a headline. Founders write company names in lots of
    ways — "Browser Use" might appear as "browser-use" or "browseruse";
    "Bland AI" might appear as just "Bland". We accept any of these as a
    company-match signal.

    Order matters: most-specific variants first so the matcher prefers a
    fuller match (e.g. "browser use" beats "browser") for the signal label.
    """
    base = company.strip().lower()
    if not base:
        return []

    variants: list[str] = []

    def _add(v: str) -> None:
        v = v.strip()
        # Skip too-short variants — single-character or empty strings would
        # match arbitrary substrings and produce false positives.
        if len(v) >= 3 and v not in variants:
            variants.append(v)

    _add(base)

    # Hyphen / space variants — "browser use" ↔ "browser-use" ↔ "browseruse"
    if " " in base:
        _add(base.replace(" ", "-"))
        _add(base.replace(" ", ""))
    if "-" in base:
        _add(base.replace("-", " "))
        _add(base.replace("-", ""))

    # Suffix-stripped variants — "bland ai" → "bland", "foo labs" → "foo".
    # Only generate these from the literal base to avoid combinatorial blowup.
    for suffix in _STRIPPABLE_SUFFIXES:
        if base.endswith(suffix):
            _add(base[: -len(suffix)])

    return variants


def _company_in_text(text: str, variants: list[str]) -> str | None:
    """Return the first variant that appears as a whole word in text, else None."""
    for v in variants:
        pattern = r"\b" + re.escape(v) + r"\b"
        if re.search(pattern, text):
            return v
    return None


def _has_founder_keyword(text: str) -> bool:
    return any(kw in text for kw in FOUNDER_KEYWORDS)


def _score_candidate(hit: ProspectHit, company: str) -> tuple[int, list[str]]:
    """Score one candidate. Returns (score, signals).

    Returns (-100, ["no founder/CEO keyword"]) for candidates that don't have
    any founder/CEO marker anywhere in the headline. These are filtered out
    before sorting so they can never be returned as the top match.

    Otherwise: scan each headline segment (split on |, ·, •, " - ") for a
    segment that BOTH names a founder/CEO role AND contains our company name.
    That's the attribution signal worth +20. Without it, the candidate can't
    clear the threshold — even if the company name appears elsewhere in the
    headline (e.g. "Lovable Ambassador") or the candidate is a founder of
    something else.
    """
    headline = (hit.headline or "").lower()
    variants = _company_variants(company)
    signals: list[str] = []

    if not _has_founder_keyword(headline):
        return -100, ["no founder/CEO keyword"]

    if not variants:
        # Shouldn't happen in practice (company name comes from a CLI arg),
        # but bail safely.
        return 0, ["no company variants to match"]

    score = 0

    # ---- Attribution scoring (segment-bounded) -----------------------------
    segments = [s.strip() for s in _SEGMENT_SPLIT.split(headline) if s.strip()]
    attribution_segment: str | None = None
    matched_variant: str | None = None
    for seg in segments:
        v = _company_in_text(seg, variants)
        if v and _has_founder_keyword(seg):
            attribution_segment = seg
            matched_variant = v
            break

    if attribution_segment:
        score += 20
        signals.append(f"founder/CEO of {matched_variant!r} (attributed)")
        if "ceo" in attribution_segment or "chief executive" in attribution_segment:
            score += 8
            signals.append("CEO role")
        if any(kw in attribution_segment for kw in ("founder", "co-founder", "cofounder")):
            score += 5
            signals.append("founder role")

    # ---- Global penalties (apply regardless of attribution) ----------------
    # Tech-role check uses word boundaries to avoid matching "AI" inside an
    # unrelated word, but we keep simple substring for multi-word terms.
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
