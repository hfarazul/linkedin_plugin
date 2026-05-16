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

def _stub_post(tg):
    """Replace the httpx client's POST with one that records and returns OK."""
    payloads = []
    def fake_post(method_path, json=None, **kwargs):
        payloads.append((method_path, json))
        class _R:
            def json(self):
                return {"ok": True, "result": {"message_id": 999}}
        return _R()
    tg._client.post = fake_post   # type: ignore
    return payloads


@pytest.mark.integration
def test_push_draft_for_approval_html_escapes_all_dynamic_content():
    """Build the payload and verify nothing dynamic appears unescaped."""
    tg = TelegramClient(_cfg())
    payloads = _stub_post(tg)

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
    assert "&amp;" in body_text
    assert "&lt;" in body_text
    assert "&gt;" in body_text
    # No unescaped specials in user-text positions
    import re
    stripped = re.sub(r"<[^>]+>", "", body_text)
    assert "<" not in stripped, f"unescaped < remains: {stripped!r}"
    assert ">" not in stripped, f"unescaped > remains: {stripped!r}"
    assert payloads[0][1]["parse_mode"] == "HTML"


@pytest.mark.integration
def test_push_draft_for_approval_url_with_quote_cannot_break_parsing():
    """A URL containing a double-quote was previously embedded inside an
    href="..." attribute with quote=False escaping — that would have broken
    the HTML and made Telegram reject the message. After the fix, URLs are
    rendered as plain escaped text so attribute injection is structurally
    impossible."""
    tg = TelegramClient(_cfg())
    payloads = _stub_post(tg)

    weird_url = 'https://www.linkedin.com/in/test"onmouseover=evil()'
    tg.push_draft_for_approval(
        draft_id=1,
        kind="connect_note",
        body="body that is long enough to satisfy any min-length check elsewhere in the codebase",
        prospect_name="Test User",
        prospect_url=weird_url,
    )
    tg.close()

    body_text = payloads[0][1]["text"]
    # Critically: there must be NO `href=` anywhere — we render URLs as plain text
    assert 'href=' not in body_text, f"unexpected href attribute: {body_text!r}"
    # The bare URL text is preserved (with HTML-escape applied — quotes pass
    # through since they're not <, >, or &)
    assert weird_url in body_text


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
