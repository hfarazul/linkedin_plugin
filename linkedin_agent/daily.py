from __future__ import annotations

# Daily orchestration — the one entry point cron fires hourly during business
# hours. Sequences:
#   1. campaign sync   (refresh DB campaign rows from markdown files)
#   2. poll            (fetch inbound replies, halt sequences, notify)
#   3. react           (warm up `targeted` prospects with relevant recent posts)
#   4. connect         (draft connect notes for `reacted` prospects)
#   5. dm1             (draft first DM for `connected` prospects)
#   6. followup        (draft DM2/DM3 for `dm_sent` prospects per cadence)
#   7. send-approved   (flush drafts that got approved outside the window)
#   8. auto-ghost      (also handled inside followup; idempotent)
#
# Each step checks caps and skips if a daily limit is hit. Each step logs
# what it did. A final Telegram summary message goes out so the user sees a
# digest in their chat without having to run `status`.

import json
import logging
from dataclasses import dataclass, field

from . import campaigns as campaigns_mod
from . import db, safety, send_window
from .adapters import get_adapter
from .config import Config
from .telegram import TelegramClient, TelegramError

logger = logging.getLogger("linkedin.daily")


@dataclass
class DailyResult:
    polled_messages: int = 0
    new_inbound: int = 0
    accepts_detected: int = 0
    reactions_sent: int = 0
    connect_drafts: int = 0
    dm1_drafts: int = 0
    dm2_drafts: int = 0
    dm3_drafts: int = 0
    ghosted: int = 0
    approved_sent: int = 0
    skipped_cap_hit: list[str] = field(default_factory=list)
    skipped_window_steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"📥 polled: {self.polled_messages} (new: {self.new_inbound})",
            f"✅ accepts detected: {self.accepts_detected}",
            f"❤️  reactions: {self.reactions_sent}",
            f"🤝 connect drafts: {self.connect_drafts}",
            f"📝 DM drafts: dm1={self.dm1_drafts} · dm2={self.dm2_drafts} · dm3={self.dm3_drafts}",
            f"👻 auto-ghosted: {self.ghosted}",
            f"📤 sent from approved-queue: {self.approved_sent}",
        ]
        if self.skipped_window_steps:
            lines.append(f"⏸️  window closed — skipped: {', '.join(self.skipped_window_steps)}")
        if self.skipped_cap_hit:
            lines.append(f"⚠️ caps hit: {', '.join(self.skipped_cap_hit)}")
        if self.errors:
            lines.append(f"❌ errors: {len(self.errors)}")
        return "\n".join(lines)


