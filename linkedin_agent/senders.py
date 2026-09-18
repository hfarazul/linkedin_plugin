"""Who a message is from, and what that person may claim about themselves.

Claims about the COMPANY live in `campaigns/_agentic_labs.md`, shared by every
campaign and every sender. Claims about a PERSON cannot live there, because
they are only true of that person.

This was forced by a real requirement. The GTM brief asked every email to
"lead with the authority of two decades of experience across sectors and
leadership profiles". No company source supports that — the site lists 15+
developers and 10+ industries, and gives no seniority figure at all. Asked
directly, Manav confirmed it is his own personal experience: "valid for me, but
will be different for someone else".

So it is a sender credential. Written into the company brief it would be
asserted by Haque, by anyone who sends next, and — worse — as "our team brings
two decades", which is false even for Manav, because he said personal, not
collective. It belongs to the sender, is used only when he is the sender, and
only in his own voice.

A sender is chosen by, in order:
    1. `sender:` in the campaign brief's frontmatter
    2. the OUTREACH_SENDER environment variable
    3. DEFAULT_SENDER

A named sender with no profile is a hard error rather than a fallback. Quietly
signing with a different person's name is worse than failing to draft.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .campaigns import _parse_frontmatter

ROOT = Path(__file__).resolve().parent.parent
SENDERS_DIR = ROOT / "senders"

# Preserves the behaviour that existed before senders did: every email was
# signed by Haque.
DEFAULT_SENDER = "haque"


class SenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Sender:
    slug: str
    name: str                     # what they sign as
    full_name: str
    company: str
    calendar_url: str | None
    # A personal seniority claim this person has confirmed as their own, or
    # None. Its presence is what licenses the decades-class phrasings in the
    # drafter's authority gate — and only in the first person.
    personal_seniority: str | None
    brief: str                    # approved personal credentials, markdown
    path: Path

    @property
    def sign_off(self) -> str:
        return f"Best,\n{self.name}\n{self.company}"

    def as_dict(self) -> dict:
        """The payload the drafter prompt sees."""
        return {
            "name": self.name,
            "full_name": self.full_name,
            "company": self.company,
            "sign_off": self.sign_off,
            "calendar_url": self.calendar_url,
            "personal_seniority": self.personal_seniority,
            "credentials": self.brief,
        }


def path_for(slug: str) -> Path:
    return SENDERS_DIR / f"{slug}.md"


def load(slug: str) -> Sender:
    path = path_for(slug)
    if not path.exists():
        raise SenderError(
            f"no sender profile at {path}. Refusing to fall back to another "
            f"sender: an email signed with the wrong person's name is worse "
            f"than no email.")
    meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    for required in ("name", "company"):
        if not meta.get(required):
            raise SenderError(f"sender profile {path} is missing `{required}`")
    return Sender(
        slug=meta.get("slug", slug),
        name=meta["name"],
        full_name=meta.get("full_name") or meta["name"],
        company=meta["company"],
        calendar_url=meta.get("calendar_url") or None,
        personal_seniority=meta.get("personal_seniority") or None,
        brief=body.strip(),
        path=path,
    )


def resolve(campaign_sender: str | None = None) -> Sender:
    """The sender for a draft, per the precedence in the module docstring."""
    slug = (campaign_sender
            or os.environ.get("OUTREACH_SENDER", "").strip()
            or DEFAULT_SENDER)
    return load(slug)
