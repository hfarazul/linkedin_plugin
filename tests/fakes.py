"""In-memory test doubles. Mirror the public surface of the real clients so
tests can monkeypatch them in without touching HTTP."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class _SentMessage:
    text: str
    reply_markup: dict | None = None
    parse_mode: str | None = "Markdown"


@dataclass
class _Edit:
    message_id: int
    text: str
    reply_markup: dict | None = None


@dataclass
class _Draft:
    draft_id: int
    kind: str
    body: str
    prospect_name: str | None
    prospect_company: str | None = None
    prospect_url: str | None = None
    campaign_name: str | None = None
    inbound_excerpt: str | None = None
    telegram_message_id: int = 0


@dataclass
class _ReplyNotif:
    prospect_name: str | None
    prospect_company: str | None
    body: str
    thread_url: str | None


@dataclass
class _CallbackAnswer:
    callback_query_id: str
    text: str | None


class FakeTelegramClient:
    """Drop-in replacement for linkedin_agent.telegram.TelegramClient.

    Records every call to its public methods so tests can assert on them.
    Returns deterministic monotonically-increasing message_ids."""

    def __init__(self, cfg: Any = None) -> None:
        self.cfg = cfg
        self._next_id = 100
        self.sent: list[_SentMessage] = []
        self.edits: list[_Edit] = []
        self.drafts_pushed: list[_Draft] = []
        self.replies_notified: list[_ReplyNotif] = []
        self.edit_requests: list[int] = []   # draft_ids
        self.callback_answers: list[_CallbackAnswer] = []
        self.marked_sent: list[tuple[int, str, str]] = []     # (message_id, body, when)
        self.marked_rejected: list[tuple[int, str]] = []
        self.marked_errored: list[tuple[int, str, str]] = []
        self._closed = False

    def _alloc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def close(self) -> None:
        self._closed = True

    # ----- mirror TelegramClient surface ------------------------------------

    def send_message(self, text, *, reply_markup=None, parse_mode="Markdown", disable_preview=True) -> int:
        self.sent.append(_SentMessage(text=text, reply_markup=reply_markup, parse_mode=parse_mode))
        return self._alloc_id()

    def edit_message(self, message_id, new_text, *, reply_markup=None, parse_mode="Markdown") -> None:
        self.edits.append(_Edit(message_id=message_id, text=new_text, reply_markup=reply_markup))

    def answer_callback(self, callback_query_id, text=None) -> None:
        self.callback_answers.append(_CallbackAnswer(callback_query_id, text))

    def push_draft_for_approval(self, draft_id, kind, body, prospect_name,
                                prospect_company=None, prospect_url=None, campaign_name=None,
                                inbound_excerpt=None) -> int:
        mid = self._alloc_id()
        self.drafts_pushed.append(_Draft(
            draft_id=draft_id, kind=kind, body=body,
            prospect_name=prospect_name, prospect_company=prospect_company,
            prospect_url=prospect_url, campaign_name=campaign_name,
            inbound_excerpt=inbound_excerpt,
            telegram_message_id=mid,
        ))
        return mid

    def mark_draft_sent(self, message_id, original_body, when) -> None:
        self.marked_sent.append((message_id, original_body, when))

    def mark_draft_rejected(self, message_id, original_body) -> None:
        self.marked_rejected.append((message_id, original_body))

    def mark_draft_error(self, message_id, original_body, error, draft_id=None) -> None:
        self.marked_errored.append((message_id, original_body, error))
        # Mirror real behaviour: record an edit with retry/giveup buttons
        # keyed off draft_id so tests can assert on the callback payload.
        if draft_id is not None:
            self.edits.append(_Edit(
                message_id=message_id,
                text=f"⚠️ Send failed: {error}\n\n{original_body}",
                reply_markup={
                    "inline_keyboard": [[
                        {"text": "🔄 Retry", "callback_data": f"retry:{draft_id}"},
                        {"text": "❌ Give up", "callback_data": f"giveup:{draft_id}"},
                    ]]
                },
            ))

    def request_edit(self, draft_id, original_body) -> int:
        self.edit_requests.append(draft_id)
        return self._alloc_id()

    def notify_reply(self, prospect_name, prospect_company, body, thread_url=None) -> int:
        self.replies_notified.append(_ReplyNotif(prospect_name, prospect_company, body, thread_url))
        return self._alloc_id()

    def notify_text(self, text) -> int:
        self.sent.append(_SentMessage(text=text, parse_mode=None))
        return self._alloc_id()

    def get_updates(self, offset=None, timeout=25) -> list[dict]:
        # Tests that need updates inject them directly via the daemon's dispatch.
        return []


# ----- provider fakes -------------------------------------------------------

class FakeProvider:
    """A CapabilityProvider that serves canned normalized objects.

    Replaces the respx/HTTP mocking that targeted Unipile's endpoints. Those
    tests proved the pipeline worked *through Unipile's transport*; with the
    provider removed, the assertions that still matter are about pipeline
    behaviour, so they are retargeted here rather than deleted.
    """

    name = "phantombuster"
    is_async = False

    def __init__(self, *, profile=None, inbox=None, accepted=None,
                 hits=None, posts=None, supports_all=True, raises=None):
        self._profile = profile
        self._inbox = inbox or []
        self._accepted = accepted
        self._hits = hits or []
        self._posts = posts or []
        self._supports_all = supports_all
        self._raises = raises
        self.calls: list = []

    def supports(self, capability):
        return self._supports_all

    def verification(self, capability):
        return "verified"

    def _maybe_raise(self):
        if self._raises:
            raise self._raises

    def fetch_profile(self, identifier, *, with_experience=False):
        self.calls.append(("fetch_profile", identifier))
        self._maybe_raise()
        return self._profile

    def check_acceptance(self, identifier):
        self.calls.append(("check_acceptance", identifier))
        self._maybe_raise()
        return self._accepted

    def fetch_inbox(self, limit=50):
        self.calls.append(("fetch_inbox", limit))
        self._maybe_raise()
        return list(self._inbox)

    def search_people(self, query, limit=20):
        self.calls.append(("search_people", query))
        self._maybe_raise()
        return list(self._hits)[:limit]

    def get_recent_posts(self, linkedin_url, limit=5, *, include_reposts=False):
        self.calls.append(("get_recent_posts", linkedin_url))
        self._maybe_raise()
        return list(self._posts)[:limit]

    def close(self):
        self.calls.append(("close",))


def fake_router(provider):
    from linkedin_agent.providers.router import CapabilityRouter
    return CapabilityRouter(provider, None)
