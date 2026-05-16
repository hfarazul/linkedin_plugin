from __future__ import annotations

# Thin sync wrappers around Telegram Bot API. No third-party library — just
# HTTPS calls. The Bot API is REST and simple enough that a dedicated SDK
# would be more weight than value.
#
# We expose two surfaces:
#   • send_* / edit_* / notify_*  — fire-and-forget helpers usable from the
#                                    sync CLI/cron path.
#   • get_updates                 — long-polling primitive used by the bot
#                                    daemon to receive callbacks and replies.

import html
import json
import textwrap
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config


def _h(text: str | None) -> str:
    """HTML-escape user/generated text for safe Telegram embedding.
    Telegram's HTML parse mode only treats &, <, > as special — three chars,
    handled by stdlib html.escape. Markdown by contrast has ~12 special chars
    and ambiguous escape rules that bite on real-world content."""
    if not text:
        return ""
    return html.escape(text, quote=False)


KIND_LABELS = {
    "connect_note": "Connection note",
    "dm1":          "DM #1 (first message)",
    "dm2":          "DM #2 (4-day follow-up)",
    "dm3":          "DM #3 (breakup)",
    "reply":        "Reply",
}


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, cfg: Config) -> None:
        if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
            raise TelegramError(
                "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env"
            )
        self.cfg = cfg
        self._client = httpx.Client(
            base_url=f"https://api.telegram.org/bot{cfg.telegram_bot_token}",
            timeout=60.0,
        )

    def close(self) -> None:
        self._client.close()

    # -------------------------------------------------------------- low-level

    def _call(self, method: str, **payload: Any) -> dict:
        r = self._client.post(f"/{method}", json=payload)
        try:
            data = r.json()
        except Exception as e:
            raise TelegramError(f"telegram /{method} returned non-json: {r.text[:200]}") from e
        if not data.get("ok"):
            raise TelegramError(f"telegram /{method} failed: {data}")
        return data.get("result", {})

    # ------------------------------------------------------------------ sends

    def send_message(
        self,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = "HTML",
        disable_preview: bool = True,
    ) -> int:
        """Send a message to the configured chat. Defaults to HTML parse mode
        (callers must HTML-escape any user/generated text via _h()). Pass
        parse_mode=None for plain text. Returns message_id."""
        payload = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": text,
            "disable_web_page_preview": disable_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self._call("sendMessage", **payload)
        return int(result["message_id"])

    def edit_message(
        self,
        message_id: int,
        new_text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = "HTML",
    ) -> None:
        payload = {
            "chat_id": self.cfg.telegram_chat_id,
            "message_id": message_id,
            "text": new_text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._call("editMessageText", **payload)

    def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        """Required after handling a callback_query — silences the loading spinner
        on the user's button tap."""
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self._call("answerCallbackQuery", **payload)

    # -------------------------------------------------------------- high-level

    def push_draft_for_approval(
        self,
        draft_id: int,
        kind: str,
        body: str,
        prospect_name: str | None,
        prospect_company: str | None = None,
        prospect_url: str | None = None,
        campaign_name: str | None = None,
        inbound_excerpt: str | None = None,
    ) -> int:
        """Format a draft and send it to chat with Approve / Edit / Reject buttons.
        All dynamic content is HTML-escaped so generated text (e.g. names with
        underscores, posts with asterisks, URLs with parens) can't break parsing.
        Returns the Telegram message_id.

        `inbound_excerpt` is optional — when provided (typically for `reply`
        kind), it renders the prospect's incoming message above the draft so
        you can judge the response in context on your phone."""
        label = KIND_LABELS.get(kind, kind)
        who = prospect_name or "(unknown)"
        if prospect_company:
            who += f" — {prospect_company}"

        header = f"📝 <b>{_h(label)}</b> → <b>{_h(who)}</b>"
        if campaign_name:
            header += f"  <i>[{_h(campaign_name)}]</i>"
        if prospect_url:
            # Render the URL as plain escaped text — Telegram HTML mode auto-
            # linkifies bare URLs, so an explicit <a href="..."> wrapper buys
            # us nothing visually but exposes us to attribute-escape bugs
            # (a URL containing " would break the href).
            header += f"\n{_h(prospect_url)}"

        # For replies, prepend the inbound message in a quote-block so the
        # reviewer sees what they're responding to.
        body_section = ""
        if inbound_excerpt:
            body_section = f"💬 <i>They said:</i>\n<blockquote>{_h(inbound_excerpt)}</blockquote>\n\n"
        body_section += _h(body)

        # Telegram has a 4096-char total limit. Drafts are <600 so safe.
        text = f"{header}\n\n{body_section}"

        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{draft_id}"},
                {"text": "✏️ Edit",    "callback_data": f"edit:{draft_id}"},
                {"text": "❌ Reject",  "callback_data": f"reject:{draft_id}"},
            ]]
        }
        return self.send_message(text, reply_markup=keyboard)

    def mark_draft_sent(self, message_id: int, original_body: str, when: str) -> None:
        new_text = f"✅ <b>Sent at {_h(when)}</b>\n\n{_h(original_body)}"
        self.edit_message(message_id, new_text, reply_markup={"inline_keyboard": []})

    def mark_draft_rejected(self, message_id: int, original_body: str) -> None:
        new_text = f"❌ <b>Rejected</b>\n\n<s>{_h(original_body)}</s>"
        self.edit_message(message_id, new_text, reply_markup={"inline_keyboard": []})

    def mark_draft_error(self, message_id: int, original_body: str, error: str,
                          draft_id: int) -> None:
        """Show send failure with retry/giveup buttons. The callbacks carry
        the draft_id (not the telegram message_id) so the daemon can route
        them to the right pending_drafts row."""
        new_text = (
            f"⚠️ <b>Send failed:</b> <code>{_h(error)}</code>\n\n"
            f"{_h(original_body)}"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "🔄 Retry", "callback_data": f"retry:{draft_id}"},
                {"text": "❌ Give up", "callback_data": f"giveup:{draft_id}"},
            ]]
        }
        self.edit_message(message_id, new_text, reply_markup=keyboard)

    def request_edit(self, draft_id: int, original_body: str) -> int:
        """Send a force-reply prompt for editing a draft. Returns the prompt's
        message_id, which the daemon uses to match the next reply to this draft."""
        prompt = (
            f"✏️ Replying to draft #{draft_id}\n\n"
            f"Current text:\n<pre>{_h(original_body)}</pre>\n"
            f"Reply to this message with the new text."
        )
        payload = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": prompt,
            "parse_mode": "HTML",
            "reply_markup": {"force_reply": True, "selective": False},
        }
        result = self._call("sendMessage", **payload)
        return int(result["message_id"])

    def notify_reply(
        self,
        prospect_name: str | None,
        prospect_company: str | None,
        body: str,
        thread_url: str | None = None,
    ) -> int:
        who = prospect_name or "(unknown)"
        if prospect_company:
            who += f" — {prospect_company}"
        excerpt = body[:400] + ("…" if len(body) > 400 else "")
        # Reply bodies are user-generated and routinely contain *, _, $, etc.
        # which trip up Telegram's Markdown parser. Send the whole notification
        # as plain text — the emoji already signals what this is.
        text = f"💬 New reply from {who}\n\n{excerpt}"
        if thread_url:
            text += f"\n\n{thread_url}"
        return self.send_message(text, parse_mode=None)

    def notify_text(self, text: str) -> int:
        """Generic notification (e.g. cron run summary, cap warning)."""
        return self.send_message(text, parse_mode=None)

    # -------------------------------------------------------------- daemon API

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict]:
        """Long-poll for new updates. Caller passes offset = last_update_id + 1
        to advance the cursor. Returns the raw list of update objects."""
        payload = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        r = self._client.post(
            "/getUpdates",
            json=payload,
            timeout=timeout + 5,
        )
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(f"getUpdates failed: {data}")
        return data.get("result", [])
