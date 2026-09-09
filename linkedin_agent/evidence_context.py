"""Assemble the drafter's evidence payload for a prospect already in the DB.

`evidence.py` is pure: it types facts and decides what they license, and knows
nothing about storage. `scripts/smoke_e2e.py` could call it directly because it
holds a live `ProfileFacts` straight from the provider. The production callers
— daily, followup, poll — hold a prospect row and some position rows instead,
so something has to reconstitute the one from the other. This is that.

Two things it is careful about.

**Ordering.** Everything downstream reads `positions[0]` as "where they work
now". `db.list_positions` is ordered to match `phantombuster.profile_from_row`
for exactly this reason; if those two ever disagree again, a prospect gets an
email naming an employer they left.

**What travels per kind.** The evidence bundle governs what may be SAID and
applies to every kind. The presentation choices — shape, closing register,
positioning angle — do not: an email shape asks for paragraph ordering that a
300-character connect note cannot hold, and dm3 is specified as a breakup line
with no question at all, which the shape check would fail on every attempt.
So they are attached only where they mean something. See `_EXTRAS`.
"""

from __future__ import annotations

import logging

from . import db
from . import evidence as evidence_mod
from .providers.capabilities import Position, ProfileFacts, is_direct_transition

logger = logging.getLogger("linkedin.evidence_context")


# Which presentation choices are meaningful for which draft kind.
#
#   shape        the paragraph outline. Written for, and calibrated against,
#                a 300-1200 char email. `missing_shape_elements` checks it.
#   positioning  how to introduce Cortivo. Only meaningful on a first touch;
#                by dm2 they have already been introduced.
#   closing      the register of the ask. dm3 is a breakup with no ask, and a
#                reply's register is set by the message being answered.
#
# The bundle itself — tier, pain_claim_licensed, facts, observations, signals,
# unknowns — is attached for every kind without exception. That is what
# activates the pain-claim and inferred-relevance gates, and it is the whole
# point of this module.
_EXTRAS = {
    "email1":       {"shape", "positioning", "closing"},
    "connect_note": {"positioning", "closing"},
    "dm1":          {"positioning", "closing"},
    "dm2":          {"closing"},
    "dm3":          set(),
    "reply":        set(),
}


def _position_from_row(row) -> Position:
    """One stored position row back into the DTO the evidence engine reads."""
    return Position(
        company=row["company"] or "",
        title=row["title"],
        company_id=row["company_id"],
        company_url=row["company_url"],
        start_date=row["started_at"],
        end_date=row["ended_at"],
        is_current=bool(row["is_current"]),
        date_precision=row["date_precision"] or "unknown",
        location=row["location"],
        description=row["description"],
        source=row["source"] or "unknown",
    )


def facts_for(prospect_id: int) -> ProfileFacts | None:
    """Rebuild a ProfileFacts from what we have stored.

    Reuses the provider DTO rather than inventing a shim, so the evidence
    engine sees the same shape whether the data arrived from a live scrape or
    from the database an hour later.
    """
    prospect = db.get_prospect(prospect_id)
    if prospect is None:
        return None
    positions = [_position_from_row(r) for r in db.list_positions(prospect_id)]
    return ProfileFacts(
        provider_id=prospect["provider_id"],
        linkedin_url=prospect["linkedin_url"],
        full_name=prospect["full_name"],
        headline=prospect["headline"],
        location=prospect["location"],
        company_name=prospect["company"],
        positions=positions,
        source="db",
    )


def inbound_messages(prospect_id: int, limit: int = 5) -> list[dict]:
    """The prospect's own words to us, most recent last."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT body FROM messages
                WHERE prospect_id = ? AND direction = 'inbound'
                ORDER BY sent_at DESC LIMIT ?""",
            (prospect_id, limit)).fetchall()
    return [{"body": r["body"]} for r in reversed(rows) if (r["body"] or "").strip()]


def build_for(kind: str, prospect_id: int, recent_posts=None) -> dict | None:
    """The evidence payload for one prospect, or None if we cannot build one.

    None means "we could not assemble evidence", not "there is no evidence" —
    the caller must treat it as a reason to be careful, never as permission to
    skip the gates. `drafter.draft` fails closed on a None for email kinds;
    for DM kinds a None preserves the pre-wiring behaviour rather than
    silently loosening anything.
    """
    facts = facts_for(prospect_id)
    if facts is None:
        return None

    # Only for a reply do the prospect's messages count as evidence. On a first
    # touch there are none, and on a follow-up the thread is ours, not theirs.
    messages = inbound_messages(prospect_id) if kind == "reply" else None

    direct = bool(len(facts.positions) > 1
                  and is_direct_transition(facts.positions[1], facts.positions[0]))
    bundle = evidence_mod.build_evidence(
        facts, recent_posts, transition_is_direct=direct, messages=messages)

    extras = _EXTRAS.get(kind, set())
    # Hashed on the prospect, so re-running the same person is reproducible
    # while a list of them spreads across the available choices.
    key = facts.provider_id or facts.linkedin_url or str(prospect_id)

    shape = closing = positioning = None
    domain = None
    if "shape" in extras:
        shape = evidence_mod.choose_shape(bundle, key)
    if "closing" in extras:
        closing = evidence_mod.choose_closing(key)
    if "positioning" in extras:
        positioning = evidence_mod.choose_positioning(facts, bundle, key)
        domain = evidence_mod.matching_proof_domain(facts, bundle)

    payload = bundle.as_dict(shape=shape, closing=closing,
                             positioning=positioning, proof_domain=domain)
    logger.info(
        "evidence for prospect %d (%s): tier=%s pain_claim_licensed=%s "
        "signals=%d unknowns=%d",
        prospect_id, kind, payload["tier"], payload["pain_claim_licensed"],
        len(payload["signals"]), len(payload["unknowns"]))
    return payload


def build_safely(kind: str, prospect_id: int, recent_posts=None) -> dict | None:
    """`build_for`, but a failure to assemble evidence never kills a run.

    The callers are cron steps that iterate prospects. An exception here would
    take out the whole step, and the honest degradation is to draft without
    evidence — which is the more conservative path, not the looser one, since
    `draft()` fails closed for email kinds when evidence is absent.
    """
    try:
        return build_for(kind, prospect_id, recent_posts=recent_posts)
    except Exception:
        logger.exception("could not build evidence for prospect %d (%s) — "
                         "drafting without it", prospect_id, kind)
        return None
