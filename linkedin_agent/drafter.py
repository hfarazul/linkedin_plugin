from __future__ import annotations

# The drafter shells out to `claude -p` using the same system prompt as the
# interactive message-drafter subagent. Both code paths read the prompt from
# .claude/agents/message-drafter.md so there's a single source of truth.
#
# No Anthropic API key needed — this uses your existing Claude Code auth.

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Sequence

from . import campaigns as campaigns_mod
from . import db

ROOT = Path(__file__).resolve().parent.parent
SUBAGENT_PATH = ROOT / ".claude" / "agents" / "message-drafter.md"

INSUFFICIENT = "INSUFFICIENT_CONTEXT"


class DrafterError(RuntimeError):
    pass


# Length caps mirror the rules in the subagent prompt. We enforce them
# defensively in code as well — a model can still go over and we want to catch
# it before sending to LinkedIn (which would reject a >300-char connect note).
KIND_MAX_CHARS = {
    "connect_note": 300,
    "dm1": 600,
    "dm2": 400,
    "dm3": 200,
}


@dataclass
class DrafterInput:
    kind: str
    campaign: dict
    prospect: dict
    recent_posts: list[dict] = field(default_factory=list)
    prior_messages: list[dict] = field(default_factory=list)


# -------------------------------------------------------------- prompt loading

_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def _load_subagent_prompt() -> str:
    if not SUBAGENT_PATH.exists():
        raise DrafterError(f"subagent prompt missing at {SUBAGENT_PATH}")
    raw = SUBAGENT_PATH.read_text()
    return _FRONTMATTER_RE.sub("", raw, count=1).strip()


# -------------------------------------------------------------- context build

def _first_name(full_name: str | None) -> str | None:
    if not full_name:
        return None
    return full_name.split()[0]


def build_input(
    kind: str,
    prospect_id: int,
    recent_posts: Sequence[dict] | None = None,
) -> DrafterInput:
    """Assemble the JSON payload the drafter prompt expects.
    The caller passes recent_posts because that comes from the adapter, not the DB."""
    if kind not in KIND_MAX_CHARS:
        raise DrafterError(f"invalid kind {kind!r}; expected one of {list(KIND_MAX_CHARS)}")

    prospect_row = db.get_prospect(prospect_id)
    if not prospect_row:
        raise DrafterError(f"prospect {prospect_id} not found")

    campaign_row = None
    if prospect_row["campaign_id"]:
        with db.connect() as conn:
            cur = conn.execute(
                "SELECT * FROM campaigns WHERE id = ?", (prospect_row["campaign_id"],)
            )
            campaign_row = cur.fetchone()

    if campaign_row:
        brief = campaigns_mod.load_brief(campaign_row["slug"])
        campaign_ctx = {
            "name": brief.name,
            "target_icp": brief.target_icp,
            "brief": brief.brief,
        }
    else:
        # Drafter still works without a campaign — useful for ad-hoc messages —
        # but quality drops significantly. The subagent prompt will likely
        # return INSUFFICIENT_CONTEXT.
        campaign_ctx = {"name": "(no campaign)", "target_icp": None, "brief": ""}

    # Prior thread for DM2/DM3 context.
    prior: list[dict] = []
    if kind in ("dm2", "dm3"):
        with db.connect() as conn:
            cur = conn.execute(
                """SELECT direction, body, sent_at FROM messages
                   WHERE prospect_id = ? ORDER BY sent_at""",
                (prospect_id,),
            )
            prior = [dict(r) for r in cur.fetchall()]

    return DrafterInput(
        kind=kind,
        campaign=campaign_ctx,
        prospect={
            "full_name": prospect_row["full_name"],
            "first_name": _first_name(prospect_row["full_name"]),
            "headline": prospect_row["headline"],
            "company": prospect_row["company"],
            "title": prospect_row["title"],
            "pitch_context": prospect_row["pitch_context"],
        },
        recent_posts=list(recent_posts or []),
        prior_messages=prior,
    )


def render_prompt(inp: DrafterInput) -> str:
    """Compose the full prompt sent to claude -p: subagent body + JSON context."""
    base = _load_subagent_prompt()
    payload = json.dumps(asdict(inp), indent=2, ensure_ascii=False)
    return f"{base}\n\n# Context\n\n```json\n{payload}\n```\n\nDraft now. Return only the message body."


# -------------------------------------------------------------- claude invoker

def _invoke_claude(prompt: str, timeout: int = 90) -> str:
    """Run `claude -p` and return stdout. Separated for test stubbing."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise DrafterError("`claude` binary not on PATH — is Claude Code installed?")
    proc = subprocess.run(
        [claude_bin, "-p", prompt, "--output-format", "text"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise DrafterError(
            f"claude -p exited {proc.returncode}\nstderr:\n{proc.stderr[:500]}"
        )
    return proc.stdout


# -------------------------------------------------------------- output cleanup

# Models sometimes wrap output in code fences despite instructions. Strip them.
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```\s*$", re.DOTALL)


def _clean_output(raw: str) -> str:
    text = raw.strip()
    m = _CODE_FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    # Strip surrounding quotes if the whole body is wrapped in them.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text


# -------------------------------------------------------------- public API

def draft(
    kind: str,
    prospect_id: int,
    recent_posts: Sequence[dict] | None = None,
) -> str:
    """Generate a draft. Raises DrafterError on failure (including
    INSUFFICIENT_CONTEXT or oversize output)."""
    inp = build_input(kind, prospect_id, recent_posts=recent_posts)
    prompt = render_prompt(inp)
    raw = _invoke_claude(prompt)
    body = _clean_output(raw)

    if not body:
        raise DrafterError("drafter returned empty output")
    if body.strip() == INSUFFICIENT:
        raise DrafterError("INSUFFICIENT_CONTEXT — not enough signal to draft")

    cap = KIND_MAX_CHARS[kind]
    if len(body) > cap:
        raise DrafterError(
            f"draft exceeds {cap}-char cap for {kind} (got {len(body)} chars):\n{body[:200]}…"
        )

    return body
