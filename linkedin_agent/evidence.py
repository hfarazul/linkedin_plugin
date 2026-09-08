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

    def as_dict(self, shape: "EmailShape | None" = None,
                closing: "Closing | None" = None,
                positioning: "Positioning | None" = None,
                proof_domain: str | None = None) -> dict:
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
        if closing is not None:
            payload["closing"] = {
                "name": closing.name,
                "purpose": closing.purpose,
                "examples": list(closing.examples),
                "names_uncertainty": closing.names_uncertainty,
            }
        if positioning is not None:
            payload["positioning"] = {
                "name": positioning.name,
                "angle": positioning.angle,
                "fits_because": positioning.when,
                "proof_domain": proof_domain,
            }
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


_PROOF_POINT_RE = re.compile(r"\*\*([A-Z][^*]{2,40})\*\*")


def proof_points(brief: str) -> list[str]:
    """The named things we have actually built, from the brief's own markup.

    The brief bolds them -- **Experial**, **Microforge**, **Mastercard** -- so
    the list comes from the document rather than a copy that drifts from it.
    """
    names = []
    for raw in _PROOF_POINT_RE.findall(brief or ""):
        name = raw.split("(")[0].strip().rstrip(".,")
        if name and name.lower() not in {"yes", "no"}:
            names.append(name)
    return names


def missing_shape_elements(body: str, shape: EmailShape,
                           bundle: "EvidenceBundle",
                           brief: str = "") -> list[str]:
    """Slots the chosen shape promised that the draft did not deliver.

    Selecting a shape and checking it was followed are different things, and
    only the first was happening: Soumya's draft was assigned
    `observation_experience_question` and returned positioning where the
    concrete example should have been. The shape was honoured in order and
    ignored in substance.

    Deliberately partial. Two slots are mechanically checkable and are
    checked; the rest -- "is this a real observation", "is this one sentence
    of hypothesis" -- are judgement, and a regex pretending to measure them
    would fail the wrong drafts. Those stay the prompt's job.
    """
    missing: list[str] = []
    low = body.lower()

    if "?" not in body:
        missing.append("a question to answer")

    if shape.name == "observation_experience_question":
        named = proof_points(brief)
        if named and not any(p.lower() in low for p in named):
            missing.append("a concrete thing we have built, by name")

    if shape.name == "two_observations_question":
        # Each observation is matched on its own distinctive words rather than
        # on any single keyword, so a passing draft has to have engaged with
        # two of them and not merely mentioned one twice.
        referenced = 0
        for obs in bundle.observations:
            words = {w for w in re.findall(r"[a-z]{5,}", (obs.detail or "").lower())}
            if words and len(words & set(re.findall(r"[a-z]{5,}", low))) >= 2:
                referenced += 1
        if referenced < 2:
            missing.append(f"a second distinct observation (found {referenced})")

    return missing


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


# ------------------------------------------------------------ positioning
#
# Across 25 drafts, 23 described Cortivo as "a small AI-engineering studio" --
# 92% -- while only 12 shared an introduction *sentence*. Twelve wordings, one
# claim. That is the fingerprint that survives paraphrase, and rotating the
# sentence would not have touched it.
#
# The brief holds Experial with Coca-Cola and Bosch, Microforge with a16z, a
# 6-10 week engagement, the 3-4 person-team equivalence. "ships in N weeks"
# appeared once in twenty-five. The material was there and went unused.
#
# The fix is NOT seven fixed descriptions, which would recreate the problem one
# level up. Each angle declares when it actually fits, and only angles that fit
# this prospect are eligible; the hash then breaks the tie. So variation comes
# from relevance first and rotation second -- otherwise every email names a
# different Cortivo fact regardless of whether it belongs, which is its own
# tell and a worse one, because it is also irrelevant.


@dataclass(frozen=True)
class Positioning:
    name: str
    angle: str
    when: str


