from __future__ import annotations

# Free-form Claude routing for the Telegram bot.
#
# When the user texts the bot conversationally (not an edit-reply, not a
# button tap), we route the text + current pipeline context through
# `claude -p` and get back one of three structured response types:
#
#   • info     — read-only answer, posted directly to the chat
#   • clarify  — Claude needs more info before it can answer
#   • preview  — Claude is proposing an action; we'll surface a Yes/No
#                confirmation (v2; v1 stops at info + clarify)
#
# Output contract is a strict JSON schema (see SYSTEM_PROMPT). The handler
# parses defensively — malformed responses degrade to a graceful info reply.
#
# v1 scope: read-only INFO + CLARIFY only. PREVIEW (action proposals) ships
# in v2 once we've validated the routing + context-loading shape.

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db, safety
from .config import Config

logger = logging.getLogger("linkedin.conversational")


# Maximum characters we'll send Claude as context. Larger context = larger
# token cost + slower response + more chance of context-window issues.
_CONTEXT_MAX_CHARS = 4000

# How long we wait for `claude -p` before giving up.
_CLAUDE_TIMEOUT_SEC = 30


SYSTEM_PROMPT = """\
You are a LinkedIn outreach assistant integrated with Cortivo's outreach
system. The user is messaging via Telegram and asking about the state of
their outreach pipeline.

You respond in STRICT JSON only. No markdown code fences, no preamble, no
explanation. Output exactly one JSON object matching one of these shapes:

  {"type": "info", "info_text": "<plain text or simple markdown>"}

  {"type": "clarify", "clarify_question": "<question to disambiguate>"}

If the user asks for an action (send a message, skip a prospect, etc.),
respond with INFO explaining that action-mode is coming in v2 and they
should use the CLI for now. Do NOT output "preview" type in v1.

## Available context

The next message will include a JSON payload with:

  • pipeline_summary: counts of prospects in each pipeline stage
  • caps: today's usage vs. limits for connect / dm / react / search
  • campaigns: list of active campaign slugs
  • recent_inbound: list of recent inbound messages (last 24h)
  • pending_drafts: drafts awaiting Telegram approval

## How to respond

  • "what's the status" → INFO with pipeline summary + caps + pending drafts
  • "how many connects today" → INFO with the connect cap usage
  • "show me Bret's thread" → if context has the inbound, INFO with the
    excerpt; otherwise CLARIFY ("which Bret?" or "I don't have that
    thread in current context; what specifically are you looking for?")
  • "skip 199" / "send X to Y" → INFO ("Action mode coming in v2 — use
    `python -m linkedin_agent skip 199` from CLI for now")
  • If ambiguous (multiple matches, unclear intent), CLARIFY

Keep INFO responses tight. Most prospects only need 2-5 sentences. Use
markdown sparingly — Telegram renders some markdown, but heavy formatting
is noise on a phone.

If the request can't be answered from the given context, say so honestly
("I don't have that in context. Try CLI: `python -m linkedin_agent ...`").
"""


# --------------------------------------------------------------- context

def _build_context(cfg: Config) -> dict[str, Any]:
    """Pull current state for Claude. Keep tight — context size affects
    Claude latency and cost. Total target: < 4000 chars."""
    db.init_db()

    # Pipeline counts by status
    pipeline = {}
    for status in ("targeted", "reacted", "connection_sent", "connected",
                   "dm_sent", "replied", "skipped"):
        pipeline[status] = len(db.list_prospects(status=status, limit=10_000))

    # Caps
    caps = {}
    for kind, field in safety.CAP_FIELD.items():
        used = db.count_actions_last_24h(kind)
        cap = getattr(cfg, field, None)
        caps[kind] = {"used": used, "cap": cap}

    # Active campaigns (just slugs — names are inferred)
    campaigns = [c["slug"] for c in db.list_campaigns(status="active")]

    # Recent inbound (last 24h)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    inbound = []
    with db.connect() as conn:
        cur = conn.execute(
            """SELECT m.prospect_id, p.full_name, m.body, m.sent_at
               FROM messages m JOIN prospects p ON p.id = m.prospect_id
               WHERE m.direction = 'inbound' AND m.sent_at > ?
               ORDER BY m.sent_at DESC LIMIT 8""",
            (cutoff,),
        )
        for r in cur.fetchall():
            inbound.append({
                "prospect_id": r["prospect_id"],
                "name": r["full_name"],
                "body": (r["body"] or "")[:300],
                "sent_at": r["sent_at"],
            })

    # Pending drafts (truncated body)
    drafts = []
    for d in db.list_pending_drafts(status="pending"):
        drafts.append({
            "id": d["id"],
            "prospect_id": d["prospect_id"],
            "kind": d["kind"],
            "body_excerpt": (d["body"] or "")[:200],
        })

    return {
        "pipeline_summary": pipeline,
        "caps": caps,
        "campaigns": campaigns,
        "recent_inbound": inbound,
        "pending_drafts": drafts,
    }


