from __future__ import annotations

# Prospect enrichment — fetches the full Unipile profile + recent posts and
# persists the useful signal so the drafter and pipeline can use it.
#
# What gets enriched (per prospect, 1-2 Unipile API calls):
#   • Full profile via GET /users/{provider_id}
#       network_distance, follower_count, connections_count, mutual,
#       is_premium / is_creator / is_open_profile / is_relationship,
#       pronoun, public_identifier, (full) headline + location
#   • Most recent post timestamp via GET /users/{provider_id}/posts?limit=1
#
# Re-enrichment cadence: 7 days by default. Prospects whose `enriched_at` is
# older than that (or NULL) are picked up by the daily cycle.

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from . import db
from .config import Config

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
    recently enough. Prospect rows that lack a provider_id can't be enriched
    via Unipile, so we don't queue them."""
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


# -------------------------------------------------------------- Unipile fetch

class _ProfileClient:
    """Thin client used by enrichment. Could fold into the adapter, but
    keeping it separate avoids growing the adapter interface for what is
    really a Unipile-specific extension."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client = httpx.Client(
            base_url=f"https://{cfg.unipile_dsn}/api/v1",
            headers={"X-API-KEY": cfg.unipile_api_key, "accept": "application/json"},
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def fetch_profile(self, provider_id: str) -> dict | None:
        """Returns the profile dict or None if the API rejects (locked profile,
        404, etc.)."""
        r = self._client.get(f"/users/{provider_id}", params={"account_id": self.cfg.unipile_account_id})
        if r.status_code == 200:
            return r.json()
        logger.warning("profile fetch failed for %s: %s — %s", provider_id, r.status_code, r.text[:120])
        return None

    def latest_post_timestamp(self, provider_id: str) -> str | None:
        """One call to fetch only the most recent post date, or None if no posts."""
        r = self._client.get(
            f"/users/{provider_id}/posts",
            params={"account_id": self.cfg.unipile_account_id, "limit": 1},
        )
        if r.status_code != 200:
            return None
        items = r.json().get("items", [])
        if not items:
            return None
        return items[0].get("date")


# -------------------------------------------------------------- field mapping

def _profile_to_db_fields(profile: dict) -> dict:
    """Map the /users/{id} JSON shape into our prospects-table columns."""
    def b(v):
        return 1 if v else 0

    return {
        "headline":                 profile.get("headline") or None,
        "location":                 profile.get("location") or None,
        "public_identifier":        profile.get("public_identifier") or None,
        "network_distance":         profile.get("network_distance") or None,
        "mutual_connections_count": profile.get("shared_connections_count"),
        "follower_count":           profile.get("follower_count"),
        "connections_count":        profile.get("connections_count"),
        "is_premium":               b(profile.get("is_premium")),
        "is_open_profile":          b(profile.get("is_open_profile")),
        "is_creator":               b(profile.get("is_creator")),
        "is_influencer":            b(profile.get("is_influencer")),
        "is_relationship":          b(profile.get("is_relationship")),
        "pronoun":                  profile.get("pronoun") or None,
    }


# -------------------------------------------------------------- API

def enrich(cfg: Config, prospect_id: int, *, client: _ProfileClient | None = None) -> bool:
    """Enrich a single prospect. Returns True on success, False if the API
    rejected (locked profile etc.). Caller owns the client lifecycle if
    they pass one; otherwise this function manages it."""
    db.init_db()
    prospect = db.get_prospect(prospect_id)
    if not prospect:
        raise ValueError(f"no prospect {prospect_id}")
    if not prospect["provider_id"]:
        logger.info("prospect %d has no provider_id — cannot enrich", prospect_id)
        return False

    own_client = False
    if client is None:
        client = _ProfileClient(cfg)
        own_client = True
    try:
        profile = client.fetch_profile(prospect["provider_id"])
        if profile is None:
            return False

        fields = _profile_to_db_fields(profile)
        fields["last_post_at"] = client.latest_post_timestamp(prospect["provider_id"])
        fields["enriched_at"]  = db.now()

        # Update the DB with whatever fields we got.
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [prospect_id]
        with db.connect() as conn:
            conn.execute(f"UPDATE prospects SET {sets} WHERE id = ?", values)
        db.log_action(
            prospect_id, "enrich",
            json.dumps({"network_distance": fields.get("network_distance"),
                        "mutual": fields.get("mutual_connections_count")}),
            "ok", False,
        )
        return True
    finally:
        if own_client:
            client.close()


# Network-distance values returned by Unipile when an invite is accepted.
# Both forms appear in different account configurations; we accept either.
_FIRST_DEGREE_VALUES = {"FIRST_DEGREE", "DISTANCE_1", "1", 1}


def check_acceptances(
    cfg: Config, *, limit: int | None = None,
    client: _ProfileClient | None = None,
) -> CheckAcceptResult:
    """For each prospect in `connection_sent`, fetch their current profile and
    check `network_distance`. If they're now 1st-degree, the invite was
    accepted — move the prospect to `connected` so the DM1 step picks them up.

    Important: this only TRANSITIONS status. It does not touch enrichment
    fields (use `enrich` for that). One Unipile API call per pending invite.
    Skips prospects with no provider_id (can't be looked up)."""
    db.init_db()
    result = CheckAcceptResult()

    candidates = [
        p for p in db.list_prospects(status="connection_sent", limit=10_000)
        if p["provider_id"]
    ]
    if limit is not None:
        candidates = candidates[:limit]

    own_client = client is None
    if own_client:
        client = _ProfileClient(cfg)
    try:
        for p in candidates:
            try:
                profile = client.fetch_profile(p["provider_id"])
                if profile is None:
                    result.errors += 1
                    result.error_messages.append(f"p={p['id']}: fetch returned None")
                    continue
                distance = profile.get("network_distance")
                if distance in _FIRST_DEGREE_VALUES:
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
        if own_client:
            client.close()
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

    client = _ProfileClient(cfg)
    try:
        for p in candidates:
            try:
                ok = enrich(cfg, int(p["id"]), client=client)
                if ok:
                    result.enriched += 1
                else:
                    result.failed += 1
            except Exception as e:
                logger.exception("enrich failed for prospect %d", p["id"])
                result.failed += 1
                result.errors.append(f"p={p['id']}: {e}")
    finally:
        client.close()
    return result
