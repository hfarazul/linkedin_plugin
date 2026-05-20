"""Tests for the free-form Claude routing handler.

When the user texts the bot conversationally, the handler builds context,
invokes claude -p, and parses a structured JSON response. These tests:

  • verify the context builder pulls the right shape
  • verify response parsing tolerates fenced JSON, malformed JSON, etc.
  • verify the end-to-end handler degrades gracefully on failures
  • verify the bot daemon routes free-form text to the handler
"""

from __future__ import annotations

import json

import pytest

from tests.fakes import FakeTelegramClient


def _cfg(**overrides):
    base = {
        "backend": "fake",
        "daily_max_reactions": 30,
        "daily_max_connections": 20,
        "daily_max_dms": 10,
        "daily_max_searches": 50,
        "action_delay_min": 0,
        "action_delay_max": 0,
        "dry_run": False,
        "unipile_api_key": None,
        "unipile_account_id": None,
        "unipile_dsn": None,
        "telegram_bot_token": "fake-token",
        "telegram_chat_id": 12345,
        "playwright_state_path": None,
    }
    base.update(overrides)
    return type("CFG", (), base)()


# ===== response parsing ====================================================

@pytest.mark.unit
def test_parse_response_handles_valid_info_json():
    from linkedin_agent.conversational import _parse_response
    raw = '{"type": "info", "info_text": "connect cap is 17/15"}'
    out = _parse_response(raw)
    assert out["type"] == "info"
    assert "17/15" in out["info_text"]


@pytest.mark.unit
def test_parse_response_strips_markdown_fences():
    from linkedin_agent.conversational import _parse_response
    raw = '```json\n{"type": "info", "info_text": "hello"}\n```'
    out = _parse_response(raw)
    assert out["type"] == "info"
    assert out["info_text"] == "hello"


@pytest.mark.unit
def test_parse_response_handles_clarify():
    from linkedin_agent.conversational import _parse_response
    raw = '{"type": "clarify", "clarify_question": "Which Bret?"}'
    out = _parse_response(raw)
    assert out["type"] == "clarify"
    assert "Which Bret" in out["clarify_question"]


@pytest.mark.unit
def test_parse_response_degrades_on_invalid_json():
    """Garbage from claude → graceful info reply, no crash."""
    from linkedin_agent.conversational import _parse_response
    raw = "this is not json at all, just prose"
    out = _parse_response(raw)
    assert out["type"] == "info"
    assert "rephrasing" in out["info_text"].lower() or "didn't catch" in out["info_text"].lower()


@pytest.mark.unit
def test_parse_response_degrades_on_missing_type():
    """JSON without `type` key → graceful info reply."""
    from linkedin_agent.conversational import _parse_response
    raw = '{"foo": "bar"}'
    out = _parse_response(raw)
    assert out["type"] == "info"
    assert "unexpected" in out["info_text"].lower()


@pytest.mark.unit
def test_parse_response_preview_degrades_to_info_in_v1():
    """v1 doesn't execute actions — preview-type responses convert to info
    with a note about v2."""
    from linkedin_agent.conversational import _parse_response
    raw = ('{"type": "preview", "action": "send_dm", '
           '"action_args": {"prospect_id": 92}, '
           '"preview_text": "Send X to Y"}')
    out = _parse_response(raw)
    assert out["type"] == "info"
    assert "v2" in out["info_text"].lower()


# ===== context building ====================================================

@pytest.mark.integration
def test_build_context_includes_pipeline_summary(db_env):
    """The context loader pulls pipeline counts so Claude knows funnel state."""
    from linkedin_agent import db
    from linkedin_agent.conversational import _build_context

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/ctx-test",
        full_name="Context Test",
    )
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET status='connected' WHERE id=?", (pid,))

    ctx = _build_context(_cfg())
    assert "pipeline_summary" in ctx
    assert ctx["pipeline_summary"]["connected"] >= 1


@pytest.mark.integration
def test_build_context_includes_recent_inbound(db_env):
    """Inbound messages from the last 24h appear in context for Claude."""
    from linkedin_agent import db
    from linkedin_agent.conversational import _build_context

    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/inbound-test",
        full_name="Inbound Test",
    )
    db.record_message(pid, "inbound", "Hey, interested in chatting!",
                       external_id="ext-ctx-1")
    ctx = _build_context(_cfg())
    names = [m["name"] for m in ctx["recent_inbound"]]
    assert "Inbound Test" in names


@pytest.mark.integration
def test_build_context_truncates_when_oversized(db_env):
    """If the context blob exceeds budget, oldest inbound + draft excerpts
    are trimmed. Pipeline + caps survive."""
    from linkedin_agent import db
    from linkedin_agent.conversational import _build_context, _truncate_context

    # Seed 20 inbound messages from 20 different prospects
    for i in range(20):
        pid = db.upsert_prospect(
            linkedin_url=f"https://www.linkedin.com/in/oversize-{i}",
            full_name=f"Oversize {i}",
        )
        db.record_message(pid, "inbound", "x" * 280, external_id=f"oversize-{i}")

    ctx = _build_context(_cfg())
    trimmed = _truncate_context(ctx, max_chars=2000)
    assert "pipeline_summary" in trimmed   # never dropped
    assert "caps" in trimmed                # never dropped
    assert len(trimmed["recent_inbound"]) <= len(ctx["recent_inbound"])


# ===== end-to-end handler ==================================================

@pytest.mark.integration
def test_handle_message_returns_info_for_status_query(db_env):
    """Stubbed claude returns an info response → handler returns it cleanly."""
    from linkedin_agent.conversational import handle_message

    def stub_invoker(prompt):
        return '{"type": "info", "info_text": "Pipeline: 5 connected, 2 replied."}'

    result = handle_message("what's the status", _cfg(), invoker=stub_invoker)
    assert result.type == "info"
    assert "Pipeline" in result.text