def run_daily(
    cfg: Config,
    *,
    adapter=None,
    telegram=None,
    drafter=None,
    notify_summary: bool = True,
) -> DailyResult:
    """Run the full daily cycle. All dependencies are injectable for tests."""
    from .drafter import draft as default_drafter, warmup_auth
    from .followup import run_followup_cycle
    from .poll import poll_once
    from .bot_daemon import send_draft_via_adapter

    # Track whether the caller wants the real drafter — only that case needs
    # OAuth warmup (tests pass a stub drafter that doesn't shell out).
    using_default_drafter = drafter is None
    drafter = drafter or default_drafter
    result = DailyResult()

    # Within a single run, react step and connect step both want recent posts
    # for the same prospect (react picks the top one to LIKE; connect-drafter
    # wants 2-3 for hook material). Cache the react-step fetch so the connect
    # step reuses it instead of re-hitting Unipile.
    posts_cache: dict[int, list] = {}

    # Claude-unavailable circuit breaker. If the cron context can't reach
    # claude (as observed on 2026-05-21 onwards), the first few drafter
    # attempts fail; we then skip subsequent draft steps for this cycle
    # rather than emitting one error per prospect.
    consecutive_claude_failures = 0
    claude_broken = False

    db.init_db()

    own_adapter = False
    own_telegram = False
    if adapter is None:
        adapter = get_adapter(cfg)
        own_adapter = True
    if telegram is None:
        try:
            telegram = TelegramClient(cfg)
            own_telegram = True
        except TelegramError as e:
            logger.warning("telegram disabled (%s)", e)
            telegram = None

    try:
        # --- 1. campaigns: sync markdown files → DB ----------------------------
        for path in campaigns_mod.list_brief_files():
            try:
                brief = campaigns_mod.load_brief(path.stem)
                cid = db.upsert_campaign(
                    slug=brief.slug, name=brief.name,
                    brief_path=str(brief.path), target_icp=brief.target_icp,
                )
                if brief.status in db.VALID_CAMPAIGN_STATUSES:
                    db.set_campaign_status(cid, brief.status)
            except Exception as e:
                logger.warning("failed to sync campaign %s: %s", path, e)
                result.errors.append(f"campaign sync {path.name}: {e}")

        # --- 1.5 warm up claude OAuth before parallel drafter calls -----------
        # When the OAuth credential refreshes during the cron run, parallel
        # `claude -p` calls race on the refresh and some exit 1 with empty
        # stderr (incident 2026-05-20: 4/4 dm1 drafts failed in that mode).
        # A single trivial call here forces the refresh to complete BEFORE
        # any drafter step. Best-effort — never blocks the cron.
        if using_default_drafter:
            warmup_auth()

        # --- 2. poll inbound replies -------------------------------------------
        try:
            poll_result = poll_once(cfg, notify=(telegram is not None))
            result.polled_messages = poll_result.fetched
            result.new_inbound = poll_result.new_inbound
        except Exception as e:
            logger.exception("poll failed")
            result.errors.append(f"poll: {e}")

        # --- 2.5 check connection acceptances ----------------------------------
        # Unipile's messages endpoint doesn't surface accept events, so we
        # poll profile-distance for every `connection_sent` prospect. Anyone
        # who is now 1st-degree accepted the invite — flip them to `connected`
        # so step 5 (dm1) drafts a follow-up in the SAME run. Only runs when
        # Unipile creds are configured; tests with fake adapter skip this.
        if cfg.unipile_api_key and cfg.unipile_account_id and cfg.unipile_dsn:
            try:
                from .enrichment import check_acceptances
                accept_result = check_acceptances(cfg)
                result.accepts_detected = accept_result.detected
                if accept_result.errors:
                    result.errors.extend(accept_result.error_messages[:3])
            except Exception as e:
                logger.exception("acceptance check failed")
                result.errors.append(f"check_accepts: {e}")

        # --- 3. react to recent posts of targeted prospects --------------------
        # Reactions are not window-gated. LIKEs are routine LinkedIn activity
        # that fires on weekends/evenings normally; the daily cron's own
        # schedule (9-5 Mon-Fri by default) is the practical envelope.
        # Connection requests and DMs are still gated — those are window-
        # protected through the daemon's approval-send path.
        for p in db.list_prospects(status="targeted", limit=10_000):
            try:
                safety.check_cap(cfg, "react")
            except safety.RateLimitExceeded:
                result.skipped_cap_hit.append("react")
                break
            try:
                # Fetch 3 (not 1) so the connect-step drafter can reuse them.
                # We still react only to posts[0] — the rest is free cache fill.
                posts = adapter.get_recent_posts(p["linkedin_url"], limit=3)
                if not posts:
                    continue
                posts_cache[int(p["id"])] = posts
                if cfg.dry_run:
                    # DRY_RUN: skip the LinkedIn write but still advance state +
                    # log the intent so subsequent daily steps see the prospect
                    # as reacted (mirrors the CLI react command's behavior).
                    db.set_status(int(p["id"]), "reacted")
                    db.log_action(int(p["id"]), "react",
                                  json.dumps({"post": posts[0].post_id, "via": "daily"}),
                                  "dry_run", True)
                else:
                    adapter.react(posts[0], reaction="LIKE")
                    db.set_status(int(p["id"]), "reacted")
                    db.log_action(int(p["id"]), "react",
                                  json.dumps({"post": posts[0].post_id, "via": "daily"}),
                                  posts[0].post_id, False)
                result.reactions_sent += 1
            except Exception as e:
                logger.warning("react failed for prospect %d: %s", p["id"], e)
                result.errors.append(f"react p={p['id']}: {e}")

        # --- 4. connect drafts for reacted prospects ---------------------------
        # Fetch each prospect's recent posts so the drafter has something
        # specific to reference. Without posts the drafter returns
        # INSUFFICIENT_CONTEXT for most profiles.
        for p in db.list_prospects(status="reacted", limit=10_000):
            if claude_broken:
                break
            try:
                safety.check_cap(cfg, "connect")
            except safety.RateLimitExceeded:
                result.skipped_cap_hit.append("connect")
                break
            if _has_pending_draft(int(p["id"]), "connect_note"):
                continue
            try:
                body = drafter("connect_note", int(p["id"]),
                               recent_posts=_fetch_posts_for_draft(adapter, p, cache=posts_cache))
                consecutive_claude_failures = 0  # success resets the breaker
            except Exception as e:
                if _is_terminal_drafter_failure(e):
                    # Drafter explicitly told us this prospect can't be drafted
                    # (no specific signal to reference). Mark them skipped so
                    # subsequent cron fires don't waste a `claude -p` call on
                    # the same hopeless case. Not counted as an error — this
                    # is the system honoring an intended terminal signal.
                    db.set_status(int(p["id"]), "skipped")
                    db.log_action(
                        int(p["id"]), "skipped_drafter",
                        json.dumps({"kind": "connect_note", "reason": "INSUFFICIENT_CONTEXT"}),
                        "skipped", False,
                    )
                    logger.info("prospect %d marked skipped — drafter INSUFFICIENT_CONTEXT", p["id"])
                    consecutive_claude_failures = 0
                else:
                    # All non-terminal failures: log the per-prospect error
                    # so they stay visible in the summary. If they look like
                    # the cron claude-unavailable pattern, also count toward
                    # the breaker; on threshold, abort remaining drafter
                    # steps cleanly with one extra summary line.
                    logger.warning("connect drafter failed for prospect %d: %s", p["id"], e)
                    result.errors.append(f"connect draft p={p['id']}: {e}")
                    if _is_claude_unavailable_failure(e):
                        consecutive_claude_failures += 1
                        if consecutive_claude_failures >= _CLAUDE_FAILURE_BREAKER_THRESHOLD:
                            claude_broken = True
                            result.errors.append(
                                f"draft steps aborted: claude unavailable in this context "
                                f"({consecutive_claude_failures} consecutive `claude -p exited 1` "
                                f"failures). Run `linkedin daily` interactively to recover."
                            )
                            logger.warning(
                                "claude circuit breaker tripped at %d failures — "
                                "skipping remaining drafter steps",
                                consecutive_claude_failures,
                            )
                            break
                continue
            did = db.enqueue_draft(int(p["id"]), "connect_note", body)
            _push_to_telegram(telegram, did, "connect_note", body, p)
            result.connect_drafts += 1

        # --- 5. dm1 drafts for connected prospects -----------------------------
        for p in db.list_prospects(status="connected", limit=10_000):
            if claude_broken:
                break
            if (p["dm_count"] or 0) > 0:
                continue
            try:
                safety.check_cap(cfg, "dm")
            except safety.RateLimitExceeded:
                result.skipped_cap_hit.append("dm")
                break
            if _has_pending_draft(int(p["id"]), "dm1"):
                continue
            try:
                body = drafter("dm1", int(p["id"]),
                               recent_posts=_fetch_posts_for_draft(adapter, p))
                consecutive_claude_failures = 0
            except Exception as e:
                logger.warning("dm1 drafter failed for prospect %d: %s", p["id"], e)
                result.errors.append(f"dm1 draft p={p['id']}: {e}")
                if _is_claude_unavailable_failure(e):
                    consecutive_claude_failures += 1
                    if consecutive_claude_failures >= _CLAUDE_FAILURE_BREAKER_THRESHOLD:
                        claude_broken = True
                        result.errors.append(
                            "draft steps aborted: claude unavailable in this context. "
                            "Run `linkedin daily` interactively to recover."
                        )
                        logger.warning("claude circuit breaker tripped during dm1 step")
                        break
                continue
            did = db.enqueue_draft(int(p["id"]), "dm1", body)
            _push_to_telegram(telegram, did, "dm1", body, p)
            result.dm1_drafts += 1

        # --- 6. follow-ups (dm2/dm3) + auto-ghost ------------------------------
        # Skip follow-up drafting if the circuit broke; auto-ghost still
        # happens because it's a pure status-transition without drafter calls.
        if claude_broken:
            logger.info("skipping followup drafts — claude breaker tripped")
        else:
            try:
                f_result = run_followup_cycle(cfg, drafter=drafter, telegram=telegram)
                result.dm2_drafts = f_result.dm2_enqueued
                result.dm3_drafts = f_result.dm3_enqueued
                result.ghosted = f_result.ghosted
            except Exception as e:
                logger.exception("followup failed")
                result.errors.append(f"followup: {e}")

        # --- 7. flush approved-but-not-sent queue if window is open ------------
        if send_window.is_open():
            for d in db.list_pending_drafts(status="approved"):
                try:
                    safety.check_cap(cfg, "dm" if d["kind"].startswith("dm") else "connect")
                except safety.RateLimitExceeded:
                    result.skipped_cap_hit.append(d["kind"])
                    break
                try:
                    send_draft_via_adapter(cfg, adapter, d, source="daily")
                    result.approved_sent += 1
                except Exception as e:
                    logger.warning("send-approved failed for draft %d: %s", d["id"], e)
                    result.errors.append(f"send-approved d={d['id']}: {e}")

        # --- 8. summary to Telegram --------------------------------------------
        if telegram and notify_summary:
            try:
                telegram.notify_text(f"📊 Daily run\n{result.summary()}")
            except Exception as e:
                logger.warning("summary push failed: %s", e)

        # Log an explicit "daily_completed" action so the healthcheck can
        # verify the cron actually fired. prospect_id=NULL (it's a system-level
        # action). dry_run=False even in DRY_RUN mode — the daily itself ran.
        db.log_action(
            None, "daily_completed",
            json.dumps({
                "accepts_detected": result.accepts_detected,
                "reactions": result.reactions_sent,
                "connect_drafts": result.connect_drafts,
                "dm1_drafts": result.dm1_drafts,
                "dm2_drafts": result.dm2_drafts,
                "dm3_drafts": result.dm3_drafts,
                "ghosted": result.ghosted,
                "approved_sent": result.approved_sent,
                "errors": len(result.errors),
            }),
            "ok",
            False,
        )

    finally:
        if own_adapter:
            adapter.close()
        if own_telegram and telegram:
            telegram.close()

    return result


