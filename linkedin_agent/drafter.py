from __future__ import annotations

# The drafter shells out to `claude -p` using the same system prompt as the
# interactive message-drafter subagent. Both code paths read the prompt from
# .claude/agents/message-drafter.md so there's a single source of truth.
#
# No Anthropic API key needed — this uses your existing Claude Code auth.

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Sequence

from . import campaigns as campaigns_mod
from . import evidence as evidence_mod
from . import senders as senders_mod
from . import db

ROOT = Path(__file__).resolve().parent.parent
SUBAGENT_PATH = ROOT / ".claude" / "agents" / "message-drafter.md"

INSUFFICIENT = "INSUFFICIENT_CONTEXT"


class DrafterError(RuntimeError):
    pass


# Length caps mirror the rules in the subagent prompt. We enforce them
# defensively in code as well — a model can still go over and we want to catch
# it before sending to LinkedIn (which would reject a >300-char connect note).
KIND_MAX_CHARS = {
    "connect_note": 300,
    "dm1": 600,
    "dm2": 400,
    "dm3": 200,
    # Replies use the same surface as DM1 (a single DM in the thread) but the
    # right reply is often shorter than an initiation — just enough to answer
    # the inbound and pose the next move. 600 cap, 400 sweet spot.
    "reply": 600,
    # Email has more room than a LinkedIn DM but is not a newsletter. The cap
    # is generous enough for a hook, positioning and a CTA without inviting a
    # wall of text that reads as a template.
    "email1": 1200,
    # The eight-email cadence. Its real bound is in words (MAIN_WORDS,
    # FOLLOWUP_MAX_WORDS), which is how the GTM brief states it; these are
    # backstops so an absurd output is caught before the word count runs.
    "email_main": 1100,
    "email_followup": 450,
}

# Minimum length per kind — anything shorter is almost always a degenerate
# output (e.g. "I'll wait for your call on which path to take." was 46 chars
# for a connect_note that should be ~200). Triggers a retry.
KIND_MIN_CHARS = {
    "connect_note": 100,
    "dm1": 350,        # was 200 — DM1 now requires hook + positioning + (opt proof) + CTA
    "dm2": 120,
    "dm3": 50,
    # Replies can be brief (e.g. "yes, Tuesday 2pm works — booked.") but a
    # one-word ack is almost always wrong on first reply. 80 keeps room for
    # acknowledge + content + sign-off.
    "reply": 80,
    "email1": 250,
    # Backstops only, set low so the word band is what fires: its retry hint
    # speaks the brief's language (words), a character floor's does not.
    "email_main": 100,
    "email_followup": 40,
}

# Auto-retry budget. The drafter is stochastic — a fresh `claude -p` call
# usually fixes oversize/empty/short outputs. INSUFFICIENT_CONTEXT is terminal
# (no retry) because that's the drafter being honestly stuck.
MAX_DRAFT_ATTEMPTS = 3

# Spam-tell substrings — the openers spam-detection (human and algorithmic)
# keys on. The subagent prompt forbids these but the model occasionally
# generates them anyway under pressure. Catching them post-hoc gives us a
# belt to the prompt's suspenders.
#
# Matched case-insensitively against the full draft body. Whole-phrase
# matching only (no partial substrings inside larger words).
SPAM_TELLS = (
    "i came across your profile",
    "i came across your",
    "i noticed you",
    "i saw your profile",
    "i'd love to connect",
    "i'd love to chat",
    "i would love to connect",
    "i would love to chat",
    "hope you're doing well",
    "hope this finds you well",
    "your impressive work",
    "your impressive background",
    "open to a quick call",
    "open to a quick chat",
)


def _contains_spam_tell(body: str) -> str | None:
    """Return the matched spam-tell phrase, or None if clean."""
    # Typographic apostrophes first: a model writes "I'd love to connect" with
    # U+2019, and every entry here is written with an ASCII quote. Without this
    # the phrase matches nothing and goes out.
    low = _normalise_quotes(body.lower())
    for phrase in SPAM_TELLS:
        if phrase in low:
            return phrase
    return None


# Details that reveal the message was assembled from a scraped profile rather
# than written by someone who noticed something. A prospect should feel read
# about, not surveilled.
#
# "the move to a new stealth venture" reads as human attention.
# "started February 2026" reads as a database row, because it is one — and it
# invites the obvious question of where we got it.
_SURVEILLANCE_TELLS = (
    # ISO or slashed dates: 2026-02, 2026/02, 02/2026
    re.compile(r"\b(19|20)\d{2}[-/]\d{1,2}\b"),
    re.compile(r"\b\d{1,2}[-/](19|20)\d{2}\b"),
    # "in March 2026", "since Feb 2026" — a month-year stamp next to a verb
    # that only makes sense if we looked it up.
    re.compile(r"(?i)\b(?:since|in|from|started|joined|as of)\s+"
               r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+"
               r"(?:19|20)\d{2}\b"),
    # Headcount straight off a company page.
    re.compile(r"(?i)\b\d{1,6}\s*(?:\+\s*)?employees\b"),
    re.compile(r"(?i)\b(?:employee count|headcount) of \d+"),
)


# Phrasings that assert the prospect has a business problem. Matched only when
# no SIGNAL licenses such a claim -- see linkedin_agent/evidence.py. A verified
# career move is not evidence of a tooling problem, and the old email asserted
# one anyway, to a real person, at their real address.
#
# When a claim IS licensed these are allowed through, and whether the claim
# stays tied to the signal that licensed it is enforced by the prompt, not
# here. A regex cannot check relevance; it can check that we are not
# diagnosing strangers.
_PAIN_CLAIM_PATTERNS = (
    re.compile(r"(?i)\byou(?:'re| are) (?:probably|likely|no doubt|almost "
               r"certainly)\b"),
    re.compile(r"(?i)\b(?:most|many) (?:teams|founders|companies|operators) "
               r"(?:at|in) (?:that|this|your)\b"),
    re.compile(r"(?i)\bteams? (?:building|operating|scaling) at (?:that|this) "
               r"(?:stage|point)\b"),
    re.compile(r"(?i)\bend up (?:rebuilding|building|doing|wiring|stitching)\b"),
    re.compile(r"(?i)\bthe real (?:squeeze|bottleneck|constraint|pain)\b"),
    re.compile(r"(?i)\b(?:struggling|wrestling|grappling) with\b"),
    re.compile(r"(?i)\bdrowning in\b"),
    re.compile(r"(?i)\byour (?:bottleneck|technical debt|tooling problem)\b"),
    re.compile(r"(?i)\bwithout hiring a (?:team|dev team)\b"),
)


def _contains_unsupported_pain_claim(body: str) -> str | None:
    """Return the phrase asserting an unevidenced problem, or None if clean."""
    body = _normalise_quotes(body)
    for pattern in _PAIN_CLAIM_PATTERNS:
        match = pattern.search(body)
        if match:
            return match.group(0)
    return None


