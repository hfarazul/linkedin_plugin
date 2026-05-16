"""Tests for the Telegram formatting layer.

These verify that user/generated content (prospect names with underscores,
campaign names with brackets, draft bodies with asterisks/ampersands, URLs
with parens) cannot break Telegram parsing now that we use HTML mode and
escape all dynamic text via _h().
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from linkedin_agent.telegram import TelegramClient, _h


def _cfg():
    return type("CFG", (), {
        "telegram_bot_token": "test-token",
        "telegram_chat_id": 12345,
    })()


# ===== _h escape helper =====================================================

@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ("plain text",                          "plain text"),
    ("Last_Name",                            "Last_Name"),
    ("4 * 5 = 20",                           "4 * 5 = 20"),
    ("a<b>c",                                "a&lt;b&gt;c"),
    ("foo & bar",                            "foo &amp; bar"),
    ("Tom & Jerry's [code] *star* _under_",  "Tom &amp; Jerry's [code] *star* _under_"),
    (None,                                   ""),
    ("",                                     ""),
])
def test_h_escapes_only_html_specials(raw, expected):
    """HTML mode only needs &, <, > escaped — asterisks/underscores/brackets
    pass through (they're plain text in HTML mode)."""
    assert _h(raw) == expected


# ===== integration: push_draft_for_approval with adversarial inputs =========

@pytest.mark.integration
def test_push_draft_for_approval_html_escapes_all_dynamic_content():
    """Build the payload and verify nothing dynamic appears unescaped."""
    tg = TelegramClient(_cfg())
    payloads = []
    # Capture the JSON payload we'd POST without actually hitting Telegram.
    def fake_post(method_path, json=None, **kwargs):
        payloads.append((method_path, json))
        class _R:
            def json(self):
                return {"ok": True, "result": {"message_id": 999}}
        return _R()
    tg._client.post = fake_post   # type: ignore

    # Adversarial inputs: characters that are special in Markdown (or HTML)
    tg.push_draft_for_approval(
        draft_id=42,
        kind="dm1",
        body="hey & friends — your post mentioned <my company> and *AI*",
        prospect_name="Test_User & Co.",
        prospect_company="<Acme> [holdings]",
        prospect_url="https://www.linkedin.com/in/test?utm=foo&bar=baz",
        campaign_name="recently-funded [Q2 push]",
    )
    tg.close()

    assert payloads, "no payload was sent"
    body_text = payloads[0][1]["text"]
    # HTML-special chars must be escaped
    assert "&amp;" in body_text       # & → &amp;
    assert "&lt;" in body_text        # < → &lt;
    assert "&gt;" in body_text        # > → &gt;
    # Raw < / > / & must NOT appear in user-text positions (only inside HTML tags)
    # Verify by removing the legit tags and checking for leftover unescaped specials
    import re
    stripped = re.sub(r"<[^>]+>", "", body_text)
    assert "<" not in stripped, f"unescaped < remains: {stripped!r}"
    assert ">" not in stripped, f"unescaped > remains: {stripped!r}"
    # parse_mode is HTML
    assert payloads[0][1]["parse_mode"] == "HTML"


@pytest.mark.integration
def test_notify_reply_stays_plain_text_for_user_content():
    """Reply bodies are routinely full of *, _, $ — keep these as plain text."""
    tg = TelegramClient(_cfg())
    payloads = []
    def fake_post(method_path, json=None, **kwargs):
        payloads.append((method_path, json))
        class _R:
            def json(self):
                return {"ok": True, "result": {"message_id": 1}}
        return _R()
    tg._client.post = fake_post   # type: ignore

    tg.notify_reply(
        prospect_name="Test User",
        prospect_company="Acme",
        body="Hey! *bold attempt* _italic attempt_ $1,000",
        thread_url="https://www.linkedin.com/in/test",
    )
    tg.close()

    # notify_reply uses parse_mode=None (plain text) — special chars pass through unmolested
    assert payloads[0][1].get("parse_mode") is None
    assert "*bold attempt*" in payloads[0][1]["text"]