_POSITIONING: tuple[Positioning, ...] = (
    Positioning(
        "extra_capacity",
        "We take on senior build work as extra capacity, alongside whoever "
        "is already there.",
        "they are hiring, or said the team is stretched",
    ),
    Positioning(
        "senior_plus_tooling",
        "We pair one senior engineer with AI tooling, which is how a small "
        "team gets through more than its headcount suggests.",
        "they are technical, or wrote about how software gets built",
    ),
    Positioning(
        "fast_focused_build",
        "We take one defined thing and build it, rather than staffing a "
        "long engagement.",
        "they just shipped or launched something",
    ),
    Positioning(
        "small_team_equivalent",
        "One engineer of ours plus AI tooling is roughly what a founder "
        "would otherwise hire three or four people to do.",
        "they are a founder or run the company",
    ),
    Positioning(
        "engagement_shape",
        "A typical engagement runs six to ten weeks, kickoff to live users.",
        "there is a concrete project in view -- they raised, or named "
        "something they are about to build",
    ),
    Positioning(
        "proof_point",
        "Name the closest thing we have actually built, from the brief.",
        "a proof point in the brief genuinely matches their world",
    ),
    Positioning(
        "custom_software",
        "We build the internal software that no vendor sells exactly, for "
        "teams who would otherwise do it by hand.",
        "they described a build, tooling or manual-process problem",
    ),
    Positioning(
        "plain",
        "Say what Cortivo is in the plainest terms and stop. A stranger who "
        "has told us nothing is owed a short honest sentence, not a pitch "
        "angle chosen for them.",
        "nothing above fits -- this is the honest default, not a failure",
    ),
)

# Which prospect worlds each proof point can honestly speak to. Keyword-matched
# against their headline, company and posts, so a case study is only offered
# where it actually lands.
_PROOF_DOMAINS = {
    "fintech, payments, wealth, investment":
        re.compile(r"(?i)\b(fintech|payments?|banking|wealth|invest(?:ment|or)|"
                   r"capital|fund|asset manage|private (?:debt|equity)|cfo)\b"),
    "enterprise, brand, consumer goods":
        re.compile(r"(?i)\b(enterprise|brand|fmcg|consumer goods|retail|"
                   r"marketing|survey|research)\b"),
    "venture capital and startup scouting":
        re.compile(r"(?i)\b(venture|vc\b|portfolio|accelerator|angel)\b"),
    "speed of delivery, campaign tooling":
        re.compile(r"(?i)\b(campaign|launch|go.to.market|speed|time.to.market)\b"),
}


def _text_about(facts, bundle: "EvidenceBundle") -> str:
    parts = [getattr(facts, "headline", "") or "",
             getattr(facts, "company_name", "") or ""]
    for pos in (getattr(facts, "positions", None) or [])[:2]:
        parts += [pos.title or "", pos.company or ""]
    parts += [e.detail or "" for e in bundle.observations]
    return " ".join(parts)


def _fits(angle: Positioning, facts, bundle: "EvidenceBundle") -> bool:
    signals = {s.name for s in bundle.signals}
    text = _text_about(facts, bundle)
    if angle.name == "extra_capacity":
        return "hiring_engineers" in signals
    if angle.name == "fast_focused_build":
        return "shipping_product" in signals
    if angle.name == "custom_software":
        return "build_pain" in signals
    if angle.name == "engagement_shape":
        return "fundraise" in signals
    if angle.name == "senior_plus_tooling":
        # Stems, not whole words: "Director of Engineering" is one of the
        # commonest titles there is, and \bengineer\b does not match it.
        return bool(re.search(r"(?i)\b(engineer(?:ing|s)?|developer|technical|"
                              r"cto|software|platform|data|architect|devops|"
                              r"product|build)\b", text))
    if angle.name == "small_team_equivalent":
        return bool(re.search(r"(?i)\b(founder|co-?founder|ceo|owner|"
                              r"managing director)\b", text))
    if angle.name == "proof_point":
        return any(p.search(text) for p in _PROOF_DOMAINS.values())
    return False        # "plain" is the fallback, never a positive match


def matching_proof_domain(facts, bundle: "EvidenceBundle") -> str | None:
    """Which of our worlds theirs resembles, if any."""
    text = _text_about(facts, bundle)
    for label, pattern in _PROOF_DOMAINS.items():
        if pattern.search(text):
            return label
    return None


