from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAMPAIGNS_DIR = ROOT / "campaigns"

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_KV_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.+)$")


@dataclass
class CampaignBrief:
    slug: str
    name: str
    target_icp: str | None
    status: str
    brief: str   # markdown body without frontmatter
    path: Path


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Minimal YAML frontmatter parser — only handles flat string key/value pairs.
    Avoids pulling in pyyaml for the simple case."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw_meta, body = m.group(1), m.group(2)
    meta: dict[str, str] = {}
    for line in raw_meta.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        kv = _KV_RE.match(line)
        if kv:
            meta[kv.group(1)] = kv.group(2).strip().strip('"').strip("'")
    return meta, body


def brief_path_for(slug: str) -> Path:
    return CAMPAIGNS_DIR / f"{slug}.md"


def load_brief(slug: str) -> CampaignBrief:
    path = brief_path_for(slug)
    if not path.exists():
        raise FileNotFoundError(f"no campaign brief at {path}")
    raw = path.read_text()
    meta, body = _parse_frontmatter(raw)
    return CampaignBrief(
        slug=meta.get("slug", slug),
        name=meta.get("name", slug),
        target_icp=meta.get("target_icp") or None,
        status=meta.get("status", "active"),
        brief=body.strip(),
        path=path,
    )


def list_brief_files() -> list[Path]:
    if not CAMPAIGNS_DIR.exists():
        return []
    return sorted(p for p in CAMPAIGNS_DIR.glob("*.md") if p.is_file())


CAMPAIGN_TEMPLATE = """\
---
slug: {slug}
name: {name}
status: active
target_icp: <e.g., Series A-C SaaS founders, eng team 5-30, no ML team yet>
---

# Pitch

<2-4 lines on the service offering you're leading with. What you do, for whom, what changes for them.>

# Pain points we address

- <pain 1>
- <pain 2>
- <pain 3>

# Proof points

- <case study or metric>
- <case study or metric>

# Tone

<direct | consultative | warm | technical — anything that should shape the drafter's voice>
"""


def scaffold_brief(slug: str, name: str | None = None) -> Path:
    path = brief_path_for(slug)
    if path.exists():
        raise FileExistsError(f"campaign brief already exists at {path}")
    CAMPAIGNS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(CAMPAIGN_TEMPLATE.format(slug=slug, name=name or slug))
    return path