@pytest.mark.integration
def test_handle_message_returns_clarify(db_env):
    """Stubbed claude returns clarify → handler prefixes with ❓."""
    from linkedin_agent.conversational import handle_message

    def stub_invoker(prompt):
        return '{"type": "clarify", "clarify_question": "Which Bret?"}'

    result = handle_message("show bret's thread", _cfg(), invoker=stub_invoker)
    assert result.type == "clarify"
    assert "Which Bret" in result.text
    assert result.text.startswith("❓")


@pytest.mark.integration
def test_handle_message_includes_context_in_prompt(db_env):
    """The prompt sent to Claude must include the pipeline context."""
    from linkedin_agent import db
    from linkedin_agent.conversational import handle_message

    db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/prompt-test",
        full_name="Prompt Test",
    )

    captured = {}
    def stub_invoker(prompt):
        captured["prompt"] = prompt
        return '{"type": "info", "info_text": "ok"}'

    handle_message("status", _cfg(), invoker=stub_invoker)
    assert "pipeline_summary" in captured["prompt"]
    assert "caps" in captured["prompt"]


@pytest.mark.integration
def test_handle_message_handles_empty_text(db_env):
    """Empty / whitespace text → safe degradation, no claude call."""
    from linkedin_agent.conversational import handle_message

    def forbidden_invoker(prompt):
        raise AssertionError("should not call claude for empty input")

    result = handle_message("", _cfg(), invoker=forbidden_invoker)
    assert result.type == "info"
    assert "empty" in result.text.lower()


@pytest.mark.integration
def test_handle_message_recovers_from_invoker_garbage(db_env):
    """Claude returns garbage → handler returns a graceful info reply."""
    from linkedin_agent.conversational import handle_message

    def stub_invoker(prompt):
        return "absolutely not json"

    result = handle_message("status", _cfg(), invoker=stub_invoker)
    assert result.type == "info"
    # Either the "didn't catch that" or generic graceful message
    assert len(result.text) > 0


# ===== bot daemon routing ==================================================

def _seed_prospect_and_draft(kind: str = "dm1"):
    from linkedin_agent import db
    pid = db.upsert_prospect(
        linkedin_url="https://www.linkedin.com/in/conv-route-test",
        full_name="Route Test",
    )
    with db.connect() as conn:
        conn.execute("UPDATE prospects SET status='connected' WHERE id=?", (pid,))
    did = db.enqueue_draft(pid, kind, "body padded to clear minimum length " * 10)
    return pid, did


def _make_daemon(cfg):
    from linkedin_agent.bot_daemon import BotDaemon
    from linkedin_agent import bot_daemon as bot_daemon_mod
    fake_tg = FakeTelegramClient(cfg)
    bot_daemon_mod.TelegramClient = lambda c: fake_tg
    daemon = BotDaemon(cfg)
    return daemon, fake_tg


@pytest.mark.integration
def test_daemon_routes_freeform_text_to_conversational_handler(db_env, monkeypatch):
    """A non-edit-reply text message triggers the conversational handler."""
    from linkedin_agent import conversational as conv_mod
    cfg = _cfg()
    daemon, fake_tg = _make_daemon(cfg)

    # Stub the conversational invoker
    monkeypatch.setattr(conv_mod, "_invoke_claude",
                         lambda prompt, timeout=30: '{"type": "info", "info_text": "stub reply"}')

    msg = {
        "chat": {"id": cfg.telegram_chat_id},
        "text": "what's the status",
        # No reply_to_message — not an edit reply
    }
    try:
        daemon._dispatch({"update_id": 1, "message": msg})
    finally:
        daemon.close()

    # The conversational reply should have hit the FakeTelegram client.
    # notify_text() routes through .sent on the fake.
    assert any("stub reply" in m.text for m in fake_tg.sent)


@pytest.mark.integration
def test_daemon_ignores_slash_commands(db_env):
    """/start and /help shouldn't trigger the conversational handler."""
    from linkedin_agent import conversational as conv_mod
    cfg = _cfg()
    daemon, fake_tg = _make_daemon(cfg)

    def forbidden(*a, **kw):
        raise AssertionError("should not invoke claude for /start")

    # Patch invoker to fail loudly if called
    import linkedin_agent.conversational as cm
    original = cm._invoke_claude
    cm._invoke_claude = forbidden
    try:
        msg = {
            "chat": {"id": cfg.telegram_chat_id},
            "text": "/start",
        }
        daemon._dispatch({"update_id": 1, "message": msg})
    finally:
        cm._invoke_claude = original
        daemon.close()


@pytest.mark.integration
def test_daemon_audit_logs_conversational_interactions(db_env, monkeypatch):
    """Every text → claude → response cycle writes a 'conversational' row
    to the actions table for audit."""
    from linkedin_agent import db, conversational as conv_mod
    cfg = _cfg()
    daemon, fake_tg = _make_daemon(cfg)

    monkeypatch.setattr(conv_mod, "_invoke_claude",
                         lambda prompt, timeout=30: '{"type": "info", "info_text": "audited reply"}')

    msg = {
        "chat": {"id": cfg.telegram_chat_id},
        "text": "how many connects today",
    }
    try:
        daemon._dispatch({"update_id": 99, "message": msg})
    finally:
        daemon.close()

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT kind, payload FROM actions WHERE kind='conversational' ORDER BY id DESC LIMIT 1"
        ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert "how many connects today" in payload["user_text"]
    assert payload["response_type"] == "info"
