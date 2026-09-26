"""An eight-email cadence to one prospect, drafted as one thread.

The GTM brief asks for eight emails that read as one experienced person
progressively talking to the same prospect. Drafting eight emails
independently produces eight individually good emails that repeat each other,
so each touch here is drafted with explicit state:

  * what every earlier email said (handed to the drafter as the thread)
  * which piece of their LinkedIn activity each email used — the brief allows
    one piece in at most three emails, never three in a row, and that is
    enforced here at SELECTION, by never offering an ineligible piece, rather
    than by hoping a gate can spot a paraphrase afterwards
  * which guess, case study and closing register each email used, so none is
    repeated and consecutive emails never sound alike

What the brief asks for and what this module will do differ in one place,
deliberately. "If no or not-enough shared content, go after their roles and
profiles and speculate their problems" becomes a HYPOTHESIS per email, which
the drafter may raise as a pattern it has seen and ask about, never state (see
evidence.build_hypotheses). And when there is nothing fresh left to build a
main email on, the sequence STOPS, and says why, rather than padding: an email
built on nothing is how invention gets in.

Nothing here sends anything. Email sending does not exist.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import asdict, dataclass, field

from . import drafter as drafter_mod
from . import evidence as evidence_mod

MAIN = "email_main"
FOLLOWUP = "email_followup"


@dataclass(frozen=True)
class TouchPlan:
    number: int
    kind: str
    role: str
    brief: str
    basis: str          # what a main email is built on: see _choose_basis


PLAN: tuple[TouchPlan, ...] = (
    TouchPlan(1, MAIN, "opener",
              "First contact. Introduce yourself once, in the first person, "
              "leading with your own credential if the sender has one. Build "
              "the email on the one thing named below, say plainly what "
              "Agentic Labs does, and end with a curious question.",
              "opener"),
    TouchPlan(2, FOLLOWUP, "nudge",
              "A short follow-up to email 1. One or two lines and a question. "
              "It should read like a person bumping their own note, not a "
              "second pitch.",
              "none"),
    TouchPlan(3, MAIN, "a pattern we see",
              "Raise the guess below as something you come across often with "
              "people in their seat, cite the case study as a time it was "
              "solved, and ask whether any of it is true for them. Do not "
              "restate email 1.",
              "hypothesis"),
    TouchPlan(4, MAIN, "a different angle",
              "A different angle from email 3, built on what is named below, "
              "with a different case study. Curious, not persuasive.",
              "content"),
    TouchPlan(5, FOLLOWUP, "nudge",
              "A short follow-up to emails 3 and 4: one easy question about "
              "them. No pitch.",
              "none"),
    TouchPlan(6, MAIN, "the lever",
              "Built on the guess below. Offer a way they could use AI "
              "implementation, grounded in the case study, and ask whether it "
              "is something they are looking at. Never say anything about "
              "their career.",
              "lever"),
    TouchPlan(7, FOLLOWUP, "one useful question",
              "One genuinely useful question they could answer in a line, "
              "about how they work. Nothing else.",
              "none"),
    TouchPlan(8, FOLLOWUP, "closing the loop",
              "The last note. Say so plainly, leave the door open, and ask "
              "one easy question. No guilt and no pitch.",
              "none"),
)

MAX_CONTENT_USES = 3

# Closing registers for the main emails: the ones that end on a question,
# since the brief wants the emails curious. The easy-out registers suit the
# last note, which carries its own instruction instead.
_MAIN_CLOSINGS = ("direct_offer", "usefulness", "overlap", "worth_it")

# Case studies in rough order of how much a stranger can check them, used
# after any the guess itself points at.
_PROOF_ROTATION = ("OrionQ", "Evergrow", "Experial", "Gymed", "Tango",
                   "Microforge", "Bespoke")
_PROOF_BY_DOMAIN = {
    "fintech, payments, wealth, investment": ("Bespoke",),
    "enterprise, brand, consumer goods": ("Experial", "AI Website Generator"),
    "venture capital and startup scouting": ("Microforge",),
    "speed of delivery, campaign tooling": ("AI marketing tools", "Tango"),
}


# ------------------------------------------------------------------ state

@dataclass
class ContentItem:
    id: str
    kind: str                    # "post" | "repost"
    text: str


class ContentLedger:
    """Which email used which piece of their activity.

    "Do not use a single content piece in more than 3 emails (never 3 emails
    in a row)."
    """

    def __init__(self, uses: dict[str, list[int]] | None = None) -> None:
        self.uses: dict[str, list[int]] = uses if uses is not None else {}

    def allows(self, item_id: str, touch: int) -> bool:
        used = self.uses.get(item_id, [])
        if len(used) >= MAX_CONTENT_USES:
            return False
        return not (touch - 1 in used and touch - 2 in used)

    def record(self, item_id: str, touch: int) -> None:
        if not self.allows(item_id, touch):
            raise ValueError(f"{item_id} may not be used in email {touch}")
        self.uses.setdefault(item_id, []).append(touch)


@dataclass
class Touch:
    number: int
    kind: str
    role: str
    subject: str
    body: str
    words: int
    content_id: str | None = None
    hypothesis: str | None = None
    proof_point: str | None = None
    closing: str | None = None
    positioning: str | None = None
    attempts: list[str] = field(default_factory=list)


@dataclass
class SequenceState:
    prospect_id: int
    first_name: str | None
    tier: str
    content: list[ContentItem]
    hypotheses: list[dict]
    content_themes: list[str] = field(default_factory=list)
    subject: str | None = None
    touches: list[Touch] = field(default_factory=list)
    content_uses: dict[str, list[int]] = field(default_factory=dict)
    failed_attempts: list[str] = field(default_factory=list)
    stopped: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------- selection

def content_items(posts) -> list[ContentItem]:
    """Their activity as numbered pieces: their own posts first."""
    own, shared = [], []
    for post in posts or []:
        text = (getattr(post, "text", None) or
                (post.get("text") if isinstance(post, dict) else "") or "")
        if not text.strip():
            continue
        (shared if evidence_mod.is_repost(post) else own).append(" ".join(text.split()))
    return ([ContentItem(f"post-{i}", "post", t) for i, t in enumerate(own, 1)]
            + [ContentItem(f"repost-{i}", "repost", t)
               for i, t in enumerate(shared, 1)])


_STOP = set("""about after again against being because before below between
could doing during every having their there these those through under until
which while would without yourself people really thing things other still
think always never great today years first""".split())


def content_themes(items: list[ContentItem], limit: int = 3) -> list[str]:
    """Words that recur across different pieces of what they share.

    "Identify patterns in the kind of content shared." Counted, not
    interpreted: a word that turns up in two or more separate posts is a
    theme; what it means about them is not ours to say.
    """
    if len(items) < 2:
        return []
    seen: Counter = Counter()
    for item in items:
        words = {w for w in re.findall(r"[a-z]{5,}", item.text.lower())
                 if w not in _STOP}
        seen.update(words)
    recurring = [(n, w) for w, n in seen.items() if n >= 2]
    recurring.sort(key=lambda nw: (-nw[0], nw[1]))
    return [w for _, w in recurring[:limit]]


def _pick_content(items, ledger, touch, avoid=()) -> ContentItem | None:
    fresh = [c for c in items if c.id not in ledger.uses]
    for candidate in fresh + items:
        if candidate.id in avoid:
            continue
        if ledger.allows(candidate.id, touch):
            return candidate
    return None


def _pick_hypothesis(hypotheses, used, *, lever: bool) -> evidence_mod.Evidence | None:
    for h in hypotheses:
        if h.name in used:
            continue
        if (h.name == evidence_mod.NEXT_LEVER) == lever:
            return h
    return None


def _choose_basis(plan, state_items, hypotheses, ledger, used_hyps, last_content):
    """(content, hypothesis) for a main email, or (None, None) if nothing is
    left that has not already been used up."""
    n = plan.number
    if plan.basis == "opener":
        content = _pick_content(state_items, ledger, n)
        if content:
            return content, None
        return None, _pick_hypothesis(hypotheses, used_hyps, lever=False)
    if plan.basis == "hypothesis":
        hyp = (_pick_hypothesis(hypotheses, used_hyps, lever=False)
               or _pick_hypothesis(hypotheses, used_hyps, lever=True))
        if hyp:
            return None, hyp
        return _pick_content(state_items, ledger, n), None
    if plan.basis == "content":
        content = _pick_content(state_items, ledger, n, avoid=(last_content,))
        if content:
            return content, None
        hyp = (_pick_hypothesis(hypotheses, used_hyps, lever=False)
               or _pick_hypothesis(hypotheses, used_hyps, lever=True))
        if hyp:
            return None, hyp
        return _pick_content(state_items, ledger, n), None
    if plan.basis == "lever":
        hyp = (_pick_hypothesis(hypotheses, used_hyps, lever=True)
               or _pick_hypothesis(hypotheses, used_hyps, lever=False))
        if hyp:
            return None, hyp
        return _pick_content(state_items, ledger, n), None
    return None, None


def _choose_proof(hypothesis, domain, used, brief) -> tuple[str, str, bool] | None:
    names = list(evidence_mod.hypothesis_proof(hypothesis.name if hypothesis else None))
    names += list(_PROOF_BY_DOMAIN.get(domain or "", ()))
    names += list(_PROOF_ROTATION)
    for name in names:
        if name in used:
            continue
        found = evidence_mod.proof_point_brief(name, brief)
        if found:
            return name, found[0], found[1]
    return None


def _choose_closing(key: str, touch: int, recent: list[str | None]):
    by_name = {c.name: c for c in evidence_mod._CLOSINGS}
    options = [by_name[n] for n in _MAIN_CLOSINGS if n in by_name]
    digest = hashlib.sha256(f"closing:{key}:{touch}".encode("utf-8")).digest()
    start = digest[0] % len(options)
    for i in range(len(options)):
        candidate = options[(start + i) % len(options)]
        if candidate.name not in recent:
            return candidate
    return options[start]


def _touch_evidence(facts, direct, full, content, hypothesis, *,
                    closing=None, positioning=None, domain=None) -> dict:
    """The evidence ONE email may draw on: the facts, plus only the piece of
    activity and the guess this email was given. Signals travel with the post
    that produced them, so a claim licensed by post A cannot surface in an
    email that was not given post A."""
    posts = ([{"text": content.text, "is_repost": content.kind == "repost"}]
             if content else [])
    bundle = evidence_mod.build_evidence(facts, posts,
                                         transition_is_direct=direct)
    if hypothesis is not None:
        bundle.items.append(hypothesis)
    # What we do not know is a fact about the prospect, not about this email.
    bundle.unknowns = list(full.unknowns)
    return bundle.as_dict(closing=closing, positioning=positioning,
                          proof_domain=domain)


# ------------------------------------------------------------ generation

def generate(prospect_id: int, facts, posts, *, brief: str,
             sign_off: str | None = None, direct: bool = False,
             today=None, touches: int = 8, draft_fn=None) -> SequenceState:
    """Draft the cadence for one prospect, one email at a time.

    `brief` is the approved company brief — case studies are cited from it
    verbatim. `draft_fn` defaults to the real drafter; tests pass a stub.
    """
    draft_fn = draft_fn or drafter_mod.draft
    full = evidence_mod.build_evidence(facts, posts, transition_is_direct=direct)
    hypotheses = evidence_mod.build_hypotheses(facts, today=today)
    items = content_items(posts)
    state = SequenceState(
        prospect_id=prospect_id,
        first_name=evidence_mod._first_name(getattr(facts, "full_name", None)),
        tier=full.tier.value,
        content=items,
        hypotheses=[h.as_dict() for h in hypotheses],
        content_themes=content_themes(items))
    if full.tier is evidence_mod.Tier.NONE:
        state.stopped = ("not enough verified about them to write a first "
                         "email — nothing drafted")
        return state

    key = (getattr(facts, "provider_id", None)
           or getattr(facts, "linkedin_url", None) or str(prospect_id))
    domain = evidence_mod.matching_proof_domain(facts, full)
    ledger = ContentLedger(state.content_uses)
    used_hyps: set[str] = set()
    used_proofs: list[str] = []
    last_hypothesis = None
    last_content_id = None

    for plan in PLAN[:touches]:
        n = plan.number
        content = hypothesis = proof = closing = positioning = None

        if plan.kind == MAIN:
            content, hypothesis = _choose_basis(plan, items, hypotheses, ledger,
                                                used_hyps, last_content_id)
            if content is None and hypothesis is None:
                state.stopped = (f"email {n}: nothing fresh left to build it "
                                 f"on — stopping rather than padding")
                break
            recent = [t.closing for t in state.touches[-2:]]
            closing = _choose_closing(key, n, recent)
            if n == 1:
                positioning = evidence_mod.choose_positioning(facts, full,
                                                              f"{key}:1")
            if n > 1 or (positioning and positioning.name == "proof_point"):
                proof = _choose_proof(hypothesis, domain, used_proofs, brief)
        else:
            # A follow-up asks about the email it follows. It carries that
            # email's guess, so the gate that keeps a guess a question
            # applies to the follow-up too, and no post of its own.
            hypothesis = last_hypothesis

        spec = {
            "number": n, "of": touches, "kind": plan.kind, "role": plan.role,
            "brief": plan.brief,
            "words": ({"min": drafter_mod.MAIN_WORDS[0],
                       "max": drafter_mod.MAIN_WORDS[1]} if plan.kind == MAIN
                      else {"max": drafter_mod.FOLLOWUP_MAX_WORDS}),
            "subject": None if n == 1 else f"Re: {state.subject}",
            "content": ({"id": content.id, "kind": content.kind,
                         "text": content.text} if content else None),
            "hypothesis": ({"name": hypothesis.name,
                            "statement": hypothesis.statement,
                            "asks": hypothesis.asks} if hypothesis else None),
            "proof_point": ({"name": proof[0], "brief": proof[1],
                             "citable": proof[2]} if proof else None),
            "content_themes": state.content_themes if n == 1 else [],
            "previous_emails": [{"number": t.number, "subject": t.subject,
                                 "body": t.body} for t in state.touches],
            "already_used": {"case_studies": list(used_proofs),
                             "guesses": sorted(used_hyps)},
        }
        evidence = _touch_evidence(
            facts, direct, full, content, hypothesis,
            closing=closing, positioning=positioning,
            domain=domain if positioning else None)
        recent_posts = ([{"text": content.text, "posted_at": None,
                          "is_repost": content.kind == "repost"}]
                        if content else [])

        tries: list = []
        try:
            raw = draft_fn(plan.kind, prospect_id, recent_posts=recent_posts,
                           evidence=evidence, attempts_out=tries, touch=spec)
        except drafter_mod.DrafterError as exc:
            state.failed_attempts = [str(t) for t in tries]
            state.stopped = (f"email {n} could not be drafted: "
                             f"{str(exc).splitlines()[0]}")
            break

        subject, body = drafter_mod.parse_email(raw)
        if n == 1:
            state.subject = subject
        if content:
            ledger.record(content.id, n)
            last_content_id = content.id
        if plan.kind == MAIN:
            if hypothesis:
                used_hyps.add(hypothesis.name)
            last_hypothesis = hypothesis
            if proof:
                used_proofs.append(proof[0])
        state.touches.append(Touch(
            number=n, kind=plan.kind, role=plan.role,
            subject=state.subject if n == 1 else f"Re: {state.subject}",
            body=body, words=drafter_mod.cadence_words(body, sign_off),
            content_id=content.id if content else None,
            hypothesis=hypothesis.name if hypothesis else None,
            proof_point=proof[0] if proof else None,
            closing=closing.name if closing else None,
            positioning=positioning.name if positioning else None,
            attempts=[str(t) for t in tries]))
    return state


# ---------------------------------------------------------------- review

def render_review(state: SequenceState, *, prospect_label: str,
                  sender_label: str, campaign_label: str) -> str:
    """The whole thread on one page, with what each email was built on.

    Written for the person judging the cadence as a cadence: the question is
    whether eight emails read as one person progressing, and that can only be
    seen all together.
    """
    main_band = drafter_mod.MAIN_WORDS
    lines = [
        f"# Eight-email cadence — first cut",
        "",
        f"**Prospect:** {prospect_label}  ",
        f"**Sender:** {sender_label}  ",
        f"**Campaign:** {campaign_label}",
        "",
        "## What the emails had to work with",
        "",
        f"- Evidence level: **{state.tier}**",
        f"- Their own posts: {sum(c.kind == 'post' for c in state.content)}; "
        f"reposts: {sum(c.kind == 'repost' for c in state.content)}",
    ]
    if state.content_themes:
        lines.append(f"- Recurring themes in what they share: "
                     f"{', '.join(state.content_themes)}")
    lines.append("- Our guesses from their role (asked about, never stated):")
    for h in state.hypotheses:
        lines.append(f"  - `{h.get('name')}`: {h.get('statement')}")
    if not any(h.get("name") == evidence_mod.NEXT_LEVER for h in state.hypotheses):
        lines.append(f"- No time-in-role angle: under "
                     f"{evidence_mod.ROLE_TENURE_YEARS} years in the current "
                     f"role and under {evidence_mod.EMPLOYER_TENURE_YEARS} "
                     f"continuous years at the employer.")
    lines.append("")

    for t in state.touches:
        label = "main" if t.kind == MAIN else "follow-up"
        built = []
        if t.content_id:
            built.append(f"their activity `{t.content_id}`")
        if t.hypothesis:
            built.append(f"guess `{t.hypothesis}`")
        meta = [f"{label} · {t.words} words"]
        if built:
            meta.append("built on " + " + ".join(built))
        if t.proof_point:
            meta.append(f"cites {t.proof_point}")
        if t.closing:
            meta.append(f"closing `{t.closing}`")
        if len(t.attempts) > 1:
            meta.append(f"{len(t.attempts)} attempts")
        lines += [f"## Email {t.number} — {t.role}", "",
                  f"*{' · '.join(meta)}*", "",
                  f"**Subject:** {t.subject}", ""]
        lines += [f"> {line}" if line else ">" for line in t.body.splitlines()]
        lines.append("")

    if state.stopped:
        lines += ["## Stopped early", "", state.stopped, ""]
        for attempt in state.failed_attempts:
            lines.append(f"- {attempt}")
        lines.append("")

    mains = [t for t in state.touches if t.kind == MAIN]
    follows = [t for t in state.touches if t.kind == FOLLOWUP]
    lines += ["## Checks against the brief", ""]
    if state.first_name and state.subject:
        words = re.findall(r"[A-Za-z0-9'’-]+", state.subject)
        position = next((i + 1 for i, w in enumerate(words[:2])
                         if w.lower() == state.first_name.lower()), None)
        lines.append(f"- Subject \"{state.subject}\": first name is word "
                     f"{position}; emails 2 onward reply as \"Re: …\"")
    lines.append(f"- Main emails {main_band[0]}–{main_band[1]} words: "
                 + ", ".join(f"email {t.number} ({t.words})" for t in mains))
    lines.append(f"- Follow-ups, at most {drafter_mod.FOLLOWUP_MAX_WORDS} words "
                 f"and each asking a question: {len(follows)} — "
                 + ", ".join(f"email {t.number} ({t.words})" for t in follows))
    for item_id, used in sorted(state.content_uses.items()):
        lines.append(f"- `{item_id}` used in emails "
                     f"{', '.join(map(str, used))} (at most "
                     f"{MAX_CONTENT_USES}, never three in a row)")
    proofs = [t.proof_point for t in state.touches if t.proof_point]
    if proofs:
        lines.append(f"- Case studies, each once: {', '.join(proofs)}")
    lines.append("- Every email passed every drafter gate: no claim about "
                 "their problems unless they published it, no guess stated "
                 "as fact, nothing about their career, only approved claims "
                 "about us.")
    lines += ["", "NOT SENT. Email sending does not exist; this is a draft "
              "for review."]
    return "\n".join(lines) + "\n"
