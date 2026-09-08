"""What we actually know about a prospect, and what that licenses us to say.

The failure this module exists to prevent: a verified profile fact silently
becoming an unverified business-pain claim. Observed 2026-09-04, from a real
run against a real person --

    verified fact:  moved from Jawakara Islands Maldives to Millennium Hotels
    email said:     "Most teams at that transition point end up rebuilding
                     internal tooling and data pipelines by hand"

Nothing in the first line supports the second. Changing jobs is not evidence of
a tooling problem, and asserting one to a stranger is both wrong and obviously
machine-generated -- the two failures reinforce each other.

So facts are typed by what they license, not by where they came from:

    VERIFIED_FACT  Something the provider returned that we can state plainly.
                   "Group CFO at Tikehau Capital." Referenceable, never
                   evidence of a problem.

    OBSERVATION    Something the prospect chose to publish. Their own words,
                   quotable back to them. Still not evidence of a problem --
                   a post about coding agents does not mean they have a
                   bottleneck.

    SIGNAL         Evidence that a business problem plausibly exists, stated by
                   the prospect themselves. Hiring engineers. Announcing a
                   raise. Describing a rebuild. ONLY a signal licenses a pain
                   claim, and the claim must stay tied to the signal that
                   licensed it.

    HYPOTHESIS     A guess. Allowed only downstream of a SIGNAL, and only
                   phrased as a guess.

The conversions below are the ones that produced the bad output and are
therefore forbidden outright, not merely discouraged:

    new job          -/->  scaling problem
    founder title    -/->  tooling problem
    engineering role -/->  technical bottleneck
    company growth   -/->  internal tooling pain
    career move      -/->  operational pain

Detection is keyword-based on the prospect's own published text. No LLM: a
model asked "does this imply a pain point?" will say yes, which is exactly the
behaviour being removed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum


class EvidenceKind(str, Enum):
    VERIFIED_FACT = "VERIFIED_FACT"
    OBSERVATION = "OBSERVATION"
    SIGNAL = "SIGNAL"
    HYPOTHESIS = "HYPOTHESIS"


class Tier(str, Enum):
    """How much the evidence lets the email attempt.

    The email's *shape* follows from this, which is what stops every prospect
    receiving the same paragraphs with different nouns: a person we know one
    fact about and a person who just announced a raise are not owed the same
    email, and pretending otherwise is how the template leaks through.
    """

    STRONG = "strong"        # a SIGNAL exists: a cautious hypothesis is allowed
    MODERATE = "moderate"    # they published something: reference it, claim nothing
    WEAK = "weak"            # role and company only: introduce, claim nothing
    NONE = "none"            # not enough to write a cold email at all


@dataclass
class Evidence:
    kind: EvidenceKind
    statement: str
    source: str                      # where it came from, for the audit trail
    licenses_pain_claim: bool = False
    detail: str | None = None        # the prospect's own words, when we have them
    claim: str | None = None         # the ONLY claim this evidence permits
    name: str | None = None          # rule that produced it, for phrasing lookup

    def as_dict(self) -> dict:
        d = {"kind": self.kind.value, "statement": self.statement,
             "source": self.source}
        if self.detail:
            d["detail"] = self.detail
        if self.claim:
            d["permits_claim_about"] = self.claim
        return d


# --------------------------------------------------------------- signal rules

# Each entry: (name, pattern, what the prospect is telling us, what a claim
# built on it may say). The licensed claim is deliberately narrow -- it stays
# about the thing they said, and never widens into "so you must also have...".
_SIGNAL_RULES: tuple[tuple[str, re.Pattern, str, str], ...] = (
    (
        # First person, always. "open role" and "funding round" on their own
        # match a recruiter describing a CLIENT, and that is exactly what
        # happened: Vinay Goel's post said a placement was joining "following
        # their recent funding round" -- StudentCrowd's round, not his -- and
        # the bare `funding round` alternative licensed a claim that he was
        # under pressure to ship what he had raised. He had raised nothing.
        #
        # Anyone whose job is writing about other companies -- recruiters,
        # agencies, investors, consultants -- trips a signal that does not
        # require the author to be the subject.
        "hiring_engineers",
        re.compile(r"(?i)\b(?:we(?:'re| are) hiring|we are now hiring|"
                   r"join(?:ing)? our team|"
                   r"we(?:'re| are) looking for an? (?:exceptional |senior |"
                   r"experienced )?(?:engineer|developer|dev\b|candidate)|"
                   r"our (?:team|company) is hiring)"),
        "they are hiring",
        "they are adding build capacity right now",
    ),
    (
        "fundraise",
        re.compile(r"(?i)\b(?:we(?:'ve| have)? (?:just )?raised|"
                   r"(?:we|i)(?:'ve| have)? closed our (?:seed|series [a-d])|"
                   r"closed our (?:seed|series [a-d])|"
                   r"our (?:seed|series [a-d]|funding) round|"
                   r"thrilled to announce (?:our|we)[^.]{0,40}rais)"),
        "they announced a raise",
        "they are under pressure to ship what the raise was raised for",
    ),
    (
        "build_pain",
        re.compile(r"(?i)\b(?:technical debt|legacy (?:system|code|stack)|"
                   r"re-?writing|re-?building (?:our|the)|migration off|"
                   r"manual process|by hand|spreadsheets?\b.{0,30}\b(?:still|"
                   r"instead)|backlog is)"),
        "they described build or process pain in their own words",
        "the specific problem they themselves described",
    ),
    (
        "shipping_product",
        re.compile(r"(?i)\b(?:just (?:launched|shipped)|we (?:launched|shipped)|"
                   r"going live|in beta|beta launch|new product launch)\b"),
        "they shipped something recently",
        "the delivery pressure that follows a launch",
    ),
)


def normalise_quotes(text: str) -> str:
    """Fold typographic quotes to ASCII before matching.

    LinkedIn's composer and every phone keyboard produce U+2019, not an ASCII
    apostrophe, so "We’re Hiring" is what actually arrives. Every pattern
    written with a straight quote silently fails against it, and silently is
    the problem: on 2026-09-04 a prospect who had posted three hiring ads in
    two days was scored as having no signal at all, and got the cautious email
    written for someone we know nothing about.

    A missed signal costs a good email. In the spam gate the same gap lets
    "I'd love to connect" reach a real person.
    """
    return (text.replace("’", "'").replace("‘", "'")
                .replace("“", '"').replace("”", '"')
                .replace("–", "-").replace("—", "-"))


def detect_signals(posts) -> list[Evidence]:
    """Signals found in the prospect's own published text.

    Only their words. A signal is a claim we are prepared to make about their
    business, so it has to come from them, not from our reading of their
    resume.
    """
    found: list[Evidence] = []
    seen: set[str] = set()
    for post in posts or []:
        raw = (getattr(post, "text", None) or
               (post.get("text") if isinstance(post, dict) else "") or "")
        if not raw.strip():
            continue
        text = normalise_quotes(raw)
        for name, pattern, statement, licensed in _SIGNAL_RULES:
            if name in seen:
                continue
            match = pattern.search(text)
            if not match:
                continue
            seen.add(name)
            found.append(Evidence(
                kind=EvidenceKind.SIGNAL,
                statement=statement,
                source=f"their own post ({name})",
                licenses_pain_claim=True,
                detail=_excerpt(text, match.start()),
                claim=licensed,
                name=name,
            ))
    return found


def _excerpt(text: str, around: int, width: int = 160) -> str:
    """A readable window of the post, so the claim can be checked against it."""
    start = max(0, around - width // 3)
    end = min(len(text), start + width)
    snippet = " ".join(text[start:end].split())
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet += "..."
    return snippet


# ---------------------------------------------------------------- the bundle


@dataclass
class EvidenceBundle:
    """Everything we know, typed, plus an explicit record of what we do not.

    `unknowns` is not decoration. The drafter's failure mode is filling silence
    with plausible-sounding invention, and naming the silence is what stops it.
    """

    items: list[Evidence] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)

    def of(self, kind: EvidenceKind) -> list[Evidence]:
        return [e for e in self.items if e.kind == kind]

    @property
    def signals(self) -> list[Evidence]:
        return self.of(EvidenceKind.SIGNAL)

    @property
    def observations(self) -> list[Evidence]:
        return self.of(EvidenceKind.OBSERVATION)

    @property
    def facts(self) -> list[Evidence]:
        return self.of(EvidenceKind.VERIFIED_FACT)

    @property
    def pain_claim_licensed(self) -> bool:
        """Whether ANY claim about this prospect's problems may be made.

        False is the common case and must stay comfortable to be in. An email
        that introduces us honestly with no diagnosis is a worse pitch and a
        better message than one that guesses at a problem and gets it wrong.
        """
        return any(e.licenses_pain_claim for e in self.items)

    @property
    def tier(self) -> Tier:
        if self.signals:
            return Tier.STRONG
        if self.observations:
            return Tier.MODERATE
        if self.facts:
            return Tier.WEAK
        return Tier.NONE

    def as_dict(self, shape: "EmailShape | None" = None) -> dict:
        payload = {
            "tier": self.tier.value,
            "pain_claim_licensed": self.pain_claim_licensed,
            "verified_facts": [e.as_dict() for e in self.facts],
            "observations": [e.as_dict() for e in self.observations],
            "signals": [e.as_dict() for e in self.signals],
            "unknowns": self.unknowns,
            "licensed_claims": [e.claim for e in self.items
                                if e.licenses_pain_claim and e.claim],
        }
        if shape is not None:
            payload["shape"] = {"name": shape.name, "outline": shape.outline}
        return payload


def build_evidence(facts, posts=None, *, transition_is_direct: bool = False) -> EvidenceBundle:
    """Type everything we hold about one prospect.

    `transition_is_direct` comes from the caller because only it knows whether
    the two positions are genuinely consecutive at different employers. A
    transition enters as a VERIFIED_FACT either way -- it is referenceable, and
    it is never evidence of a problem.
    """
    items: list[Evidence] = []
    unknowns: list[str] = []

    positions = list(getattr(facts, "positions", None) or [])
    current = positions[0] if positions else None

    if current and current.company:
        role = current.title or "working"
        items.append(Evidence(
            kind=EvidenceKind.VERIFIED_FACT,
            statement=f"{role} at {current.company}",
            source="profile",
        ))
    elif getattr(facts, "company_name", None):
        items.append(Evidence(
            kind=EvidenceKind.VERIFIED_FACT,
            statement=f"at {facts.company_name}",
            source="profile",
        ))

    if transition_is_direct and len(positions) > 1:
        prev = positions[1]
        if prev.company and current and current.company:
            items.append(Evidence(
                kind=EvidenceKind.VERIFIED_FACT,
                statement=f"recently moved from {prev.company} to {current.company}",
                source="profile (consecutive dated positions)",
            ))
    elif len(positions) > 1 and positions[1].is_current:
        prev = positions[1]
        if prev.company:
            items.append(Evidence(
                kind=EvidenceKind.VERIFIED_FACT,
                statement=f"also currently {prev.title or 'involved'} at {prev.company}",
                source="profile (concurrent role)",
            ))

    quotable = []
    for post in posts or []:
        text = (getattr(post, "text", None) or
                (post.get("text") if isinstance(post, dict) else "") or "")
        if text.strip():
            quotable.append((post, text))

    for post, text in quotable:
        items.append(Evidence(
            kind=EvidenceKind.OBSERVATION,
            statement="they published this",
            source="their own post",
            detail=" ".join(text.split())[:400],
        ))

    items.extend(detect_signals([p for p, _ in quotable]))

    # Name the silence explicitly. Each of these is a gap the drafter has
    # previously filled with invention.
    if not quotable:
        unknowns.append("nothing they have written — no posts retrieved")
    if not any(e.licenses_pain_claim for e in items):
        unknowns.append("whether they have any build, tooling or scaling "
                        "problem — no evidence either way")
    if not (current and current.description):
        unknowns.append("what they actually do day to day in this role")
    unknowns.append("their budget, timeline, and whether they buy this kind of work")

    return EvidenceBundle(items=items, unknowns=unknowns)


# How each signal is said *to* the prospect. Kept next to the rules that
# produce it: `statement` and `claim` are third-person because they describe
# the prospect to the drafter, and splicing them into second-person prose
# produced "You wrote about are hiring" on a live run. Anything writing an
# actual sentence uses these.
SIGNAL_PHRASING: dict[str, tuple[str, str]] = {
    "hiring_engineers": (
        "you're hiring",
        "you're adding build capacity",
    ),
    "fundraise": (
        "you'd raised",
        "there's something specific you raised to go build",
    ),
    "build_pain": (
        "what you're rebuilding",
        "that's the kind of work you'd rather not do by hand",
    ),
    "shipping_product": (
        "what you'd just shipped",
        "there's a delivery push on right now",
    ),
}


def phrasing_for(evidence: Evidence) -> tuple[str, str]:
    """(what they said, the claim it licenses) in second person."""
    return SIGNAL_PHRASING.get(
        evidence.name or "",
        ("what you posted", "that's relevant to what you're building"))


# ------------------------------------------------- claims about ourselves
#
# The prospect gate stops us inventing THEIR problems. It says nothing about
# inventing OUR credentials, and the first live drafter run did exactly that:
#
#     "so a lot of our week is spent in exactly that parallel-agent workflow,
#      on client code rather than side projects"
#
# Nothing in the brief says that. It is a fabricated claim about how this
# agency works, mirrored back at a prospect who had just posted about parallel
# agents — flattering, plausible, and untrue. A prospect who replies to it is
# replying to something we made up, and the first call has to walk it back.
#
# So Cortivo claims get their own category, grounded in the approved brief
# rather than in the evidence about the prospect.


class CortivoGrounding:
    """The approved facts about us, and a check against them.

    The brief is the only authority. Anything specific about our team, our
    clients, our results, our timelines or our working practices has to trace
    back to it.

    This is deliberately not a general-purpose fact checker, and it should not
    be described as one. It catches two shapes that cover the realistic
    failures: an invented specific (a name, a number, a metric we never
    claimed), and an assertion about how we work built from vocabulary the
    brief never uses. A fluent paraphrase of a false claim, using only
    brief-approved words, would pass — the human approval step remains the
    backstop.
    """

    def __init__(self, *briefs: str) -> None:
        joined = " ".join(b or "" for b in briefs)
        self.text = joined
        low = joined.lower()
        self.vocabulary = set(re.findall(r"[a-z0-9][a-z0-9'+.-]*", low))
        # Numbers written any way the brief writes them.
        self.numbers = set(re.findall(r"\d+", low))


# Sentences that assert how we spend our time or who we work with. The brief
# describes what we build and for whom; it does not describe our week. These
# shapes are where invention showed up.
_PRACTICE_ASSERTION = re.compile(
    r"(?i)\b(?:"
    r"(?:a lot|most|much|half|the bulk) of our|"
    r"our (?:week|days?|time|process|workflow) (?:is|are|gets?)|"
    r"we (?:spend|typically|usually|always|often|routinely|tend to|mostly)|"
    r"we work (?:with|on|in)|"
    r"our (?:clients?|customers?|portfolio)"
    r")\b")

# Words any email may use about us without the brief listing them. Without
# this the check fires on ordinary English rather than on invented facts.
_GENERIC = {
    "cortivo", "we", "our", "us", "i", "im", "a", "an", "the", "and", "or",
    "but", "so", "that", "this", "these", "those", "it", "its", "is", "are",
    "was", "were", "be", "been", "am", "do", "does", "did", "have", "has",
    "had", "will", "would", "can", "could", "should", "may", "might", "must",
    "of", "in", "on", "at", "to", "for", "with", "from", "by", "as", "than",
    "then", "there", "here", "what", "which", "who", "how", "when", "where",
    "why", "not", "no", "yes", "up", "out", "over", "into", "about", "rather",
    "instead", "usually", "often", "typically", "mostly", "very", "much",
    "more", "less", "most", "lot", "lots", "some", "any", "all", "one", "two",
    "small", "senior", "engineer", "engineers", "engineering", "studio",
    "build", "builds", "building", "built", "work", "works", "working",
    "team", "teams", "founder", "founders", "software", "product", "products",
    "week", "weeks", "day", "days", "time", "client", "clients", "code",
    "run", "runs", "pair", "pairs", "tooling", "ai", "you", "your", "they",
    "their", "them", "he", "she", "if", "just", "own", "get", "gets", "make",
    "makes", "made", "need", "needs", "want", "wants", "put", "take", "takes",
    "help", "helps", "kind", "sort", "thing", "things", "way", "ways",
    # Ordinary verbs and adverbs. Leaving these in makes the check fire on
    # "spent" and report that as the invented fact, which misdirects the retry.
    "spend", "spent", "spends", "exactly", "side", "rather", "really",
    "actually", "still", "even", "also", "both", "each", "every", "same",
    "other", "another", "across", "around", "between", "before", "after",
    "since", "while", "during", "through", "without", "within", "onto",
}


def ungrounded_cortivo_claim(body: str,
                             grounding: CortivoGrounding) -> str | None:
    """Return the unsupported claim about Cortivo, or None if clean."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", body):
        clean = sentence.strip()
        if not clean or not re.search(r"(?i)\b(?:we|our|cortivo|i run|i'm at)\b",
                                      clean):
            continue

        # An invented specific: a proper noun or figure we never claimed.
        for token in re.findall(r"\b[A-Z][A-Za-z0-9+.-]{2,}\b", clean[1:]):
            if token.lower() not in grounding.vocabulary:
                return f"{token} (not in the brief)"
        for number in re.findall(r"\b\d+\b", clean):
            if number not in grounding.numbers:
                return f"the figure {number} (not in the brief)"

        # An assertion about how we work, built from words the brief never uses.
        if _PRACTICE_ASSERTION.search(clean):
            unknown = [w for w in re.findall(r"[a-z][a-z'-]{3,}", clean.lower())
                       if w not in _GENERIC and w not in grounding.vocabulary]
            if unknown:
                # Report the longest first: the distinctive term is the one the
                # drafter invented, and naming "spent" instead of
                # "parallel-agent" makes for a retry hint that misdirects.
                #
                # The offending terms, never the sentence. This string reaches
                # the run trace, and a trace is diagnostic output that gets
                # pasted into tickets and chat -- quoting a rejected draft back
                # into it would put a prospect's details somewhere nobody
                # intended. The terms alone say what went wrong.
                unknown.sort(key=len, reverse=True)
                named = ", ".join(repr(w) for w in unknown[:3])
                return f"a claim about how we work, using {named} — not in the brief"
    return None


