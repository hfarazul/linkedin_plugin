"""ICP-fit scoring for search results.

Before any campaign commits prospects to the DB, we grade a sample of
search results against three signals:

  • Geographic match (location regex)
  • Role match (headline contains founder/ceo/owner-style words)
  • Noise exclusion (headline does NOT contain investor/coach/consultant)

The score is keepers/total. Quality bar (default 6/10) gates whether
the campaign proceeds to actual import.

Generic defaults work for most campaigns. Per-campaign overrides via
the brief's YAML frontmatter let specific campaigns relax or tighten:

  ---
  slug: my-campaign
  ...
  icp_role_required: "founder|ceo|co-?founder|owner|cofounder"
  icp_role_excluded: "investor|vc|venture|angel|coach|consultant"
  icp_geo_required: "United States|, CA\\b|, NY\\b|United Kingdom"
  ---
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .adapters.base import ProspectHit


# --------------------------------------------------------- default heuristics

_DEFAULT_ROLE_REQUIRED = re.compile(
    r"\b(founder|co-?founder|cofounder|ceo|chief executive|owner|principal)\b",
    re.IGNORECASE,
)

_DEFAULT_ROLE_EXCLUDED = re.compile(
    r"\b(investor|vc|venture\s+capital|angel\s+investor|venture\s+partner|"
    r"venture\s+associate|coach|consultant|advisor|recruiter|talent|"
    r"head\s+hunter|head\s+of\s+capital|headhunter|fundraise\s+consultant|"
    r"fundraising\s+specialist|ipo\s+specialist)\b",
    re.IGNORECASE,
)

# US + Europe locations — same set as our manual filter
_DEFAULT_GEO_PATTERNS = (
    # US
    r"United States", r", CA\b", r", NY\b", r", MA\b", r", TX\b", r", IL\b",
    r", WA\b", r", FL\b", r", CO\b", r", GA\b", r", PA\b", r", VA\b",
    r"\bNew York\b", r"\bSan Francisco\b", r"\bLos Angeles\b", r"\bBoston\b",
    r"\bSeattle\b", r"\bAustin\b", r"\bChicago\b", r"\bDenver\b",
    r"\bMiami\b", r"\bAtlanta\b", r"\bBay Area\b", r"\bSilicon Valley\b",
    # Europe
    r"United Kingdom", r"\bUK\b", r"\bLondon\b", r"\bManchester\b",
    r"\bBerlin\b", r"\bMunich\b", r"\bAmsterdam\b", r"\bParis\b",
    r"\bMadrid\b", r"\bBarcelona\b", r"\bStockholm\b", r"\bCopenhagen\b",
    r"\bDublin\b", r"\bZurich\b", r"\bMilan\b", r"\bGermany\b",
    r"\bFrance\b", r"\bNetherlands\b", r"\bSpain\b", r"\bSweden\b",
    r"\bItaly\b", r"\bDenmark\b", r"\bIreland\b", r"\bPortugal\b",
    r"\bSwitzerland\b", r"\bBelgium\b", r"\bAustria\b", r"\bFinland\b",
    r"\bNorway\b", r"\bPoland\b",
)
_DEFAULT_GEO_RE = re.compile("|".join(_DEFAULT_GEO_PATTERNS))

DEFAULT_QUALITY_THRESHOLD = 6   # out of 10


# ------------------------------------------------------------- scoring types

@dataclass
class ProspectGrade:
    prospect: ProspectHit
    geo_match: bool
    role_match: bool
    noise_excluded: bool   # True == clean (no noise keywords)
    notes: list[str]

    @property
    def is_keeper(self) -> bool:
        """All three signals must pass to count as a keeper."""
        return self.geo_match and self.role_match and self.noise_excluded


@dataclass
class CampaignICP:
    """Per-campaign overrides for the heuristics. Built from a campaign
    brief's frontmatter, with sensible defaults when fields are missing."""
    role_required: re.Pattern = _DEFAULT_ROLE_REQUIRED
    role_excluded: re.Pattern = _DEFAULT_ROLE_EXCLUDED
    geo_required: re.Pattern = _DEFAULT_GEO_RE

    @classmethod
    def from_brief_meta(cls, meta: dict[str, str]) -> "CampaignICP":
        kwargs = {}
        if meta.get("icp_role_required"):
            kwargs["role_required"] = re.compile(meta["icp_role_required"], re.IGNORECASE)
        if meta.get("icp_role_excluded"):
            kwargs["role_excluded"] = re.compile(meta["icp_role_excluded"], re.IGNORECASE)
        if meta.get("icp_geo_required"):
            kwargs["geo_required"] = re.compile(meta["icp_geo_required"])
        return cls(**kwargs)


# --------------------------------------------------------- scoring functions

def grade(prospect: ProspectHit, icp: CampaignICP | None = None) -> ProspectGrade:
    icp = icp or CampaignICP()
    notes: list[str] = []

    # Geographic match
    geo_match = bool(prospect.location and icp.geo_required.search(prospect.location))
    if not geo_match:
        notes.append(f"geo miss ({(prospect.location or 'unknown')[:30]})")

    # Role match in headline
    headline = prospect.headline or ""
    role_match = bool(icp.role_required.search(headline))
    if not role_match:
        notes.append("no founder/CEO/owner keyword in headline")

    # Noise exclusion
    noise_match = icp.role_excluded.search(headline)
    noise_excluded = noise_match is None
    if noise_match:
        notes.append(f"noise: {noise_match.group(0)!r}")

    return ProspectGrade(
        prospect=prospect,
        geo_match=geo_match,
        role_match=role_match,
        noise_excluded=noise_excluded,
        notes=notes,
    )


@dataclass
class ValidationResult:
    grades: list[ProspectGrade]
    threshold: int

    @property
    def keepers(self) -> list[ProspectGrade]:
        return [g for g in self.grades if g.is_keeper]

    @property
    def keeper_count(self) -> int:
        return len(self.keepers)

    @property
    def total(self) -> int:
        return len(self.grades)

    @property
    def passes(self) -> bool:
        return self.keeper_count >= self.threshold


def validate_results(
    prospects: Iterable[ProspectHit],
    icp: CampaignICP | None = None,
    threshold: int = DEFAULT_QUALITY_THRESHOLD,
) -> ValidationResult:
    grades = [grade(p, icp) for p in prospects]
    return ValidationResult(grades=grades, threshold=threshold)
