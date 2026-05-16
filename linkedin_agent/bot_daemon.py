from __future__ import annotations

# Long-running daemon that processes incoming Telegram updates:
#   • callback_query  (button tap on Approve / Edit / Reject)
#   • message         (text reply to a force-reply prompt — the "edit" path)
#
# Drafts are pushed to Telegram by the cron-driven `daily` command via
# telegram.push_draft_for_approval(). This daemon only handles the user's
# inbound responses.
#
# State that needs to survive a daemon restart lives in the DB. The only
# in-memory state is `_pending_edits: {force_reply_msg_id -> draft_id}`,
# which is fine to lose on restart — user can re-tap Edit if needed.

import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from . import db, safety, send_window
from .adapters import get_adapter
from .config import Config, load as load_config
from .telegram import TelegramClient, TelegramError

logger = logging.getLogger("linkedin.bot")


class BotDaemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.tg = TelegramClient(cfg)
        self.adapter = get_adapter(cfg)
        self._last_update_id: Optional[int] = None
        # Maps force-reply prompt message_id → draft_id awaiting edit text.
        self._pending_edits: dict[int, int] = {}
        self._stop = False

    def close(self) -> None:
        self.tg.close()
        self.adapter.close()

    # -------------------------------------------------------- main loop

    def run(self) -> None:
        logger.info("bot daemon starting (chat_id=%s)", self.cfg.telegram_chat_id)
        signal.signal(signal.SIGINT, self._handle_stop)
        signal.signal(signal.SIGTERM, self._handle_stop)
        while not self._stop:
            try:
                offset = (self._last_update_id + 1) if self._last_update_id else None
                updates = self.tg.get_updates(offset=offset, timeout=25)
            except TelegramError as e:
                logger.error("getUpdates failed: %s — backing off 10s", e)
                time.sleep(10)
                continue
            except Exception as e:
                logger.exception("unexpected getUpdates error, backing off 10s: %s", e)
                time.sleep(10)
                continue

            for upd in updates:
                self._last_update_id = upd["update_id"]
                try:
                    self._dispatch(upd)
                except Exception as e:
                    logger.exception("error handling update %s: %s", upd.get("update_id"), e)
        logger.info("bot daemon stopped")

    def _handle_stop(self, *_args) -> None:
        logger.info("shutdown signal received")
        self._stop = True

    # -------------------------------------------------------- dispatch

    def _dispatch(self, upd: dict) -> None:
        if "callback_query" in upd:
            self._handle_callback(upd["callback_query"])
        elif "message" in upd:
            self._handle_message(upd["message"])

    # -------------------------------------------------------- callback

    def _handle_callback(self, cb: dict) -> None:
        data = cb.get("data") or ""
        cb_id = cb["id"]
        message_id = cb.get("message", {}).get("message_id")
        chat_id = cb.get("message", {}).get("chat", {}).get("id")

        if chat_id != self.cfg.telegram_chat_id:
            self.tg.answer_callback(cb_id, "Unauthorized chat")
            return

        action, _, payload = data.partition(":")
        try:
            draft_id = int(payload)
        except ValueError:
            self.tg.answer_callback(cb_id, "Bad callback data")
            return

        draft = db.get_draft(draft_id)
        if not draft:
            self.tg.answer_callback(cb_id, "Draft not found")
            return

        # Idempotency: if already terminal, don't re-do it. Note 'approved'
        # is NOT terminal — that's the queued-outside-window state, and a
        # retry tap should still flush. 'sent' and 'rejected' are terminal.
        terminal_actions = ("approve", "reject", "retry", "giveup")
        if draft["status"] in ("sent", "rejected") and action in terminal_actions:
            self.tg.answer_callback(cb_id, f"Already {draft['status']}")
            return

        if action == "approve" or action == "retry":
            # Retry on a previously-failed send walks the same path as approve.
            # _approve handles the idempotency check (which counts the draft
            # as still-actionable after mark_draft_error since we don't change
            # its status on failure).
            self._approve(cb_id, draft, message_id)
        elif action == "reject" or action == "giveup":
            self._reject(cb_id, draft, message_id)
        elif action == "edit":
            self._start_edit(cb_id, draft)
        else:
            self.tg.answer_callback(cb_id, f"Unknown action: {action}")

    def _approve(self, cb_id: str, draft, message_id: int) -> None:
        # Outside the send window, queue: flip draft to 'approved' but don't
        # call the adapter. The daily cron's `send-approved` step flushes
        # these during the next open window.
        if not send_window.is_open():
            db.set_draft_status(draft["id"], "approved")
            self.tg.answer_callback(cb_id, "Approved — queued for next window")
            when = send_window.format_next_open()
            # HTML-escape so a body containing &, <, > can't break parsing.
            from .telegram import _h
            self.tg.edit_message(
                message_id,
                f"⏸️ <b>Approved</b> — queued for {_h(when)}\n\n{_h(draft['body'])}",
                reply_markup={"inline_keyboard": []},
            )
            return

        self.tg.answer_callback(cb_id, "Sending…")
        try:
            self._send_via_adapter(draft)
        except safety.RateLimitExceeded as e:
            self.tg.answer_callback(cb_id, f"Cap hit: {e}")
            self.tg.mark_draft_error(message_id, draft["body"], str(e), draft_id=draft["id"])
            return
        except Exception as e:
            logger.exception("send failed for draft %s", draft["id"])
            self.tg.mark_draft_error(message_id, draft["body"], str(e)[:200], draft_id=draft["id"])
            return
        now_local = datetime.now().strftime("%H:%M")
        self.tg.mark_draft_sent(message_id, draft["body"], now_local)

    def _reject(self, cb_id: str, draft, message_id: int) -> None:
        db.set_draft_status(draft["id"], "rejected", reject_reason="user_rejected")
        self.tg.answer_callback(cb_id, "Rejected")
        self.tg.mark_draft_rejected(message_id, draft["body"])

    def _start_edit(self, cb_id: str, draft) -> None:
        self.tg.answer_callback(cb_id, "Edit mode")
        prompt_msg_id = self.tg.request_edit(draft["id"], draft["body"])
        self._pending_edits[prompt_msg_id] = draft["id"]

    # -------------------------------------------------------- message (edit reply)

    def _handle_message(self, msg: dict) -> None:
        chat_id = msg.get("chat", {}).get("id")
        if chat_id != self.cfg.telegram_chat_id:
            return
        reply_to = msg.get("reply_to_message") or {}
        reply_to_id = reply_to.get("message_id")
        if not reply_to_id or reply_to_id not in self._pending_edits:
            # Not an edit reply — ignore. (Could be a /start or random message.)
            return

        draft_id = self._pending_edits.pop(reply_to_id)
        new_body = (msg.get("text") or "").strip()
        if not new_body:
            self.tg.notify_text(f"Empty edit ignored for draft #{draft_id}.")
            return

        draft = db.get_draft(draft_id)
        if not draft:
            self.tg.notify_text(f"Draft #{draft_id} not found.")
            return

        db.update_draft_body(draft_id, new_body)
        prospect = db.get_prospect(draft["prospect_id"])
        campaign_name = None
        if prospect and prospect["campaign_id"]:
            campaign_row = db.get_campaign(int(prospect["campaign_id"]))
            campaign_name = campaign_row["name"] if campaign_row else None

        # Push a fresh approval card with the updated body. The old card stays
        # in chat history — we don't try to edit it because its message_id may
        # already be stale and Telegram only allows editing your own messages.
        new_msg_id = self.tg.push_draft_for_approval(
            draft_id=draft_id,
            kind=draft["kind"],
            body=new_body,
            prospect_name=prospect["full_name"] if prospect else None,
            prospect_company=prospect["company"] if prospect else None,
            prospect_url=prospect["linkedin_url"] if prospect else None,
            campaign_name=campaign_name,
        )
        db.set_draft_telegram_id(draft_id, new_msg_id)

    # -------------------------------------------------------- adapter send

    def _send_via_adapter(self, draft) -> None:
        send_draft_via_adapter(self.cfg, self.adapter, draft, source="telegram")


