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


# Headlines that signal someone is a builder engineer at the company.
# "engineer" alone is too noisy (sales engineer, customer engineer, quality
# engineer); we need the modifier to confirm builder-side.
_BUILDER_ENG_PATTERNS = (
    "software engineer", "senior engineer", "lead engineer",
    "principal engineer", "staff engineer", "tech lead",
    "ml engineer", "ai engineer", "data engineer", "devops engineer",
    "full-stack", "fullstack", "full stack",
    "backend", "frontend", "front-end", "back-end",
    "founding engineer", "founding mle", "founding software",
    # "engineering @ X" / "engineering at X" — usually means they're on the
    # eng team in some capacity (Sr Manager Eng, IC, etc.).
    "engineering @", "engineering at",
)


@dataclass
class TeamCheckResult:
    """Result of looking for an existing engineering team at the company.

    The disqualification reason is non-None when the team check thinks the
    company has too much eng capacity to need an agency. The CLI uses this
    to skip the import with a clear reason logged.
    """
    cto_found: bool
    cto_name: str | None
    builder_engineers: list[str]   # names (display) of current builder engineers
    employees_seen: int            # de-duplicated count of current employees that surfaced
    disqualification: str | None


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


_PAST_ROLE_MARKERS = (
    "previously", "previous:", "ex-", "former ", "(former)",
    "past:", "prev:", "last:", "ex ",
)


def _is_current_employee(headline_lower: str, company_variants: list[str]) -> bool:
    """Heuristic: does this headline describe a CURRENT role at <company>?

    LinkedIn headlines typically lead with the current role, with past roles
    or side-projects pushed after a "|", "previously:", "ex-", etc.

      ✓ "Cofounder @ Hardline | Construction has always run on verbal..."
         — current: company in first segment, no past markers
      ✗ "Sr. Engineering Manager at Sofar Ocean. Previously: Founder & CTO
         at Particle and Hardline"
         — past: company appears AFTER a 'previously' marker
      ✗ "Ex-CTO at Acme | Building something new at Beta"
         — past: company appears immediately after an 'ex-' marker
    """
    # If any past-role marker appears, check whether the company sits before
    # or after it. Before → still counts (companies people lead with).
    # After → past role, doesn't count.
    past_marker_idx = -1
    for marker in _PAST_ROLE_MARKERS:
        idx = headline_lower.find(marker)
        if idx >= 0 and (past_marker_idx == -1 or idx < past_marker_idx):
            past_marker_idx = idx

    # Find earliest variant match (any of them is enough).
    company_idx = -1
    for v in company_variants:
        pattern = r"\b" + re.escape(v) + r"\b"
        m = re.search(pattern, headline_lower)
        if m and (company_idx == -1 or m.start() < company_idx):
            company_idx = m.start()
    if company_idx == -1:
        return False

    # No past marker: presence in headline is enough (founders + most others
    # lead with their current role).
    if past_marker_idx == -1:
        return True
    # Company appears BEFORE the past marker → current role.
    return company_idx < past_marker_idx


def check_team(
    adapter: LinkedInAdapter,
    company: str,
    *,
    founder_url: str | None = None,
    limit_per_query: int = 10,
) -> TeamCheckResult:
    """Detect whether `company` already has an in-house engineering team.

    The campaign brief for recently-funded-non-tech says explicitly "no
    in-house engineering team yet" — but the founder-match step doesn't
    verify that. So we run a few company-keyword searches and look at who
    currently lists the company in their headline.

    Disqualification rules:
      - any current CTO / "chief technology officer" → ICP miss (the
        non-tech founder already has an engineering counterpart)
      - 2+ builder engineers (software/ML/founding eng) currently at the
        company → has a team forming; agency pitch is weaker
      - 1 lone engineer / "AI at X" / ambiguous → no disqualification,
        but recorded so the operator can decide

    Each query consumes one Unipile search call. We do 3 queries → 3 extra
    search-budget units per funding-import. The cap-check upstream still
    only counts the founder-lookup call (1 unit) so the budget hit is real.
    """
    company_variants = _company_variants(company)
    if not company_variants:
        return TeamCheckResult(
            cto_found=False, cto_name=None,
            builder_engineers=[], employees_seen=0, disqualification=None,
        )

    employees_by_url: dict[str, ProspectHit] = {}
    for query in (
        f'"{company}" CTO',
        f'"{company}" engineer',
        f'"{company}" "founding"',
    ):
        try:
            hits = adapter.search(query, limit=limit_per_query)
        except Exception:
            # Best-effort — if a single sub-search fails (e.g. transient
            # Unipile blip), skip it and rely on the others. Don't propagate
            # since this would block legitimate imports.
            continue
        for h in hits:
            if not h.linkedin_url:
                continue
            if founder_url and h.linkedin_url == founder_url:
                continue
            # Current-employee check: company must appear in the headline AND
            # not after a "previously" / "ex-" past-role marker.
            headline_lower = (h.headline or "").lower()
            if not _is_current_employee(headline_lower, company_variants):
                continue
            employees_by_url[h.linkedin_url] = h

    cto_name: str | None = None
    builder_engineers: list[str] = []
    for hit in employees_by_url.values():
        hl = (hit.headline or "").lower()
        # CTO check first — covers "Co-Founder & CTO" and "Chief Technology"
        if " cto" in hl or hl.startswith("cto") or "chief technology" in hl:
            cto_name = cto_name or hit.full_name
            continue  # don't double-count CTO as a builder engineer
        if any(p in hl for p in _BUILDER_ENG_PATTERNS):
            if hit.full_name:
                builder_engineers.append(hit.full_name)

    disqualification: str | None = None
    if cto_name:
        disqualification = f"has CTO ({cto_name})"
    elif len(builder_engineers) >= 2:
        disqualification = f"has eng team ({len(builder_engineers)}: {', '.join(builder_engineers[:3])})"

    return TeamCheckResult(
        cto_found=cto_name is not None,
        cto_name=cto_name,
        builder_engineers=builder_engineers,
        employees_seen=len(employees_by_url),
        disqualification=disqualification,
    )


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


def format_hiring_pitch_context(
    company: str,
    role: str | None,
    posted: str | None,
    description: str | None,
) -> str:
    """Build the pitch_context for a hiring-signal-sourced prospect — used by
    `hiring-import` to give the drafter a concrete hook for the connect note.

    Examples:
      All fields:   "Hiring first engineer (posted today). Building Acme AI
                     — AI-native accounting for SMBs."
      Role + date:  "Hiring senior engineer (posted 3 days ago). Building
                     Acme AI."
      Role only:    "Hiring first engineer. Building Acme AI."
      Just company: "Hiring engineering. Building Acme AI."
    """
    if role:
        hiring_phrase = f"Hiring {role}"
        if posted:
            hiring_phrase += f" (posted {posted})"
    else:
        hiring_phrase = "Hiring engineering"
    base = f"{hiring_phrase}. Building {company}"
    if description:
        base += f" — {description}"
    return base + "."
