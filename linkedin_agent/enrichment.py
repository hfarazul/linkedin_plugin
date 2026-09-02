from __future__ import annotations

# Prospect enrichment — fetches a profile and its work history, and persists
# the useful signal so the drafter, the pipeline and the transition detectors
# can use it.
#
# Provider-agnostic: routed through Capability.PROFILE and
# Capability.ACCEPTANCE_CHECK, so whichever provider owns those answers. This
# module deliberately knows nothing about Unipile endpoints or PhantomBuster
# agents — that lives behind linkedin_agent/providers/.
#
# What gets persisted per prospect:
#   • prospects columns  headline, location, network distance, counts, flags
#   • positions rows     dated job history, used by the transition detectors
#
# Re-enrichment cadence: 7 days by default. Prospects whose `enriched_at` is
# older than that (or NULL) are picked up by the daily cycle.

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import db
from .config import Config
from .providers import Capability, build_router

logger = logging.getLogger("linkedin.enrichment")

DEFAULT_STALENESS_DAYS = 7


@dataclass
class EnrichResult:
    enriched: int = 0
    failed: int = 0
    skipped_fresh: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class CheckAcceptResult:
    """Result of one acceptance-check pass over `connection_sent` prospects."""
    detected: int = 0           # newly-detected accepts (moved to 'connected')
    still_pending: int = 0      # invites still pending (not 1st-degree yet)
    errors: int = 0             # profile-fetch failures
    error_messages: list[str] = field(default_factory=list)


# -------------------------------------------------------------- predicate

