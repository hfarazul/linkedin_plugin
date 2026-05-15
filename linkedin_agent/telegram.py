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

import json
import textwrap
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config


KIND_LABELS = {
    "connect_note": "Connection note",
    "dm1":          "DM #1 (first message)",
    "dm2":          "DM #2 (4-day follow-up)",
    "dm3":          "DM #3 (breakup)",
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
        parse_mode: str | None = "Markdown",
        disable_preview: bool = True,
    ) -> int:
        """Send a plain message to the configured chat. Returns message_id."""
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
        parse_mode: str | None = "Markdown",
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
    ) -> int:
        """Format a draft and send it to chat with Approve / Edit / Reject buttons.
        Returns the Telegram message_id (stored on pending_drafts.telegram_message_id)."""
        label = KIND_LABELS.get(kind, kind)
        who = prospect_name or "(unknown)"
        if prospect_company:
            who += f" — {prospect_company}"

        header = f"📝 *{label}* → *{who}*"
        if campaign_name:
            header += f"  _[{campaign_name}]_"
        if prospect_url:
            header += f"\n[{prospect_url}]({prospect_url})"

        # Telegram has a 4096-char total limit. Drafts are <600 so safe.
        text = f"{header}\n\n{body}"

        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{draft_id}"},
                {"text": "✏️ Edit",    "callback_data": f"edit:{draft_id}"},
                {"text": "❌ Reject",  "callback_data": f"reject:{draft_id}"},
            ]]
        }
        return self.send_message(text, reply_markup=keyboard)

    def mark_draft_sent(self, message_id: int, original_body: str, when: str) -> None:
        new_text = textwrap.dedent(f"""\
            ✅ *Sent at {when}*

            {original_body}""")
        self.edit_message(message_id, new_text, reply_markup={"inline_keyboard": []})

    def mark_draft_rejected(self, message_id: int, original_body: str) -> None:
        new_text = textwrap.dedent(f"""\
            ❌ *Rejected*

            ~{original_body}~""")
        self.edit_message(message_id, new_text, reply_markup={"inline_keyboard": []})

    def mark_draft_error(self, message_id: int, original_body: str, error: str) -> None:
        new_text = textwrap.dedent(f"""\
            ⚠️ *Send failed:* `{error}`

            {original_body}

            (Re-tap Approve to retry.)""")
        keyboard = {
            "inline_keyboard": [[
                {"text": "🔄 Retry", "callback_data": f"retry:{message_id}"},
                {"text": "❌ Give up", "callback_data": f"giveup:{message_id}"},
            ]]
        }
        self.edit_message(message_id, new_text, reply_markup=keyboard)

    def request_edit(self, draft_id: int, original_body: str) -> int:
        """Send a force-reply prompt for editing a draft. Returns the prompt's
        message_id, which the daemon uses to match the next reply to this draft."""
        prompt = (
            f"✏️ Replying to draft #{draft_id}\n\n"
            f"Current text:\n```\n{original_body}\n```\n"
            f"Reply to this message with the new text."
        )
        payload = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": prompt,
            "parse_mode": "Markdown",
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