def send_draft_via_adapter(cfg: Config, adapter, draft, *, source: str = "cli") -> None:
    """Translate a pending_draft into the right adapter call. Updates DB on
    success. Caller handles exceptions for telemetry/UI feedback. Module-level
    so both the bot daemon and `send-approved` CLI can call it.

    When cfg.dry_run is True, the LinkedIn write is skipped but the local
    state still advances (prospect status, dm_count, draft marked sent) so
    the rest of the pipeline behaves identically to a real send. The action
    log records dry_run=True so the audit trail makes the distinction clear."""
    prospect = db.get_prospect(draft["prospect_id"])
    if not prospect:
        raise RuntimeError(f"prospect {draft['prospect_id']} missing")
    kind = draft["kind"]
    body = draft["body"]
    url = prospect["linkedin_url"]
    pid = prospect["id"]

    if kind == "connect_note":
        safety.check_cap(cfg, "connect")
        if cfg.dry_run:
            api_result = "dry_run"
        else:
            api_result = adapter.send_connection(url, note=body)
        db.set_status(pid, "connection_sent")
        db.log_action(pid, "connect", json.dumps({"note": body[:200], "via": source}),
                      api_result, cfg.dry_run)
    elif kind in ("dm1", "dm2", "dm3"):
        safety.check_cap(cfg, "dm")
        if cfg.dry_run:
            api_result = "dry_run"
        else:
            api_result = adapter.send_dm(url, body)
        db.set_status(pid, "dm_sent")
        db.record_message(pid, "outbound", body)
        db.record_dm(pid)
        db.log_action(pid, "dm", json.dumps({"kind": kind, "via": source}),
                      api_result, cfg.dry_run)
    else:
        raise RuntimeError(f"unknown draft kind {kind!r}")

    db.set_draft_status(draft["id"], "sent")


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    db.init_db()
    daemon = BotDaemon(cfg)
    try:
        daemon.run()
    finally:
        daemon.close()


if __name__ == "__main__":
    run()
