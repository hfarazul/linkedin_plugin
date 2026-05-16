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

# Minimum length per kind — anything shorter is almost always a degenerate
# output (e.g. "I'll wait for your call on which path to take." was 46 chars
# for a connect_note that should be ~200). Triggers a retry.
KIND_MIN_CHARS = {
    "connect_note": 100,
    "dm1": 200,
    "dm2": 120,
    "dm3": 50,
}

# Auto-retry budget. The drafter is stochastic — a fresh `claude -p` call
# usually fixes oversize/empty/short outputs. INSUFFICIENT_CONTEXT is terminal
# (no retry) because that's the drafter being honestly stuck.
MAX_DRAFT_ATTEMPTS = 3

# Spam-tell substrings — the openers spam-detection (human and algorithmic)
# keys on. The subagent prompt forbids these but the model occasionally
# generates them anyway under pressure. Catching them post-hoc gives us a
# belt to the prompt's suspenders.
#
# Matched case-insensitively against the full draft body. Whole-phrase
# matching only (no partial substrings inside larger words).
SPAM_TELLS = (
    "i came across your profile",
    "i came across your",
    "i noticed you",
    "i saw your profile",
    "i'd love to connect",
    "i'd love to chat",
    "i would love to connect",
    "i would love to chat",
    "hope you're doing well",
    "hope this finds you well",
    "your impressive work",
    "your impressive background",
    "open to a quick call",
    "open to a quick chat",
)


def _contains_spam_tell(body: str) -> str | None:
    """Return the matched spam-tell phrase, or None if clean."""
    low = body.lower()
    for phrase in SPAM_TELLS:
        if phrase in low:
            return phrase
    return None


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


def render_prompt(inp: DrafterInput, retry_hint: str | None = None) -> str:
    """Compose the full prompt sent to claude -p: subagent body + JSON context.

    On retry, an optional hint is appended to nudge the next attempt toward
    fixing the specific failure (oversize, too-short, etc.)."""
    base = _load_subagent_prompt()
    payload = json.dumps(asdict(inp), indent=2, ensure_ascii=False)
    closing = "Draft now. Return only the message body."
    if retry_hint:
        closing = f"{retry_hint}\n\n{closing}"
    return f"{base}\n\n# Context\n\n```json\n{payload}\n```\n\n{closing}"


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
    max_attempts: int = MAX_DRAFT_ATTEMPTS,
) -> str:
    """Generate a draft, retrying on recoverable failures (oversize / empty /
    suspiciously short). Raises DrafterError when:
      - INSUFFICIENT_CONTEXT is returned (terminal — no retry, drafter is right)
      - All `max_attempts` runs failed quality checks
      - Build fails (missing prospect, invalid kind, etc.)
    """
    inp = build_input(kind, prospect_id, recent_posts=recent_posts)
    cap_max = KIND_MAX_CHARS[kind]
    cap_min = KIND_MIN_CHARS.get(kind, 50)

    last_failure: str | None = None
    last_body_preview: str | None = None
    retry_hint: str | None = None

    for attempt in range(1, max_attempts + 1):
        prompt = render_prompt(inp, retry_hint=retry_hint)
        raw = _invoke_claude(prompt)
        body = _clean_output(raw)

        # INSUFFICIENT_CONTEXT is terminal — the drafter is telling us there
        # genuinely isn't enough signal. Retrying just wastes tokens.
        if body.strip() == INSUFFICIENT:
            raise DrafterError("INSUFFICIENT_CONTEXT — not enough signal to draft")

        if not body:
            last_failure = f"empty output (attempt {attempt})"
            retry_hint = (
                "Your previous attempt returned an empty response. "
                "Please produce an actual message body this time."
            )
            continue

        if len(body) > cap_max:
            last_failure = f"oversize {len(body)}/{cap_max} (attempt {attempt})"
            last_body_preview = body[:180]
            retry_hint = (
                f"Your previous attempt was {len(body)} characters; the cap for "
                f"`{kind}` is {cap_max}. Be tighter. Cut the second sentence "
                f"if you have to. Keep only the most specific reference."
            )
            continue

        if len(body) < cap_min:
            last_failure = f"too short {len(body)}/{cap_min} (attempt {attempt})"
            last_body_preview = body
            retry_hint = (
                f"Your previous attempt was only {len(body)} characters, which "
                f"is below the {cap_min}-char minimum for a substantive "
                f"`{kind}`. Add a specific reference from the prospect's post "
                f"or profile and a real question. Do not return a single line."
            )
            continue

        # Spam-tell scan: even with the prompt rule, the model occasionally
        # produces banal openers like "I came across your profile". Reject +
        # retry with a specific call-out.
        spam = _contains_spam_tell(body)
        if spam:
            last_failure = f"spam tell {spam!r} (attempt {attempt})"
            last_body_preview = body
            retry_hint = (
                f"Your previous attempt contained the spam-tell phrase "
                f"{spam!r}. This is in the hard-rules list. Rewrite without "
                f"any variant of: 'I came across', 'I noticed you', 'I saw "
                f"your profile', 'I'd love to chat/connect', 'hope you're "
                f"doing well'. Start with a specific reference instead."
            )
            continue

        # All quality gates passed.
        return body

    msg = f"all {max_attempts} drafter attempts failed; last={last_failure}"
    if last_body_preview:
        msg += f"\nlast body preview: {last_body_preview!r}"
    raise DrafterError(msg)