def _is_terminal_drafter_failure(exc: Exception) -> bool:
    """A drafter that returns INSUFFICIENT_CONTEXT is telling us this prospect
    genuinely can't be drafted — no useful signal in their profile/posts to
    reference. That's a terminal signal: retrying every 3 hours wastes
    `claude -p` calls and produces noise in the Telegram summary.

    Distinguishing this from transient failures (claude binary errors, network
    blips) matters — we only auto-skip on the terminal signal. The
    INSUFFICIENT_CONTEXT marker is encoded in DrafterError's message by
    drafter.draft() — see drafter.py."""
    return "INSUFFICIENT_CONTEXT" in str(exc)


def _is_claude_unavailable_failure(exc: Exception) -> bool:
    """Detect the cron-spawn `claude -p exited 1` failure mode.

    Verified 2026-05-21: when daily.py runs under launchd-managed cron, every
    `claude -p` call exits 1 with empty stderr. The same script run from an
    interactive shell with identical env works fine. We can't fully diagnose
    the cron sandbox issue from inside the cron — so when we see this pattern,
    we trip a circuit breaker and skip remaining drafter steps for the cycle
    rather than emitting 19 identical errors to Telegram."""
    return "exited 1" in str(exc)


# After N consecutive `claude -p exited 1` failures in the same cycle, assume
# we're in a bad cron context and stop firing more drafter calls. Three is
# enough to distinguish a genuine pattern from a one-off transient hiccup.
_CLAUDE_FAILURE_BREAKER_THRESHOLD = 3


