#!/usr/bin/env python
"""Measure what a batch of drafts has in common.

Reading twenty emails one at a time tells you whether each is good. It does
not tell you the thing that actually matters at volume: whether a recipient
who saw two of them would notice they came from the same machine. That is a
property *between* emails, and eyeballing cannot see it — every fingerprint
this project has removed was invisible in a single draft and obvious in five.

    python scripts/fingerprint_report.py <dir-of-run-logs>

Reads the FINAL MESSAGE blocks out of smoke_e2e run output and reports what
repeats. Nothing here judges an individual email.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from linkedin_agent.trace import say  # noqa: E402

_FINAL = re.compile(r"^FINAL MESSAGE\s*$(.*?)^NOT SENT", re.M | re.S)
_SUBJECT = re.compile(r"^Subject:\s*(.+?)\s*$", re.M)
_FIELD = re.compile(r"^\s+(\w+)=(.*?)\s*$", re.M)

# Sign-off lines carry no signal about sameness; they are supposed to repeat.
_SIGNOFF = re.compile(r"(?im)^\s*(best|regards|thanks|haque|cortivo)[,.]?\s*$")


def _emails(directory: Path) -> list[dict]:
    out = []
    for path in sorted(directory.glob("*.txt")):
        text = path.read_text(encoding="utf-8", errors="replace")
        match = _FINAL.search(text)
        if not match:
            continue
        block = match.group(1)
        subject = _SUBJECT.search(block)
        body_start = block.find("Body:")
        body = block[body_start + 5:] if body_start >= 0 else block
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not _SIGNOFF.match(ln)]
        fields = dict(_FIELD.findall(text))
        out.append({
            "file": path.name,
            "subject": subject.group(1) if subject else "",
            "lines": lines,
            "body": " ".join(lines),
            "shape": fields.get("email_shape", "?"),
            "closing": fields.get("closing_register", "?"),
            "tier": fields.get("evidence_tier", "?"),
            "dashes": fields.get("connector_dashes", "?"),
            "angle": fields.get("positioning_angle", "?"),
        })
    return out


# The claim, not the phrasing. "a small AI-engineering studio" and "a small
# engineering studio we run" are two wordings of one idea, and a reader who
# saw both would notice the idea, not the difference.
_CORTIVO_CLAIMS = (
    ("small studio", re.compile(r"(?i)small\s+(?:ai[- ])?(?:engineering\s+)?studio")),
    ("senior engineer + AI tooling",
     re.compile(r"(?i)senior engineer[^.]{0,40}ai tooling|pair(?:ing)? a senior")),
    ("custom software", re.compile(r"(?i)custom software")),
    ("without hiring a team",
     re.compile(r"(?i)without (?:hiring|standing up)|rather not hire|"
                r"instead of hiring|not have to hire")),
    ("we build software for X", re.compile(r"(?i)we build software")),
    ("outside engineering support",
     re.compile(r"(?i)outside (?:engineering|engineers|help|capacity)")),
    ("equivalent of an N-person team", re.compile(r"(?i)equivalent of a")),
    ("ships in N weeks", re.compile(r"(?i)\d+\s*-\s*\d+\s*weeks")),
    ("contract / extra capacity",
     re.compile(r"(?i)contract capacity|extra capacity|build capacity")),
)

# How the email gets from silence to the observation. A shared opening move is
# as much a tell as a shared closing one.
_MARKERS = (
    ("Your post about…", re.compile(r"(?i)\byour post\b")),
    ("You wrote…", re.compile(r"(?i)\byou wrote\b")),
    ("Saw you / I saw…", re.compile(r"(?i)\b(?:saw|i saw) you\b")),
    ("I noticed…", re.compile(r"(?i)\bi noticed\b")),
    ("The part about…", re.compile(r"(?i)\bthe part (?:about|that|where)\b")),
    ("caught my eye", re.compile(r"(?i)caught my eye")),
    ("stood out / stuck with me",
     re.compile(r"(?i)stood out|stuck with me|stayed with me")),
    ("made me laugh/smile", re.compile(r"(?i)made me (?:laugh|smile)")),
    ("I keep thinking about", re.compile(r"(?i)keep thinking about")),
    ("that's why I'm writing",
     re.compile(r"(?i)(?:that's )?why i'm writing|the reason i'm writing")),
    ("no list / not a list",
     re.compile(r"(?i)\bno list\b|not (?:going to )?a list|pulled you off")),
)


def _cta_category(sentence: str) -> str:
    """What the ask does, rather than how it is worded."""
    s = sentence.lower()
    if re.search(r"no worries|no hard feelings|won't (?:chase|follow)", s):
        return "explicit opt-out"
    if re.search(r"compare notes", s):
        return "offer to compare notes"
    if re.search(r"overlap|anything you're working on", s):
        return "asks about overlap"
    if re.search(r"useful|worth", s):
        return "asks if useful/worth it"
    if re.search(r"relevant", s):
        return "asks if relevant"
    if "?" in sentence:
        return "other question"
    return "no question"


def _sentences(body: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]


def _ngrams(text: str, n: int) -> list[str]:
    words = re.findall(r"[a-z']+", text.lower())
    return [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]


def _bar(count: int, total: int, width: int = 24) -> str:
    filled = round(width * count / total) if total else 0
    return "#" * filled + "." * (width - filled)


def main() -> int:
    if len(sys.argv) < 2:
        say(__doc__)
        return 2
    directory = Path(sys.argv[1])
    emails = _emails(directory)
    if not emails:
        say(f"no FINAL MESSAGE blocks found in {directory}")
        return 1

    n = len(emails)
    say(f"\n{'=' * 66}\nFINGERPRINT REPORT — {n} drafts from {directory.name}\n{'=' * 66}")

    for label, key in (("shape", "shape"), ("closing register", "closing"),
                       ("positioning angle", "angle"),
                       ("evidence tier", "tier")):
        say(f"\n{label.upper()}")
        for value, count in Counter(e[key] for e in emails).most_common():
            say(f"  {value:34} {_bar(count, n)} {count}/{n}")

    # The closing line is where the last two fingerprints lived.
    say("\nCLOSING LINE  (the last sentence before the sign-off)")
    closers = Counter(_sentences(e["body"])[-1].lower() if _sentences(e["body"])
                      else "" for e in emails)
    for value, count in closers.most_common(8):
        flag = "  <-- REPEATED" if count > 1 else ""
        say(f"  {count}x  {value[:60]}{flag}")
    say(f"  distinct closings: {len(closers)}/{n}")

    say("\nUNCERTAINTY PHRASING")
    hedges = sum(1 for e in emails
                 if re.search(r"(?i)no idea whether|don't know whether|"
                              r"do not know whether", e["body"]))
    say(f"  emails naming their own uncertainty: {hedges}/{n}")

    say("\nCORTIVO INTRODUCTION")
    intros = Counter()
    for e in emails:
        for s in _sentences(e["body"]):
            if re.search(r"(?i)\bcortivo\b", s):
                intros[re.sub(r"\s+", " ", s.lower())[:60]] += 1
                break
    for value, count in intros.most_common(6):
        flag = "  <-- REPEATED" if count > 1 else ""
        say(f"  {count}x  {value}{flag}")
    say(f"  distinct introductions: {len(intros)}/{n}")

    # Wording and claim are different fingerprints, and the second is the one
    # that survives paraphrase. Eight syntactically distinct introductions
    # still read as one author if six of them describe us the same way.
    say("\nCORTIVO CLAIMS  (the concept, regardless of wording)")
    for label, pattern in _CORTIVO_CLAIMS:
        hits = sum(1 for e in emails if pattern.search(e["body"]))
        if hits:
            say(f'  "{label}"'.ljust(38) + f"{_bar(hits, n)} {hits}/{n}")

    say("\nDISCOURSE MARKERS  (how the observation is introduced)")
    for label, pattern in _MARKERS:
        hits = sum(1 for e in emails if pattern.search(e["body"]))
        if hits:
            say(f'  "{label}"'.ljust(38) + f"{_bar(hits, n)} {hits}/{n}")

    say("\nCTA CATEGORY  (what the ask actually does)")
    cats = Counter(_cta_category(_sentences(e["body"])[-1]
                                 if _sentences(e["body"]) else "")
                   for e in emails)
    for value, count in cats.most_common():
        say(f"  {value:34} {_bar(count, n)} {count}/{n}")

    say("\nRHYTHM  (sentences per email — is every one the same shape?)")
    counts = Counter(len(_sentences(e["body"])) for e in emails)
    for length in sorted(counts):
        say(f"  {length:2} sentences  {_bar(counts[length], n)} {counts[length]}/{n}")
    lengths = [len(_sentences(e["body"])) for e in emails]
    words = [len(re.findall(r"[a-z']+", e['body'].lower())) for e in emails]
    say(f"  mean {sum(lengths)/n:.1f} sentences, {sum(words)/n:.0f} words; "
        f"range {min(lengths)}-{max(lengths)} sentences")

    say("\nSHAPE x CLOSING  (do the two rotations move together?)")
    pairs = Counter((e["shape"], e["closing"]) for e in emails)
    for (shape, close), count in pairs.most_common(10):
        flag = "  <-- REPEATED PAIR" if count > 2 else ""
        say(f"  {count}x  {shape} + {close}{flag}")
    say(f"  distinct pairings: {len(pairs)}/{n}")

    say("\nREPEATED PHRASES  (6 words, appearing in 2+ drafts)")
    seen = Counter()
    for e in emails:
        for gram in set(_ngrams(e["body"], 6)):
            seen[gram] += 1
    repeated = [(g, c) for g, c in seen.most_common(40) if c > 1]
    if not repeated:
        say("  none — no six-word run appears in two drafts")
    for gram, count in repeated[:12]:
        say(f"  {count}x  {gram}")

    say("\nSUBJECT LINES")
    for e in emails:
        say(f"  {e['subject'][:64]}")

    say(f"\nDASHES  {Counter(e['dashes'] for e in emails).most_common()}")
    say("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