def _truncate_context(ctx: dict, max_chars: int = _CONTEXT_MAX_CHARS) -> dict:
    """If context exceeds budget, drop oldest inbound + truncate draft
    excerpts. Pipeline + caps + campaigns always survive — they're tiny."""
    serialized = json.dumps(ctx)
    if len(serialized) <= max_chars:
        return ctx

    # Drop oldest inbound messages first
    ctx = dict(ctx)
    ctx["recent_inbound"] = ctx["recent_inbound"][:4]
    if len(json.dumps(ctx)) <= max_chars:
        return ctx

    # Truncate draft excerpts further
    ctx["pending_drafts"] = [
        {**d, "body_excerpt": d["body_excerpt"][:80]}
        for d in ctx["pending_drafts"]
    ]
    return ctx


# --------------------------------------------------------------- claude

def _invoke_claude(prompt: str, timeout: int = _CLAUDE_TIMEOUT_SEC) -> str:
    """Run `claude -p` and return stdout. Separated for stubbing in tests.

    Does NOT raise on non-zero exit — wraps the failure in a JSON info
    response so the caller can still send something useful to the user."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return json.dumps({
            "type": "info",
            "info_text": "⚠️ Claude binary not on PATH — can't process conversational requests.",
        })
    try:
        proc = subprocess.run(
            [claude_bin, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({
            "type": "info",
            "info_text": "⏱️ Claude timed out. Try a more specific question?",
        })
    if proc.returncode != 0:
        # Same shape as drafter's flaky-claude failure mode. Return a
        # friendly response instead of crashing.
        logger.warning("conversational claude exited %d", proc.returncode)
        return json.dumps({
            "type": "info",
            "info_text": "⚠️ Claude call failed. Try again or check `data/bot-daemon.err.log`.",
        })
    return proc.stdout


# --------------------------------------------------------------- parsing

# Models occasionally wrap JSON in fences despite instructions. Strip them.
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _parse_response(raw: str) -> dict[str, Any]:
    """Parse Claude's JSON response. Defensive — bad JSON degrades to
    a graceful info reply rather than a crash."""
    text = raw.strip()
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("conversational: invalid JSON from claude: %r", text[:200])
        return {
            "type": "info",
            "info_text": ("⚠️ Sorry, didn't catch that. Try rephrasing — "
                          "or use the CLI for specific commands."),
        }

    if not isinstance(parsed, dict) or "type" not in parsed:
        return {
            "type": "info",
            "info_text": "⚠️ Got an unexpected response shape from Claude.",
        }

    t = parsed.get("type")
    if t == "info":
        return {"type": "info", "info_text": str(parsed.get("info_text", ""))}
    if t == "clarify":
        return {"type": "clarify", "clarify_question": str(parsed.get("clarify_question", ""))}
    if t == "preview":
        # v1 doesn't implement action execution. Degrade to info with a
        # note about v2 — Claude was told not to emit preview, but defend.
        preview_text = parsed.get("preview_text", "")
        return {
            "type": "info",
            "info_text": (f"📋 Action proposal (not yet executable):\n\n{preview_text}\n\n"
                          "Action-mode ships in v2. Use the CLI for now."),
        }
    return {
        "type": "info",
        "info_text": f"⚠️ Unknown response type from Claude: {t!r}",
    }


# --------------------------------------------------------------- entry

@dataclass
class ConversationalResult:
    """Result of one free-form text → Claude → response cycle."""
    type: str               # 'info' | 'clarify'
    text: str               # the reply we'll send to Telegram
    raw_claude_output: str  # for audit log
    error: str | None = None


def handle_message(
    text: str, cfg: Config, *,
    invoker=None,
) -> ConversationalResult:
    """Process a free-form text from the Telegram chat.

    Returns ConversationalResult with the text to post back. Caller (bot
    daemon) is responsible for actually posting + audit logging.

    `invoker` is the function used to call claude. When None (production),
    resolves `_invoke_claude` at call time — late binding so tests that
    monkeypatch the module-level reference take effect. Tests can also
    pass a stub directly via this parameter."""
    if invoker is None:
        invoker = _invoke_claude
    if not text or not text.strip():
        return ConversationalResult(
            type="info", text="(empty message)", raw_claude_output="",
        )

    try:
        context = _build_context(cfg)
        context = _truncate_context(context)
    except Exception as e:
        logger.exception("context build failed")
        return ConversationalResult(
            type="info",
            text=f"⚠️ Error gathering context: {str(e)[:200]}",
            raw_claude_output="", error=str(e),
        )

    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"## User's message\n\n{text}\n\n"
        f"## Current context\n\n"
        f"```json\n{json.dumps(context, indent=2)}\n```\n\n"
        f"Respond with valid JSON only — no markdown fences, no preamble."
    )

    raw = invoker(prompt)
    parsed = _parse_response(raw)
    reply_text = (
        parsed["info_text"] if parsed["type"] == "info"
        else f"❓ {parsed['clarify_question']}"
    )
    return ConversationalResult(
        type=parsed["type"], text=reply_text, raw_claude_output=raw,
    )