# The softer move the drafter makes when it has nothing: instead of asserting
# a problem outright, it argues that the prospect's *category* is one where our
# work matters. From the first live run, for a prospect we knew one fact about:
#
#     "Multi-property, multi-country operations is a setting where that work
#      tends to matter"
#
# That is the same invention wearing a hedge. It reads as insight and contains
# none — we did not know it, we reasoned it from his job title, and reasoning
# a pain from a job title is the conversion this system exists to block.
#
# Applied only at the weak and none tiers. At moderate the prospect has given
# us something to react to and a cautious relevance argument is legitimate; at
# strong it is the point.
_INFERRED_RELEVANCE_PATTERNS = (
    re.compile(r"(?i)\bis (?:a|an|the) (?:setting|environment|context|world|"
               r"space|place) where\b"),
    re.compile(r"(?i)\btends to (?:matter|be|come up|bite|get|show up)\b"),
    re.compile(r"(?i)\b(?:usually|often|typically) (?:matters|comes up|where)\b"),
    re.compile(r"(?i)\bwhere that (?:kind of )?work (?:tends|usually|often|"
               r"matters)\b"),
    re.compile(r"(?i)\bthat(?:'s| is) (?:usually|often|typically) (?:where|when)\b"),
    re.compile(r"(?i)\bin (?:my|our) experience,? (?:teams|companies|operators)\b"),
)


# Dashes used as connective tissue. Not banned: people use them, and a rule
# that forbids them outright produces prose that reads artificially
# constrained, which is the same tell from the other direction.
#
# The problem is density and sameness. Across five real drafts the em dash
# appeared in nearly every sentence that joined an observation to an
# explanation, always the same construction, and combined with balanced
# clauses and careful hedging it produced an unmistakable copywriter texture.
# One is fine. Three is a fingerprint.
_DASH_CONNECTORS = ("—", "–", " - ")

MAX_CONNECTOR_DASHES = 1


def count_connector_dashes(body: str) -> int:
    return sum(body.count(d) for d in _DASH_CONNECTORS)


def _overuses_dashes(body: str) -> str | None:
    """Return a description when dashes are doing too much of the joining."""
    n = count_connector_dashes(body)
    if n > MAX_CONNECTOR_DASHES:
        return f"{n} dashes (soft limit {MAX_CONNECTOR_DASHES})"
    return None


def _contains_inferred_relevance(body: str) -> str | None:
    """Return the reasoned-from-nothing relevance claim, or None if clean."""
    body = _normalise_quotes(body)
    for pattern in _INFERRED_RELEVANCE_PATTERNS:
        match = pattern.search(body)
        if match:
            return match.group(0)
    return None


# Phrasings from the previous template. These are banned as *strings*, not as
# concepts: a prospect who publicly said they are rebuilding their data
# pipeline can still be told we build data pipelines. What cannot survive is
# the reusable scaffolding that made every email the same email with the nouns
# swapped.
_FILLER_TELLS = (
    "what caught my eye is the work you are doing",
    "teams building at that stage",
    "internal tooling and data pipelines",
    "take that load off",
    "tailored to how your company actually works",
    "that's our outside read",
    "thats our outside read",
    "our outside read",
    "go-to-market ops or product velocity",
    "somewhere we haven't surfaced",
    "somewhere we havent surfaced",
    "shipping without hiring a team",
    "walk through what we'd build",
    "walk through what wed build",
)


_normalise_quotes = evidence_mod.normalise_quotes


# Claims about US whose parts are individually approved but whose whole is not.
#
# `ungrounded_cortivo_claim` cannot see any of these, and that is structural
# rather than an oversight. It rejects three shapes: a proper noun absent from
# the brief, a DIGIT absent from the brief, and a practice assertion built from
# unknown words. None of these claims is any of the three:
#
#   * "two decades of experience" spelled out carries no digits at all.
#   * "a 3-4 person team" carries only digits the brief already approves for
#     unrelated claims — "Top 3% on Toptal", "4 minutes time-to-itinerary". The
#     gate checks whether a number is approved, not what it is approved FOR.
#
# So they get the mechanism that already works for phrasings that must simply
# never ship: literal deny-lists, matched like a spam tell. Three classes,
# because they are licensed differently.

# Never licensed, for anyone. No source documents a collective seniority figure
# for the team, and Manav — who holds the only documented seniority claim —
# confirmed his is personal, not collective.
_COLLECTIVE_SENIORITY_TELLS = (
    "combined experience", "collective experience", "collectively bring",
    "years of combined", "years of collective", "decades between us",
    "combined decades",
)

# Licensed only when the active sender's profile carries `personal_seniority`,
# and then only when attributed to the sender in the first person. The claim
# is true of one person; attached to "we" it becomes a claim about the team,
# which nobody has made.
_PERSONAL_SENIORITY_TELLS = (
    "two decades", "2 decades", "20 years", "twenty years",
    "decades of experience",
)

# Never licensed. The approved brief marks a fixed engagement length and a
# team-size equivalence as NOT APPROVED, and the old positioning catalogue
# stated both — so they are the phrasings a draft is most likely to reach for.
_UNAPPROVED_ENGAGEMENT_TELLS = (
    "three or four people", "3-4 person", "3 to 4 person", "team of three or",
    "one engineer's cost", "six to ten weeks", "6-10 weeks", "6 to 10 weeks",
    "kickoff to live users",
)

_FIRST_PERSON_SINGULAR = re.compile(r"(?i)\b(?:i|i've|i'm|i'd|my|me)\b")

# The literal lists above only catch the wordings someone thought of. Measured
# against them on 2026-09-26, eight of nine rewordings of the same claims got
# through both this gate and the brief-vocabulary gate: "six-to-ten weeks",
# "a 3 to 4-person team", "20+ years", "a twenty-year track record",
# "decades of combined engineering experience". A hyphen, a plus sign, a
# spelled-out number or one extra word is all it took.
#
# So the text is folded into one spelling first, and the claims are matched as
# shapes. Only sentences about US are checked: "you've spent 12 years at
# Deloitte" is about the prospect, and is not ours to license or refuse here.
_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "fifteen": "15", "twenty": "20",
    "thirty": "30",
}
_NUMBER_WORD = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b")
# "engagement" counts: in a message from an agency, "a typical engagement"
# is ours even with no pronoun in the sentence.
_ABOUT_US = re.compile(r"\b(?:i|i've|i'm|i'd|my|me|we|we've|we're|we'd|our|"
                       r"ours|us|team|agentic labs|engagements?)\b")
_RANGE = r"\d+(?:\s*(?:to|or|-)\s*|\s+)\d+"

# A duration only counts where the sentence is about delivering work. "Could
# we find time in the next 2 weeks?" is an ask, not an engagement length.
_DELIVERY_CONTEXT = re.compile(
    r"\b(?:ship|ships|shipped|shipping|build|builds|built|deliver|delivers|"
    r"delivered|engagements?|projects?|live|launch|launched|kickoff|kick off|"
    r"v1|mvp|typical|typically|usually|turnaround|go from|went from)\b")
_DURATION = re.compile(rf"\b(?:{_RANGE}|\d+)\s*(?:weeks?|months?)\b")
_TEAM_RANGE = re.compile(
    rf"\b{_RANGE}\s*(?:person|people|engineers?|developers?|devs|hires?)\b")
_ONE_ENGINEER_EQUIVALENCE = re.compile(
    r"\b1 (?:of our )?(?:engineers?|developers?)\b.{0,60}?"
    r"\b(?:cost|costs|replaces?|instead of|worth|equivalent|would otherwise)\b")
_COLLECTIVE = re.compile(r"\b(?:combined|collective|collectively|between us|"
                         r"together we)\b")
_SENIORITY_UNITS = re.compile(r"\b(?:years?|decades?|experience)\b")
# Ten years or more, or any decade. Haque's documented "5+ years" is a sender
# credential the brief-vocabulary gate already grounds, and stays sayable.
_SENIORITY = re.compile(r"\b[1-9]\d\s*years?\b|\bdecades?\b")