# --------------------------------------------------------------- the shape
#
# Across five real drafts every single email came out in the same order:
#
#     observation -> interpretation -> Cortivo -> "I don't know if this is
#     relevant" -> question
#
# Each one read well alone. All five together read as one author, and over a
# hundred sends that order is the fingerprint -- the honest-uncertainty line
# had quietly become the new template component, which is the same failure the
# old filler was removed for, wearing a more likeable costume.
#
# So shape is chosen per prospect rather than left to the model, which will
# otherwise settle into whatever order it likes best. Two rules:
#
#   * the evidence still decides what may be SAID; shape only decides the
#     order it is said in, so this can never license a claim
#   * selection is deterministic on the prospect, so re-running the same
#     person is reproducible while a list of them spreads across the shapes


@dataclass(frozen=True)
class EmailShape:
    name: str
    outline: str
    needs_licence: bool = False   # only where a signal permits a hypothesis
    needs_two_observations: bool = False


_SHAPES: tuple[EmailShape, ...] = (
    EmailShape(
        "observation_question",
        "The observation, then the ask. Introduce Cortivo in a half-sentence "
        "at most. Do NOT explain why the observation makes them relevant to "
        "us -- leave the connection unmade and let the question carry it. "
        "This should be the shortest email you write.",
    ),
    EmailShape(
        "observation_bridge_question",
        "The observation, one plain sentence on what Cortivo does, the ask. "
        "No interpretation of their situation in between.",
    ),
    EmailShape(
        "observation_experience_question",
        "The observation, then one concrete thing we have actually built "
        "that connects to it (from the brief, never invented), then the ask. "
        "Let the example do the work instead of an explanation.",
    ),
    EmailShape(
        "observation_hypothesis_question",
        "The observation, ONE sentence of hypothesis tied to the signal, "
        "then the ask. One sentence -- not a paragraph of reasoning.",
        needs_licence=True,
    ),
    EmailShape(
        "two_observations_question",
        "Two specific things you noticed, a very short introduction, the "
        "ask. The specificity carries this one; keep everything else minimal.",
        needs_two_observations=True,
    ),
)


