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
        })
    return out


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