def _fold_authority_text(body: str) -> str:
    """One spelling for the many a claim can hide behind."""
    low = _normalise_quotes(body.lower())
    low = re.sub(r"(?<=[a-z0-9])-(?=[a-z0-9])", " ", low)   # six-to-ten, 4-person
    low = re.sub(r"(?<=\d)\s*\+", "", low)                   # 20+ years
    low = _NUMBER_WORD.sub(lambda m: _NUMBER_WORDS[m.group(1)], low)
    return re.sub(r"[ \t]+", " ", low)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _sentence_containing(text: str, phrase: str) -> str:
    for sentence in _sentences(text):
        if phrase in sentence:
            return sentence
    return text


def _contains_unsupported_authority(body: str,
                                    sender: dict | None = None) -> str | None:
    """Return the unlicensed claim about us, or None if clean.

    `sender` is the payload from `senders.Sender.as_dict()`. Without one, no
    personal seniority is licensed — the fail-closed direction.
    """
    low = _normalise_quotes(body.lower())
    for phrase in _COLLECTIVE_SENIORITY_TELLS + _UNAPPROVED_ENGAGEMENT_TELLS:
        if phrase in low:
            return phrase

    licensed = bool((sender or {}).get("personal_seniority"))
    for phrase in _PERSONAL_SENIORITY_TELLS:
        if phrase not in low:
            continue
        if not licensed:
            return phrase
        if not _FIRST_PERSON_SINGULAR.search(_sentence_containing(low, phrase)):
            return f"{phrase} (stated as the team's, not the sender's own)"

    for sentence in _sentences(_fold_authority_text(body)):
        if not _ABOUT_US.search(sentence):
            continue
        collective = _COLLECTIVE.search(sentence)
        if collective and _SENIORITY_UNITS.search(sentence):
            return f"{collective.group(0)} (a collective seniority claim)"
        for pattern in (_TEAM_RANGE, _ONE_ENGINEER_EQUIVALENCE):
            match = pattern.search(sentence)
            if match:
                return f"{match.group(0)} (a team-size equivalence)"
        if _DELIVERY_CONTEXT.search(sentence):
            match = _DURATION.search(sentence)
            if match:
                return f"{match.group(0)} (an engagement length)"
        match = _SENIORITY.search(sentence)
        if match:
            if not licensed:
                return f"{match.group(0)} (a seniority claim no source supports)"
            if not _FIRST_PERSON_SINGULAR.search(sentence):
                return (f"{match.group(0)} (stated as the team's, not the "
                        f"sender's own)")
    return None


# Reposts are the one piece of evidence that is somebody else's words. They
# were filtered out entirely because "quoting a repost back as the prospect's
# own thinking attributes someone else's words to them". They now reach the
# drafter as `interests`, labelled as reposts, and this is the backstop for
# the failure the filter used to prevent.
#
# "You shared" and "you reposted" are the honest verbs and are never matched.
# What is matched is authorship: "you wrote", "your post", "in your words".
_AUTHORSHIP = re.compile(
    r"(?i)\b(?:you (?:wrote|posted|said|argued|mentioned|noted|observed|"
    r"pointed out|made the point|put it)|"
    r"your (?:own )?(?:post|article|piece|words|take|point|line|thoughts?|"
    r"writing)|in your (?:post|words))\b")
_QUOTED_SPAN = re.compile(r'"[^"]{12,}"')