def available_shapes(bundle: "EvidenceBundle") -> list[EmailShape]:
    """The shapes this prospect's evidence can actually support."""
    out = []
    for shape in _SHAPES:
        if shape.needs_licence and not bundle.pain_claim_licensed:
            continue
        if shape.needs_two_observations and len(bundle.observations) < 2:
            continue
        out.append(shape)
    return out


def choose_shape(bundle: "EvidenceBundle", key: str | None) -> EmailShape:
    """Pick one shape, stably, from what the evidence supports.

    Hashed on the prospect rather than randomised so a re-run of the same
    person produces the same shape -- otherwise the drafter looks broken every
    time you check your work, and A/B comparison is impossible.
    """
    options = available_shapes(bundle)
    if not options:
        return _SHAPES[0]
    digest = hashlib.sha256((key or "").encode("utf-8")).digest()
    return options[digest[0] % len(options)]


# ------------------------------------------------------------ subject + ask

def subject_for(facts, bundle: EvidenceBundle) -> str:
    """A subject that matches the evidence, and varies with it.

    "<Company> - shipping without hiring a team" was the old output for every
    prospect: one structure, and a claim about a problem we had not verified.
    A subject line is not the place to assert something the body is forbidden
    from asserting.
    """
    first = _first_name(getattr(facts, "full_name", None))

    for signal in bundle.signals:
        if "hiring" in signal.source:
            return f"Your engineering hire" if not first else f"Your engineering hire, {first}"
        if "fundraise" in signal.source:
            return "Congrats on the raise"
        if "shipping_product" in signal.source:
            return "Your launch"

    for fact in bundle.facts:
        if fact.statement.startswith("recently moved from"):
            company = fact.statement.rsplit(" to ", 1)[-1]
            return f"Your move to {company}"

    if first:
        return f"Quick question, {first}"
    return "Quick question"


# CTAs ordered by how much they presume. The strongest ask is only reachable
# when a signal exists, because "walk through what we'd build" assumes a
# problem worth building for -- an assumption we usually have not earned.
_ASKS = {
    Tier.STRONG: "Worth comparing notes on how you're handling it?",
    Tier.MODERATE: "Open to a quick conversation?",
    Tier.WEAK: "Would this be relevant on your side?",
}


def ask_for(bundle: EvidenceBundle) -> str:
    return _ASKS.get(bundle.tier, _ASKS[Tier.WEAK])


def _first_name(full_name: str | None) -> str | None:
    if not full_name:
        return None
    parts = full_name.strip().split()
    return parts[0] if parts else None