def should_reenrich(prospect, *, now: datetime | None = None, staleness_days: int = DEFAULT_STALENESS_DAYS) -> bool:
    """True if the prospect has never been enriched or hasn't been enriched
    recently enough. Prospect rows that lack a provider_id can't be looked
    up by any provider, so we don't queue them."""
    if not prospect["provider_id"]:
        return False
    if not prospect["enriched_at"]:
        return True
    when = datetime.fromisoformat(prospect["enriched_at"].replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = (now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - when) > timedelta(days=staleness_days)


# -------------------------------------------------------------- API

def _facts_to_db_fields(facts) -> dict:
    """Map a provider-neutral ProfileFacts onto prospects columns.

    Only non-None values are returned, which is a deliberate change from the
    old single-provider behaviour. Providers return different subsets — Unipile
    has `pronoun` and `is_creator`, PhantomBuster has neither but supplies
    firmographics — so a blanket overwrite would silently erase whatever the
    other provider had previously stored for the same prospect.
    """
    candidates = {
        "headline":                 facts.headline,
        "location":                 facts.location,
        "public_identifier":        facts.public_identifier,
        "network_distance":         facts.network_distance,
        "mutual_connections_count": facts.mutual_connections_count,
        "follower_count":           facts.follower_count,
        "connections_count":        facts.connections_count,
        "is_premium":               _as_flag(facts.is_premium),
        "is_open_profile":          _as_flag(facts.is_open_profile),
        "is_creator":               _as_flag(facts.is_creator),
        "is_influencer":            _as_flag(facts.is_influencer),
        "is_relationship":          _as_flag(facts.is_relationship),
        "pronoun":                  facts.pronoun,
    }
    return {k: v for k, v in candidates.items() if v is not None}


def _as_flag(value) -> int | None:
    return None if value is None else (1 if value else 0)


def enrich(cfg: Config, prospect_id: int, *, router=None) -> bool:
    """Enrich a single prospect. Returns True on success, False when the
    profile could not be fetched (locked profile, 404, no provider_id).

    Routed through the capability layer, so whichever provider owns
    Capability.PROFILE answers. Work history is requested at the same time and
    persisted to `positions`, because both providers return it on the same
    call and a second round trip would cost scraping budget for nothing.
    """
    db.init_db()
    prospect = db.get_prospect(prospect_id)
    if not prospect:
        raise ValueError(f"no prospect {prospect_id}")
    if not prospect["provider_id"]:
        logger.info("prospect %d has no provider_id — cannot enrich", prospect_id)
        return False

    own_router = router is None
    if own_router:
        router = build_router(cfg)
    try:
        facts = router.perform(
            Capability.PROFILE, "fetch_profile",
            prospect["provider_id"], with_experience=True,
        )
        if facts is None:
            return False

        fields = _facts_to_db_fields(facts)
        fields["enriched_at"] = db.now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [prospect_id]
        with db.connect() as conn:
            conn.execute(f"UPDATE prospects SET {sets} WHERE id = ?", values)

        stored = 0
        if facts.positions:
            stored = db.replace_positions(prospect_id, facts.positions)

        db.log_action(
            prospect_id, "enrich",
            json.dumps({"provider": facts.source,
                        "network_distance": fields.get("network_distance"),
                        "positions": stored}),
            "ok", False,
        )
        return True
    finally:
        if own_router:
            router.close()


def check_acceptances(
    cfg: Config, *, limit: int | None = None, router=None,
) -> CheckAcceptResult:
    """For each prospect in `connection_sent`, fetch their current profile and
    check `network_distance`. If they're now 1st-degree, the invite was
    accepted — move the prospect to `connected` so the DM1 step picks them up.

    Important: this only TRANSITIONS status. It does not touch enrichment
    fields (use `enrich` for that). One provider call per pending invite.
    Skips prospects with no provider_id (can't be looked up).

    Routed through Capability.ACCEPTANCE_CHECK. Unipile reads
    `network_distance`; PhantomBuster reads `connectionDegree` from a profile
    re-scrape. Both answer the same question, so this function does not know
    or care which responded."""
    db.init_db()
    result = CheckAcceptResult()

    candidates = [
        p for p in db.list_prospects(status="connection_sent", limit=10_000)
        if p["provider_id"]
    ]
    if limit is not None:
        candidates = candidates[:limit]

    own_router = router is None
    if own_router:
        router = build_router(cfg)
    try:
        for p in candidates:
            try:
                accepted = router.perform(
                    Capability.ACCEPTANCE_CHECK, "check_acceptance",
                    p["provider_id"])
                if accepted is None:
                    result.errors += 1
                    result.error_messages.append(f"p={p['id']}: fetch returned None")
                    continue
                distance = "FIRST_DEGREE" if accepted else "not_first_degree"
                if accepted:
                    db.set_status(int(p["id"]), "connected")
                    db.log_action(
                        int(p["id"]), "accept_detected",
                        json.dumps({"prev_distance": p["network_distance"],
                                    "current_distance": distance}),
                        "ok", False,
                    )
                    result.detected += 1
                    logger.info(
                        "accept detected for prospect %d (%s)",
                        p["id"], p["full_name"],
                    )
                else:
                    result.still_pending += 1
            except Exception as e:
                logger.exception("acceptance check failed for prospect %d", p["id"])
                result.errors += 1
                result.error_messages.append(f"p={p['id']}: {e}")
    finally:
        if own_router:
            router.close()
    return result


def enrich_stale(cfg: Config, *, staleness_days: int = DEFAULT_STALENESS_DAYS,
                  limit: int | None = None) -> EnrichResult:
    """Find every prospect whose enrichment is stale (or missing) and refresh.
    Used by `linkedin daily` as a step before reactions/drafts."""
    db.init_db()
    result = EnrichResult()
    now = datetime.now(timezone.utc)

    # Build query: include rows with provider_id set, and either no enriched_at
    # or it's older than staleness_days. Simpler to filter in Python — there
    # are never that many rows.
    candidates = []
    for p in db.list_prospects(limit=10_000):
        if should_reenrich(p, now=now, staleness_days=staleness_days):
            candidates.append(p)

    if limit is not None:
        candidates = candidates[:limit]

    router = build_router(cfg)
    try:
        for p in candidates:
            try:
                ok = enrich(cfg, int(p["id"]), router=router)
                if ok:
                    result.enriched += 1
                else:
                    result.failed += 1
            except Exception as e:
                logger.exception("enrich failed for prospect %d", p["id"])
                result.failed += 1
                result.errors.append(f"p={p['id']}: {e}")
    finally:
        router.close()
    return result