def _shingles(text: str, n: int = 4) -> list[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9']+", _normalise_quotes(text.lower()))
    return [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]


def _misattributes_repost(body: str, evidence: dict | None) -> str | None:
    """Return how the draft presents a repost as the prospect's words, or None.

    Two shapes. With nothing of their own to point at, any authorship language
    is about a repost, because that is all there is. With both, a sentence that
    attributes or quotes is checked for a run of words that appears in a repost
    and in nothing they wrote themselves.
    """
    interests = (evidence or {}).get("interests") or []
    if not interests:
        return None
    own = [o.get("detail") or "" for o in (evidence or {}).get("observations") or []]
    reposted = {s for i in interests for s in _shingles(i.get("detail") or "")}
    theirs = {s for text in own for s in _shingles(text)}
    for sentence in _sentences(_normalise_quotes(body)):
        attributed = _AUTHORSHIP.search(sentence)
        if not (attributed or _QUOTED_SPAN.search(sentence)):
            continue
        if attributed and not own:
            return (f"{attributed.group(0)!r}, but nothing they wrote is in "
                    f"the evidence — only posts they reposted")
        for shingle in _shingles(sentence):
            if shingle in reposted and shingle not in theirs:
                return (f"{' '.join(shingle)!r}, from a post they reposted, "
                        f"presented as their own words")
    return None


# ------------------------------------------------------------ the cadence
#
# Gates that exist only for the eight-email cadence (email_main and
# email_followup). Each one holds a line the GTM brief could otherwise be read
# as crossing. The brief's wording is quoted where it matters, because the
# point of each gate is the gap between what was asked for and what may be
# said to a real person.

CADENCE_KINDS = ("email_main", "email_followup")

# "At least 80 words long (and always less than 120 words)" for the main
# emails; follow-ups "light on words but asking questions".
MAIN_WORDS = (80, 119)
FOLLOWUP_MAX_WORDS = 50

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")


def email_text(body: str, sign_off: str | None = None) -> str:
    """The email itself: no subject line, no sign-off block."""
    text = parse_email(body)[1]
    if sign_off and sign_off.strip() in text:
        text = text[:text.rindex(sign_off.strip())]
    return text.strip()


def cadence_words(body: str, sign_off: str | None = None) -> int:
    return len(_WORD.findall(email_text(body, sign_off)))


def subject_problem(subject: str | None, first_name: str | None) -> str | None:
    """Why a first-touch subject fails the brief, or None.

    "Have their first name in the first or second word of the subject."
    """
    if not subject:
        return "no subject line"
    if not first_name:
        return None
    words = [w.strip("'’").lower() for w in _WORD.findall(subject)]
    if first_name.lower() not in words[:2]:
        return f"their first name ({first_name}) is not in the first two words"
    if len(words) > 9:
        return f"{len(words)} words — a subject line, not a sentence"
    return None


# The career angle. The brief asked the drafter to "speculate if the person is
# stuck in their career - and if yes, provide them a way out by being a hero".
# Tenure can shape which question is asked; it can never be said back to them
# as a verdict about their career, and a stranger who opens with one has told
# the prospect exactly how the email was written.
_CAREER_TELLS = re.compile(
    r"(?i)\b(?:stuck|plateau(?:ed|ing)?|stagnat\w*|your career|"
    r"career (?:move|change|growth|path|progression|next step)|"
    r"next chapter|next career|time for a change|outgr(?:ew|own|owing)|"
    r"been there (?:a while|too long)|way out|hero)\b")
_TENURE_FIGURE = re.compile(
    r"\b\d+ years? (?:at|with|in|as|running|leading|building)\b")


def _contains_career_diagnosis(body: str) -> str | None:
    text = _normalise_quotes(body)
    match = _CAREER_TELLS.search(text)
    if match:
        return match.group(0)
    for sentence in _sentences(_fold_authority_text(text)):
        if _SECOND_PERSON.search(sentence):
            figure = _TENURE_FIGURE.search(sentence)
            if figure:
                return f"{figure.group(0)} (their tenure as a figure)"
    return None


# A hypothesis is our guess from their role. It licenses a question — "is that
# true for you?" — and a pattern stated about other people — "something I see
# a lot with COOs". It does not license a statement about THEM. A sentence
# addressed to them that is not tentative, and carries the guess's own words,
# is the guess turned into a claim.
_SECOND_PERSON = re.compile(r"(?i)\b(?:you|your|you're|you've|yours)\b")
_TENTATIVE = re.compile(r"(?i)\b(?:whether|wonder|wondering|curious|if)\b")


def _states_hypothesis_as_fact(body: str, evidence: dict | None) -> str | None:
    hypotheses = (evidence or {}).get("hypotheses") or []
    if not hypotheses:
        return None
    for sentence in _sentences(_normalise_quotes(parse_email(body)[1])):
        clean = sentence.strip()
        if clean.endswith("?") or _TENTATIVE.search(clean):
            continue
        if not _SECOND_PERSON.search(clean):
            continue
        low = clean.lower()
        for h in hypotheses:
            for term in evidence_mod.hypothesis_terms(h.get("name")):
                if term in low:
                    return (f"{term!r}, said about them as a fact — the "
                            f"{h.get('name')} guess may only be asked about")
    return None


# The case studies are the company's work. The Experial entry names Haque as
# its builder; with anyone else sending, "I built Experial" is false. "We
# built" is true for every sender.
_FIRST_PERSON_BUILT = re.compile(
    r"(?i)\b(?:i|i've|i have|i had)\s+(?:personally\s+)?"
    r"(?:built|shipped|designed|developed|created|led the build of|made)\b")


def _claims_team_work_personally(body: str, brief: str) -> str | None:
    names = evidence_mod.proof_points(brief)
    for sentence in _sentences(_normalise_quotes(body)):
        if not _FIRST_PERSON_BUILT.search(sentence):
            continue
        for name in names:
            if name.lower() in sentence.lower():
                return f"{name} claimed in the first person singular"
    return None


def _repeats_previous_email(body: str, previous: Sequence[str],
                            sign_off: str | None = None,
                            n: int = 7) -> str | None:
    """A run of words lifted from an earlier email in the same thread.

    Eight emails that each read well alone and repeat each other's sentences
    are the failure the whole cadence has to avoid: one person progressing a
    conversation does not paste themselves.
    """
    mine = _shingles(email_text(body, sign_off), n)
    for earlier in previous:
        theirs = set(_shingles(email_text(earlier, sign_off), n))
        for shingle in mine:
            if shingle in theirs:
                return " ".join(shingle)
    return None


_LINK = re.compile(r"(?i)https?://|www\.|\b[a-z0-9-]+\.(?:ai|com|io)/\S")


def _contains_filler(body: str) -> str | None:
    """Return the recycled template phrase, or None if clean."""
    low = _normalise_quotes(body.lower())
    for phrase in _FILLER_TELLS:
        if phrase in low:
            return phrase
    return None


def _contains_surveillance_tell(body: str) -> str | None:
    """Return the matched scraped-detail phrase, or None if clean.

    Applied to email only. A LinkedIn DM sits inside LinkedIn, where seeing
    someone's profile is the medium; a cold email arriving with their start
    date in it is a different and worse experience.
    """
    for pattern in _SURVEILLANCE_TELLS:
        match = pattern.search(body)
        if match:
            return match.group(0)
    return None


@dataclass
class DrafterInput:
    kind: str
    campaign: dict
    prospect: dict
    recent_posts: list[dict] = field(default_factory=list)
    prior_messages: list[dict] = field(default_factory=list)
    # Typed evidence from linkedin_agent.evidence. When present it, not the
    # raw profile fields, is what the drafter is told to write from: the
    # prospect dict is a bag of strings with no indication of which ones
    # license a claim, and handing a model such a bag is how "changed jobs"
    # became "has a tooling problem".
    evidence: dict | None = None
    # Who the message is from: their sign-off, their calendar link, and the
    # personal credentials only they may claim. See linkedin_agent/senders.py.
    sender: dict | None = None
    # One email of the cadence: which touch it is, what it is for, what it
    # may use, and what the thread has already said. See sequence.py.
    touch: dict | None = None


# -------------------------------------------------------------- prompt loading

_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def _load_subagent_prompt() -> str:
    if not SUBAGENT_PATH.exists():
        raise DrafterError(f"subagent prompt missing at {SUBAGENT_PATH}")
    raw = SUBAGENT_PATH.read_text()
    return _FRONTMATTER_RE.sub("", raw, count=1).strip()


# -------------------------------------------------------------- context build

def _first_name(full_name: str | None) -> str | None:
    if not full_name:
        return None
    return full_name.split()[0]


def build_input(
    kind: str,
    prospect_id: int,
    recent_posts: Sequence[dict] | None = None,
    evidence: dict | None = None,
    touch: dict | None = None,
) -> DrafterInput:
    """Assemble the JSON payload the drafter prompt expects.
    The caller passes recent_posts because that comes from the adapter, not the DB."""
    if kind not in KIND_MAX_CHARS:
        raise DrafterError(f"invalid kind {kind!r}; expected one of {list(KIND_MAX_CHARS)}")

    prospect_row = db.get_prospect(prospect_id)
    if not prospect_row:
        raise DrafterError(f"prospect {prospect_id} not found")

    campaign_row = None
    if prospect_row["campaign_id"]:
        with db.connect() as conn:
            cur = conn.execute(
                "SELECT * FROM campaigns WHERE id = ?", (prospect_row["campaign_id"],)
            )
            campaign_row = cur.fetchone()

    campaign_sender = None
    if campaign_row:
        brief = campaigns_mod.load_brief(campaign_row["slug"])
        campaign_sender = brief.sender
        campaign_ctx = {
            "name": brief.name,
            "target_icp": brief.target_icp,
            "brief": brief.brief,
        }
    else:
        # Drafter still works without a campaign — useful for ad-hoc messages —
        # but quality drops significantly. The subagent prompt will likely
        # return INSUFFICIENT_CONTEXT.
        campaign_ctx = {"name": "(no campaign)", "target_icp": None, "brief": ""}

    # Prior thread for DM2/DM3/reply context. Replies need the full thread —
    # the inbound we're answering is the last row.
    prior: list[dict] = []
    if kind in ("dm2", "dm3", "reply"):
        with db.connect() as conn:
            cur = conn.execute(
                """SELECT direction, body, sent_at FROM messages
                   WHERE prospect_id = ? ORDER BY sent_at""",
                (prospect_id,),
            )
            prior = [dict(r) for r in cur.fetchall()]
    elif touch:
        # Nothing in the cadence has been sent, so the thread lives in the
        # sequence state rather than in `messages`.
        prior = [{"direction": "outbound", "touch": t.get("number"),
                  "subject": t.get("subject"), "body": t.get("body")}
                 for t in touch.get("previous_emails") or []]

    return DrafterInput(
        kind=kind,
        campaign=campaign_ctx,
        prospect={
            "full_name": prospect_row["full_name"],
            "first_name": _first_name(prospect_row["full_name"]),
            "headline": prospect_row["headline"],
            "company": prospect_row["company"],
            "title": prospect_row["title"],
            "pitch_context": prospect_row["pitch_context"],
        },
        # The prompt describes `recent_posts` as the prospect's posts, so only
        # their own go here. Reposts reach the drafter solely through
        # `evidence.interests`, where they are labelled as someone else's.
        recent_posts=[p for p in (recent_posts or [])
                      if not evidence_mod.is_repost(p)],
        prior_messages=prior,
        evidence=evidence,
        sender=senders_mod.resolve(campaign_sender).as_dict(),
        touch=touch,
    )


def _touch_block(touch: dict) -> str:
    """What this one email of the cadence is for, stated outside the JSON.

    Restated for the same reason the evidence tier is: it is binding, and a
    rule buried in a payload field competes with everything else there.
    """
    n, total = touch["number"], touch.get("of", 8)
    words = touch.get("words") or {}
    if touch.get("kind") == "email_followup":
        length = (f"At most {words.get('max')} words, not counting the "
                  f"sign-off. It must ask a question. No case study, no "
                  f"re-introduction, no pitch.")
    else:
        length = (f"Between {words.get('min')} and {words.get('max')} words, "
                  f"not counting the sign-off. Count them.")
    lines = [f"THIS EMAIL — {n} of {total}, {touch.get('kind')}: "
             f"{touch.get('role')}",
             touch.get("brief", ""), "", f"Length: {length}"]
    if touch.get("subject"):
        lines.append(f"Subject: do NOT write a subject line. This email "
                     f"replies in the thread \"{touch['subject']}\".")
    else:
        lines.append("Subject: write one, as the first line, prefixed "
                     "`Subject: `. Their first name must be the first or "
                     "second word. Short, plain, no marketing structure.")
    content = touch.get("content")
    if content:
        verb = ("they reposted it; someone else wrote it, so say they "
                "shared it" if content.get("kind") == "repost"
                else "they wrote it")
        lines.append(f"The one piece of their LinkedIn activity this email may "
                     f"use ({verb}): {content.get('text')!r}. Mention no "
                     f"other post.")
    else:
        lines.append("Mention none of their posts in this email.")
    themes = touch.get("content_themes")
    if themes:
        lines.append(f"Words that recur across what they share (counted, not "
                     f"interpreted): {', '.join(themes)}. You may say they "
                     f"share a lot about one of these; do not read meaning "
                     f"into it.")
    hyp = touch.get("hypothesis")
    if hyp:
        lines.append(f"The guess this email is built on (OUR guess from their "
                     f"role, not anything they said): {hyp.get('statement')}. "
                     f"You may present it as a pattern you come across often "
                     f"with people in their seat, then ASK "
                     f"{hyp.get('asks')}. Never state it as a fact about them.")
    proof = touch.get("proof_point")
    if proof:
        lines.append(f"Case study to cite, exactly as the brief states it. "
                     f"Say \"we\" built it, never \"I\", and add no result it "
                     f"does not list: {proof.get('brief')}")
    used = touch.get("already_used") or {}
    if any(used.values()):
        lines.append(f"Already used earlier in this thread, do not reuse: "
                     f"{json.dumps(used, ensure_ascii=False)}")
    if touch.get("previous_emails"):
        lines.append("Every earlier email in this thread is in "
                     "`prior_messages`. Read them. Move the conversation on; "
                     "do not repeat a sentence, an opener, or an "
                     "introduction from them.")
    return "\n".join(lines)


def render_prompt(inp: DrafterInput, retry_hint: str | None = None) -> str:
    """Compose the full prompt sent to claude -p: subagent body + JSON context.

    On retry, an optional hint is appended to nudge the next attempt toward
    fixing the specific failure (oversize, too-short, etc.)."""
    base = _load_subagent_prompt()
    payload = json.dumps(asdict(inp), indent=2, ensure_ascii=False)
    closing = "Draft now. Return only the message body."
    if inp.evidence:
        # Restated outside the JSON because it is the binding constraint, and
        # a rule buried in a payload field competes with everything else in
        # the payload for the model's attention.
        licensed = inp.evidence.get("pain_claim_licensed")
        close = inp.evidence.get("closing")
        if close:
            uncertainty = (
                "You may name your uncertainty ONCE, briefly."
                if close.get("names_uncertainty") else
                "Do NOT write a sentence saying you don't know whether this "
                "is relevant. The question carries it."
            )
            samples = "\n".join(f"  - {e}" for e in close.get("examples", []))
            closing = (
                f"HOW TO CLOSE — {close['name']}\n"
                f"Purpose: {close['purpose']} {uncertainty}\n"
                f"Sentences that hit this intent:\n{samples}\n"
                f"Do not copy any of them. They triangulate the target; write "
                f"your own sentence that lands in the same place.\n\n{closing}"
            )
        angle = inp.evidence.get("positioning")
        if angle:
            domain = angle.get("proof_domain")
            closing = (
                f"HOW TO INTRODUCE AGENTIC LABS — {angle['name']}\n"
                f"Angle: {angle['angle']}\n"
                f"Chosen because: {angle['fits_because']}.\n"
                + (f"Their world resembles ours in: {domain}. Name the "
                   f"matching proof point from the brief.\n" if domain and
                   angle["name"] == "proof_point" else "")
                + "Across 25 drafts, 23 called us \"a small AI-engineering "
                  "studio\" while the brief's actual work went unmentioned. "
                  "Twelve different sentences, one identical claim. Use THIS "
                  "angle instead, in your own words, and do not fall back on "
                  "the generic self-description.\n\n" + closing
            )
        shape = inp.evidence.get("shape")
        if shape:
            closing = (
                f"SHAPE FOR THIS EMAIL — {shape['name']}: {shape['outline']}\n\n"
                f"Follow it. Left alone you settle into one order for every "
                f"prospect, and across a hundred sends that order is the "
                f"tell.\n\n{closing}"
            )
        if licensed:
            standing = ("A signal supports a claim about their situation; "
                        "keep the claim tied to that signal and phrase it as "
                        "a read, not a diagnosis.")
        elif inp.touch:
            # The cadence's own version. "Introduce Agentic Labs plainly" is
            # wrong for every email after the first, and a guess-led email is
            # allowed to raise the pattern it was given, as long as it asks.
            standing = ("NOTHING licenses a claim about this person's "
                        "problems. If this email was given a guess, you may "
                        "describe it as a pattern you see with other people "
                        "in their seat and ask whether it is true for them; "
                        "never state, imply or hedge that it is true of "
                        "them.")
        else:
            standing = ("NOTHING licenses a claim about this person's "
                        "problems. Do not state, imply, or hedge one. "
                        "Reference what is verified, introduce Agentic Labs "
                        "plainly, ask whether it is relevant.")
        closing = (
            f"Evidence tier for this prospect: "
            f"{inp.evidence.get('tier')}. {standing}\n\n{closing}"
        )
    if inp.touch:
        closing = f"{_touch_block(inp.touch)}\n\n{closing}"
    if retry_hint:
        closing = f"{retry_hint}\n\n{closing}"
    return f"{base}\n\n# Context\n\n```json\n{payload}\n```\n\n{closing}"


# -------------------------------------------------------------- claude invoker

def _invoke_claude(prompt: str, timeout: int = 90) -> str:
    """Run `claude -p` and return stdout. Separated for test stubbing.

    stdin is explicitly closed via DEVNULL. Without this, in non-TTY contexts
    (cron, launchd) claude waits 3s for stdin then in some cases exits 1
    with empty stderr — the exact "claude -p exited 1" failure mode we kept
    hitting. Interactive shells dodge this because stdin is a TTY."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise DrafterError("`claude` binary not on PATH — is Claude Code installed?")
    proc = subprocess.run(
        [claude_bin, "-p", prompt, "--output-format", "text"],
        capture_output=True,
        text=True,
        # The model emits UTF-8. Without this, text=True decodes with the
        # locale codec — cp1252 on Windows — and the first live drafter run
        # produced "Vincent â€”" where an em-dash should be. That corruption
        # is in the draft body itself, not the terminal: it would be stored,
        # approved on a phone, and mailed to a real person. Names with
        # accents corrupt the same way.
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise DrafterError(
            f"claude -p exited {proc.returncode}\nstderr:\n{proc.stderr[:500]}"
        )
    return proc.stdout


def warmup_auth(timeout: int = 20) -> bool:
    """Single trivial `claude -p` call to serialize any pending OAuth refresh
    before the drafter is hit in rapid succession. Returns True if the call
    returned 0, False otherwise. Never raises — best-effort.

    Why this exists: when the daily cron fires multiple drafter calls in
    quick succession, an OAuth token mid-refresh causes some of them to fail
    with `claude -p exited 1` (empty stderr). This warmup forces the token
    refresh to complete BEFORE we make parallel drafter calls. Verified
    against incident 2026-05-20 where 4/4 dm1 drafts failed at 12:31 UTC,
    the same minute the credentials.json was rewritten. Cost: 1-3 seconds
    on a hot start, ~5-15 seconds when refresh actually fires."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        # No claude binary on PATH — surfaces in tests + dev environments
        # without Claude Code installed. Production cron always has it.
        return False
    try:
        proc = subprocess.run(
            [claude_bin, "-p", "ok", "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            return False
        return True
    except Exception:
        # Timeouts, OSError, anything — warmup is best-effort, never blocks
        # the real cron run. The downstream drafter calls will surface real
        # failures normally.
        return False


# -------------------------------------------------------------- output cleanup

# Models sometimes wrap output in code fences despite instructions. Strip them.
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```\s*$", re.DOTALL)


def _clean_output(raw: str) -> str:
    text = raw.strip()
    m = _CODE_FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    # Strip surrounding quotes if the whole body is wrapped in them.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text


# -------------------------------------------------------------- public API

@dataclass
class DraftAttempt:
    """What happened on one pass through the drafter.

    Carries the verdict and never the draft. A rejected body is assembled from
    a real person's scraped profile, and the trace it would land in is
    diagnostic output that gets pasted into tickets and chat. The category and
    reason say everything an operator needs — which gate fired and why — with
    nothing about the prospect in them.
    """

    number: int
    outcome: str                 # "accepted" | "rejected"
    category: str | None = None  # the gate that fired, machine-readable
    reason: str | None = None    # short human explanation, no draft content

    def __str__(self) -> str:
        if self.outcome == "accepted":
            return f"attempt {self.number}: accepted"
        return f"attempt {self.number}: rejected — {self.category}: {self.reason}"


def _record(sink, number: int, outcome: str,
            category: str | None = None, reason: str | None = None) -> None:
    """Append an attempt to the caller's list, if it asked for one."""
    if sink is None:
        return
    sink.append(DraftAttempt(number=number, outcome=outcome,
                             category=category, reason=reason))


def shape_gaps(body: str, evidence: dict, brief: str) -> list[str]:
    """Which of the chosen shape's parts the draft failed to deliver."""
    shape_name = (evidence.get("shape") or {}).get("name")
    if not shape_name:
        return []
    for shape in evidence_mod._SHAPES:
        if shape.name != shape_name:
            continue
        # The bundle is already flattened into `evidence` by this point, so a
        # light stand-in carries the one field the checks read.
        bundle = evidence_mod.EvidenceBundle(items=[
            evidence_mod.Evidence(kind=evidence_mod.EvidenceKind.OBSERVATION,
                                  statement="", source="",
                                  detail=o.get("detail", ""))
            for o in evidence.get("observations", [])])
        return evidence_mod.missing_shape_elements(body, shape, bundle, brief)
    return []


def _shared_positioning() -> str:
    """The shared Cortivo brief, which holds the facts about us.

    Read rather than imported so an edit to the brief takes effect on the next
    draft — the brief is the authority on what we may claim, and a stale copy
    would authorise yesterday's facts.
    """
    try:
        return campaigns_mod.brief_path_for("_agentic_labs").read_text(
            encoding="utf-8", errors="replace")
    except (OSError, AttributeError):
        # No brief means nothing is grounded, so every specific claim about us
        # is rejected. That is the right way to fail: silence beats invention.
        return ""


_SUBJECT_RE = re.compile(r"^\s*subject\s*:\s*(.+?)\s*\n+", re.IGNORECASE)


def parse_email(text: str) -> tuple[str | None, str]:
    """Split "Subject: ...\\n\\n<body>" into its two parts.

    The subject is generated by the drafter rather than assembled from a
    format string, because a fixed "<Company> - <benefit>" structure is a
    marketing subject line and reads like one on every prospect. A missing
    subject returns None rather than a fabricated one, so the caller decides
    what to do instead of silently sending a template.
    """
    match = _SUBJECT_RE.match(text)
    if not match:
        return None, text.strip()
    return match.group(1).strip(), text[match.end():].strip()


def _cadence_structure_problem(body: str, kind: str, touch: dict,
                               inp: DrafterInput):
    """(category, reason, retry hint) for a cadence email with the wrong
    shape, or None. Length in words, a question in every follow-up, the
    first-touch subject rule, and no links."""
    sign_off = (inp.sender or {}).get("sign_off")
    words = cadence_words(body, sign_off)
    band = touch.get("words") or {}
    low, high = band.get("min", 0), band.get("max", 10_000)
    if not low <= words <= high:
        want = (f"between {low} and {high}" if low else f"at most {high}")
        return ("word_band", f"{words} words, want {want}",
                f"Your previous attempt was {words} words; this email must be "
                f"{want} words, not counting the sign-off. "
                + ("Cut it to one or two short lines and a question."
                   if kind == "email_followup" else
                   "Rewrite it to that length; do not pad with filler."))
    if kind == "email_followup" and "?" not in email_text(body, sign_off):
        return ("followup_without_question", "no question asked",
                "A follow-up must ask a question. Keep it short and end on "
                "one genuine question they could answer in a line.")
    if not touch.get("subject"):
        first = (inp.prospect or {}).get("first_name")
        problem = subject_problem(parse_email(body)[0], first)
        if problem:
            return ("subject_rule", problem,
                    f"Start with `Subject: ` on the first line. {problem}: "
                    f"put {first} as the first or second word, keep it short.")
    if _LINK.search(email_text(body, sign_off)):
        return ("link", "the email contains a link",
                "No links. Cite our work by name and its published result; "
                "a cold email with a link reads as a campaign.")
    return None


def _cadence_content_problem(body: str, touch: dict, inp: DrafterInput,
                             evidence: dict | None, brief: str):
    """(category, reason, retry hint) for a cadence email that says something
    it may not, or None."""
    career = _contains_career_diagnosis(body)
    if career:
        return ("career_diagnosis", f"{career!r}",
                f"Your previous attempt said {career!r}. Their time in a role "
                f"may shape the question you ask; it is never said back to "
                f"them. No word about their career, no tenure figure, no "
                f"'stuck', 'next chapter' or 'hero'. Ask whether AI "
                f"implementation is a lever they are looking at, and stop.")
    stated = _states_hypothesis_as_fact(body, evidence)
    if stated:
        return ("hypothesis_as_fact", stated,
                f"Your previous attempt stated our guess as a fact about them "
                f"({stated}). It is a guess from their role. Say it as a "
                f"pattern you come across with people in their seat, then ask "
                f"whether it is true for them. Never 'your team is...' or "
                f"'you have...'.")
    personal = _claims_team_work_personally(body, brief)
    if personal:
        return ("case_study_claimed_personally", personal,
                f"Your previous attempt claimed a case study in the first "
                f"person ({personal}). It is the company's work: say 'we "
                f"built', never 'I built'.")
    previous = [t.get("body") or "" for t in touch.get("previous_emails") or []]
    repeated = _repeats_previous_email(body, previous,
                                       (inp.sender or {}).get("sign_off"))
    if repeated:
        return ("repeats_earlier_email", f"{repeated!r}",
                f"Your previous attempt repeated an earlier email in this "
                f"thread word for word ({repeated!r}). One person moving a "
                f"conversation on does not paste themselves. Say something "
                f"new, in new words.")
    return None


def draft(
    kind: str,
    prospect_id: int,
    recent_posts: Sequence[dict] | None = None,
    max_attempts: int = MAX_DRAFT_ATTEMPTS,
    evidence: dict | None = None,
    attempts_out: list | None = None,
    touch: dict | None = None,
) -> str:
    """Generate a draft, retrying on recoverable failures (oversize / empty /
    suspiciously short). Raises DrafterError when:
      - INSUFFICIENT_CONTEXT is returned (terminal — no retry, drafter is right)
      - All `max_attempts` runs failed quality checks
      - Build fails (missing prospect, invalid kind, etc.)
    """
    # `touch` only when there is one, so every non-cadence call is made
    # exactly as it was before the cadence existed.
    extra = {"touch": touch} if touch else {}
    inp = build_input(kind, prospect_id, recent_posts=recent_posts,
                      evidence=evidence, **extra)
    cap_max = KIND_MAX_CHARS[kind]
    cap_min = KIND_MIN_CHARS.get(kind, 50)

    # Whether this prospect's evidence licenses ANY claim about their problems.
    # Absent evidence the answer is no for email, which is the fail-closed
    # direction: an email that diagnoses a stranger with no basis is the
    # failure being fixed, and silently permitting it whenever the caller
    # forgot to pass evidence would reintroduce it. DM kinds keep their
    # existing behaviour unless evidence is supplied, so the live LinkedIn
    # flow is not changed underneath itself.
    if evidence is not None:
        pain_licensed = bool(evidence.get("pain_claim_licensed"))
        enforce_pain_gate = True
    else:
        pain_licensed = False
        enforce_pain_gate = kind.startswith("email")

    # At the weak tier the correct email is: the verified observation, a plain
    # introduction, an ask. Arguing that the prospect's category makes us
    # relevant is what the drafter reaches for instead of admitting it knows
    # one thing, and it is invention with a hedge on it.
    thin_evidence = (evidence or {}).get("tier") in ("weak", "none")

    # Claims about us are grounded in the brief, not in anything about the
    # prospect. Both briefs: the campaign's own, and the shared positioning
    # file that holds the team, clients and engagement facts.
    # The sender's own approved credentials ground claims about them too —
    # their past employers and projects are real proper nouns for THIS sender.
    grounding = evidence_mod.CortivoGrounding(
        (inp.campaign or {}).get("brief") or "", _shared_positioning(),
        (inp.sender or {}).get("credentials") or "")

    last_failure: str | None = None
    last_body_preview: str | None = None
    retry_hint: str | None = None

    for attempt in range(1, max_attempts + 1):
        prompt = render_prompt(inp, retry_hint=retry_hint)
        raw = _invoke_claude(prompt)
        body = _clean_output(raw)

        # INSUFFICIENT_CONTEXT is terminal — the drafter is telling us there
        # genuinely isn't enough signal. Retrying just wastes tokens.
        if body.strip() == INSUFFICIENT:
            _record(attempts_out, attempt, "rejected",
                    "insufficient_context",
                    "the drafter judged the evidence too thin")
            raise DrafterError("INSUFFICIENT_CONTEXT — not enough signal to draft")

        # Length applies to the message, not to the subject line the drafter
        # emits above it. Measuring the raw output let a 292-char body pass a
        # 300-char floor because "Subject: your SDET and AI security roles"
        # made up the difference — the gate and the validation stage were
        # measuring two different strings. Content gates below still see the
        # subject, because a subject can carry a spam tell or a stock phrase.
        measured = parse_email(body)[1] if kind.startswith("email") else body

        if not body:
            _record(attempts_out, attempt, "rejected", "empty_output",
                    "the model returned nothing")
            last_failure = f"empty output (attempt {attempt})"
            retry_hint = (
                "Your previous attempt returned an empty response. "
                "Please produce an actual message body this time."
            )
            continue

        if len(measured) > cap_max:
            _record(attempts_out, attempt, "rejected", "length_over",
                    f"{len(measured)} chars, cap {cap_max}")
            last_failure = f"oversize {len(measured)}/{cap_max} (attempt {attempt})"
            last_body_preview = body[:180]
            retry_hint = (
                f"Your previous attempt was {len(measured)} characters; the cap for "
                f"`{kind}` is {cap_max}. Be tighter. Cut the second sentence "
                f"if you have to. Keep only the most specific reference."
            )
            continue

        if len(measured) < cap_min:
            _record(attempts_out, attempt, "rejected", "length_under",
                    f"{len(measured)} chars, floor {cap_min}")
            last_failure = f"too short {len(measured)}/{cap_min} (attempt {attempt})"
            last_body_preview = body
            retry_hint = (
                f"Your previous attempt was only {len(measured)} characters, which "
                f"is below the {cap_min}-char minimum for a substantive "
                f"`{kind}`. Add a specific reference from the prospect's post "
                f"or profile and a real question. Do not return a single line."
            )
            continue

        if touch:
            problem = _cadence_structure_problem(body, kind, touch, inp)
            if problem:
                category, reason, hint = problem
                _record(attempts_out, attempt, "rejected", category, reason)
                last_failure = f"{category} {reason} (attempt {attempt})"
                last_body_preview = body
                retry_hint = hint
                continue

        # Spam-tell scan: even with the prompt rule, the model occasionally
        # produces banal openers like "I came across your profile". Reject +
        # retry with a specific call-out.
        spam = _contains_spam_tell(body)
        if spam:
            _record(attempts_out, attempt, "rejected", "spam_tell",
                    f"{spam!r}")
            last_failure = f"spam tell {spam!r} (attempt {attempt})"
            last_body_preview = body
            retry_hint = (
                f"Your previous attempt contained the spam-tell phrase "
                f"{spam!r}. This is in the hard-rules list. Rewrite without "
                f"any variant of: 'I came across', 'I noticed you', 'I saw "
                f"your profile', 'I'd love to chat/connect', 'hope you're "
                f"doing well'. Start with a specific reference instead."
            )
            continue

        # Recycled-template scan. These phrasings were mandated by the old
        # prompt and appeared verbatim in every email, which is what made
        # "personalised" output read as a mail merge.
        filler = _contains_filler(body)
        if filler:
            _record(attempts_out, attempt, "rejected", "template_filler",
                    f"{filler!r}")
            last_failure = f"template filler {filler!r} (attempt {attempt})"
            last_body_preview = body
            retry_hint = (
                f"Your previous attempt reused the stock phrase {filler!r}. "
                f"That phrasing appeared in every email this system has ever "
                f"produced, which is precisely why it reads as a template. "
                f"Say the same thing in words that only make sense for this "
                f"person, or cut the sentence entirely."
            )
            continue

        # Unsupported-diagnosis scan. A claim about the prospect's problems
        # requires a SIGNAL that they themselves published; a job change, a
        # job title and a headcount are not evidence of anything.
        if enforce_pain_gate and not pain_licensed:
            claim = _contains_unsupported_pain_claim(body)
            if claim:
                _record(attempts_out, attempt, "rejected", "unsupported_pain_claim",
                        f"{claim!r} with no signal licensing it")
                last_failure = f"unsupported pain claim {claim!r} (attempt {attempt})"
                last_body_preview = body
                retry_hint = (
                    f"Your previous attempt asserted a business problem "
                    f"({claim!r}) that nothing in the evidence supports. "
                    f"There is no signal that this person has a build, "
                    f"tooling or scaling problem — do not infer one from "
                    f"their job title, their employer, or the fact that they "
                    f"changed roles. Reference what is verified, introduce "
                    f"Agentic Labs plainly, and ask whether it is relevant. A "
                    f"short honest email beats a confident wrong one."
                )
                continue

        # Reasoned-relevance scan. Only at the thin tiers, where the drafter
        # has nothing to react to and argues from the prospect's category
        # instead.
        if thin_evidence and not pain_licensed:
            inferred = _contains_inferred_relevance(body)
            if inferred:
                _record(attempts_out, attempt, "rejected", "inferred_relevance",
                        f"{inferred!r} reasoned from their role")
                last_failure = f"inferred relevance {inferred!r} (attempt {attempt})"
                last_body_preview = body
                retry_hint = (
                    f"Your previous attempt argued that this person's "
                    f"situation is one where our work matters ({inferred!r}). "
                    f"You reasoned that from their job title, not from "
                    f"anything they said. At this evidence level the email is "
                    f"three things and no more: the one thing we verified, a "
                    f"plain sentence on what Agentic Labs does, and the ask. Do "
                    f"not argue for relevance — ask about it."
                )
                continue

        # Someone else's words presented as theirs. Only possible when the
        # evidence holds reposts, and then checked on every kind.
        borrowed = _misattributes_repost(body, evidence)
        if borrowed:
            _record(attempts_out, attempt, "rejected", "misattributed_repost",
                    borrowed)
            last_failure = f"misattributed repost {borrowed} (attempt {attempt})"
            last_body_preview = body
            retry_hint = (
                f"Your previous attempt presented a post they REPOSTED as "
                f"their own words ({borrowed}). Someone else wrote it. It "
                f"tells you what caught their interest, nothing more. You may "
                f"say they shared or reposted something and engage with its "
                f"topic; do not say they wrote, posted or said it, do not "
                f"quote it, and do not name its author."
            )
            continue

        if touch:
            problem = _cadence_content_problem(body, touch, inp, evidence,
                                               grounding.text)
            if problem:
                category, reason, hint = problem
                _record(attempts_out, attempt, "rejected", category, reason)
                last_failure = f"{category} {reason} (attempt {attempt})"
                last_body_preview = body
                retry_hint = hint
                continue

        # Seniority claims about US. Checked before the brief-vocabulary gate
        # because that gate structurally cannot see this one: a spelled-out
        # credential carries no digits and no proper noun, so there is nothing
        # for it to reject.
        boast = _contains_unsupported_authority(body, inp.sender)
        if boast:
            _record(attempts_out, attempt, "rejected",
                    "unsupported_authority_claim", f"{boast!r}")
            last_failure = f"unsupported authority claim {boast!r} (attempt {attempt})"
            last_body_preview = body
            personal = (inp.sender or {}).get("personal_seniority")
            retry_hint = (
                f"Your previous attempt claimed {boast!r}. No source states "
                f"any figure for the team's collective seniority, and no "
                f"engagement length or team-size equivalence is approved. "
                + (f"The sender's own confirmed credential is: {personal!r}. "
                   f"It is PERSONAL — state it in the first person ('I've "
                   f"spent...'), never as 'we' or 'our team'. "
                   if personal else
                   "This sender has no confirmed seniority claim; do not "
                   "state one. ")
                + "Otherwise lead with what is published and checkable: 15+ "
                  "developers, 10+ industries served, and a named case study "
                  "with its actual result."
            )
            continue

        # Claims about US. The brief is the only authority for our team,
        # clients, results, timelines and how we work.
        invented = evidence_mod.ungrounded_cortivo_claim(body, grounding)
        if invented:
            _record(attempts_out, attempt, "rejected", "ungrounded_cortivo_claim",
                    str(invented))
            last_failure = f"ungrounded Cortivo claim {invented!r} (attempt {attempt})"
            last_body_preview = body
            retry_hint = (
                f"Your previous attempt made a claim about Agentic Labs that the "
                f"brief does not support: {invented}. Do not describe our "
                f"week, our process, our clients or our results beyond what "
                f"the brief states, and do not mirror the prospect's own "
                f"vocabulary back as something we do. Everything about us "
                f"must be traceable to the brief."
            )
            continue

        # Scraped-detail scan, email only. Inside LinkedIn, having seen
        # someone's profile is the medium. A cold email that quotes their
        # start date announces that we pulled a record on them.
        if kind.startswith("email"):
            tell = _contains_surveillance_tell(body)
            if tell:
                _record(attempts_out, attempt, "rejected", "surveillance_tell",
                        f"{tell!r}")
                last_failure = f"surveillance tell {tell!r} (attempt {attempt})"
                last_body_preview = body
                retry_hint = (
                    f"Your previous attempt contained {tell!r} — a detail that "
                    f"only comes from scraping a profile, so it reads as "
                    f"surveillance rather than attention. Remove every date, "
                    f"month-year stamp and employee count. Describe the move "
                    f"in narrative terms instead: what they built at the "
                    f"previous company, and what they are doing now."
                )
                continue

        # ---- soft gates -------------------------------------------------
        # Style, not truth. These re-prompt while there is budget left, but
        # never destroy a draft: an email with two dashes in it is worse than
        # one with one, and far better than no email at all. A correctness
        # gate above would rather send nothing; this one would not.
        soft = _overuses_dashes(body)
        soft_category = "dash_overuse"
        if not soft and inp.evidence and inp.evidence.get("shape"):
            # Selecting a shape and checking it was followed are different
            # things, and only the first was happening.
            gaps = shape_gaps(measured, inp.evidence, grounding.text)
            if gaps:
                soft, soft_category = "; ".join(gaps), "shape_unfulfilled"
        if soft and attempt < max_attempts:
            _record(attempts_out, attempt, "rejected", soft_category, soft)
            # Name the gate that actually fired. This was hardcoded to "dash
            # overuse" above the shape branch, so a run killed by three
            # unfulfilled shapes reported a punctuation problem — and the
            # message on the terminal DrafterError is the one thing an
            # operator has to work from when nothing came back.
            last_failure = f"{soft_category} {soft} (attempt {attempt})"
            if soft_category == "shape_unfulfilled":
                retry_hint = (
                    f"Your previous attempt did not deliver what the shape "
                    f"asked for: missing {soft}. Follow the SHAPE section "
                    f"above -- it names the parts this email needs."
                )
                continue
            retry_hint = (
                f"Your previous attempt used {soft}. You are leaning on the "
                f"dash to join an observation to its explanation, which is "
                f"the punctuation habit that makes writing read as generated. "
                f"Use commas, or start a new sentence. At most one dash in "
                f"the whole email, and only where it genuinely reads better."
            )
            continue

        # All quality gates passed. A surviving soft issue is recorded on the
        # accepted attempt rather than hidden — the draft went out with it.
        _record(attempts_out, attempt, "accepted",
                soft_category if soft else None, soft)
        return body

    msg = f"all {max_attempts} drafter attempts failed; last={last_failure}"
    if last_body_preview:
        msg += f"\nlast body preview: {last_body_preview!r}"
    raise DrafterError(msg)