def _fetch_posts_for_draft(adapter, prospect, *, limit: int = 3, cache: dict | None = None) -> list[dict]:
    """Best-effort: fetch a prospect's recent posts and return them as the dict
    list the drafter expects. Returns empty list on any error so the drafter
    can still try (and may itself bail with INSUFFICIENT_CONTEXT).

    If `cache` is provided and contains posts for this prospect (keyed by
    int(prospect_id)), reuse those instead of hitting the adapter. The react
    step in `run_daily` populates the cache so the connect step here can avoid
    a second round-trip for the same profile in the same cron fire."""
    pid = int(prospect["id"])
    if cache is not None and pid in cache:
        cached = cache[pid][:limit]
        return [{"text": p.text, "posted_at": p.posted_at} for p in cached]
    try:
        posts = adapter.get_recent_posts(prospect["linkedin_url"], limit=limit)
    except Exception as e:
        logger.warning("get_recent_posts failed for prospect %d: %s", prospect["id"], e)
        return []
    return [{"text": p.text, "posted_at": p.posted_at} for p in posts]


def _has_pending_draft(prospect_id: int, kind: str) -> bool:
    with db.connect() as conn:
        cur = conn.execute(
            """SELECT 1 FROM pending_drafts
               WHERE prospect_id = ? AND kind = ? AND status IN ('pending', 'approved')
               LIMIT 1""",
            (prospect_id, kind),
        )
        return cur.fetchone() is not None


def _push_to_telegram(telegram, draft_id, kind, body, prospect_row):
    if telegram is None:
        return
    campaign_name = None
    if prospect_row["campaign_id"]:
        camp = db.get_campaign(int(prospect_row["campaign_id"]))
        campaign_name = camp["name"] if camp else None
    try:
        msg_id = telegram.push_draft_for_approval(
            draft_id=draft_id, kind=kind, body=body,
            prospect_name=prospect_row["full_name"],
            prospect_company=prospect_row["company"],
            prospect_url=prospect_row["linkedin_url"],
            campaign_name=campaign_name,
        )
        db.set_draft_telegram_id(draft_id, msg_id)
    except Exception as e:
        logger.warning("telegram push for draft %d failed: %s", draft_id, e)