def choose_positioning(facts, bundle: "EvidenceBundle",
                       key: str | None) -> Positioning:
    """The most relevant truthful angle, with the hash only breaking ties.

    An angle that does not fit is never eligible. Nothing here licenses a
    claim about the prospect -- it selects which true thing about US is worth
    saying to them.
    """
    eligible = [a for a in _POSITIONING if _fits(a, facts, bundle)]
    if not eligible:
        return _POSITIONING[-1]         # plain
    digest = hashlib.sha256(f"pos:{key or ''}".encode("utf-8")).digest()
    return eligible[digest[0] % len(eligible)]


# -------------------------------------------------------------- the closing
#
# "I've no idea whether that's relevant — would it be?" turned up in two of
# three drafts after the shape fix. The shape rotation broke the paragraph
# order and the closing simply became the next fingerprint: honesty is right,
# but honesty arriving in the same words every time is a template with good
# manners.
#
# The fix is not to ban the sentence. It is to vary the register, and to
# sometimes not name the uncertainty at all -- a question that assumes nothing
# already carries it. "Is outside engineering support something you'd consider
# while those roles are open?" is more natural than announcing that we do not
# know, and says the same thing.
#
# These are registers, not strings to paste. The example shows the tone; the
# drafter writes its own words in it, or we have swapped one template for seven.


@dataclass(frozen=True)
class Closing:
    """A conversational exit, described by intent rather than by wording.

    The first version carried one short example and an instruction not to
    reuse it. Across 25 drafts the `compare_notes` register reproduced its
    example verbatim 4 times out of 4: a single short phrase is not a
    register, it is a string to memorise, and telling a model not to copy the
    only example it has been given does not work.

    So each register now states its purpose and offers several examples. The
    target is the intent; the examples triangulate it rather than define it.
    """

    name: str
    purpose: str
    examples: tuple[str, ...]
    names_uncertainty: bool


_CLOSINGS: tuple[Closing, ...] = (
    Closing("direct_offer",
            "Ask about the specific thing you observed, assuming nothing. The "
            "question does the hedging, so do not add a sentence saying you "
            "don't know whether it is relevant.",
            ("Is outside engineering support something you'd consider while "
             "those roles are open?",
             "Would extra build capacity be useful while that's running?",
             "Is that something you'd ever bring in help for?"),
            False),
    Closing("usefulness",
            "Ask plainly whether it would be useful. Nothing before it.",
            ("Would this be useful on your side?",
             "Any use to you?",
             "Is that useful, or not really?"),
            False),
    Closing("overlap",
            "Ask whether it touches anything they are actually working on. "
            "Name the company or the work where it reads naturally.",
            ("Does that overlap with anything you're working on?",
             "Does any of that land near what you're doing at <company>?",
             "Anything there close to what's on your plate?"),
            False),
    Closing("worth_it",
            "Offer the conversation and the refusal in the same breath, so "
            "saying no costs them nothing.",
            ("Worth a conversation, or not really?",
             "Worth twenty minutes, or is this off the mark?",
             "Happy to talk, equally happy to be told it's not relevant."),
            False),
    Closing("easy_out",
            "Give them an explicit way to ignore it. Warm, not apologetic, "
            "and not self-deprecating.",
            ("If none of that is on your plate, no worries at all.",
             "If this isn't your area, ignore me entirely.",
             "If the timing's wrong, no need to reply."),
            True),
    Closing("compare_notes",
            "Invite a low-pressure exchange without implying a sales call. "
            "Peer to peer, not vendor to buyer.",
            ("Curious how you're approaching that.",
             "Happy to compare notes if useful.",
             "Might be worth comparing how you're handling it.",
             "Would be interesting to hear how you've set it up."),
            True),
    Closing("curious",
            "Name your own uncertainty once, briefly, then stop. One short "
            "sentence, not a paragraph of throat-clearing.",
            ("Curious whether this is relevant for you.",
             "Not sure if that's your world at all.",
             "May be well off the mark."),
            True),
)


def choose_closing(key: str | None) -> Closing:
    """Pick the closing register for this prospect.

    Salted differently from the shape so the two do not move together: if
    shape and closing were drawn from the same byte, a reader would see five
    fixed combinations rather than a spread.
    """
    digest = hashlib.sha256(f"closing:{key or ''}".encode("utf-8")).digest()
    return _CLOSINGS[digest[0] % len(_CLOSINGS)]


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
