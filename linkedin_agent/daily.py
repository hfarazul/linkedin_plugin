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
    from .drafter import draft as default_drafter
    from .followup import run_followup_cycle
    from .poll import poll_once
    from .bot_daemon import send_draft_via_adapter

    drafter = drafter or default_drafter
    result = DailyResult()

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

        # --- 2. poll inbound replies -------------------------------------------
        try:
            poll_result = poll_once(cfg, notify=(telegram is not None))
            result.polled_messages = poll_result.fetched
            result.new_inbound = poll_result.new_inbound
        except Exception as e:
            logger.exception("poll failed")
            result.errors.append(f"poll: {e}")

        # --- 3. react to recent posts of targeted prospects --------------------
        # Reactions are outbound LinkedIn writes, so they respect the send
        # window. Drafting still happens outside hours (see steps 4-6) — it's
        # the API-visible action that gets gated.
        if not send_window.is_open():
            logger.info("send window closed — skipping react step")
            result.skipped_window_steps.append("react")
        else:
            for p in db.list_prospects(status="targeted", limit=10_000):
                try:
                    safety.check_cap(cfg, "react")
                except safety.RateLimitExceeded:
                    result.skipped_cap_hit.append("react")
                    break
                try:
                    posts = adapter.get_recent_posts(p["linkedin_url"], limit=1)
                    if not posts:
                        continue
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
            try:
                safety.check_cap(cfg, "connect")
            except safety.RateLimitExceeded:
                result.skipped_cap_hit.append("connect")
                break
            if _has_pending_draft(int(p["id"]), "connect_note"):
                continue
            try:
                body = drafter("connect_note", int(p["id"]),
                               recent_posts=_fetch_posts_for_draft(adapter, p))
            except Exception as e:
                logger.warning("connect drafter failed for prospect %d: %s", p["id"], e)
                result.errors.append(f"connect draft p={p['id']}: {e}")
                continue
            did = db.enqueue_draft(int(p["id"]), "connect_note", body)
            _push_to_telegram(telegram, did, "connect_note", body, p)
            result.connect_drafts += 1

        # --- 5. dm1 drafts for connected prospects -----------------------------
        for p in db.list_prospects(status="connected", limit=10_000):
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
            except Exception as e:
                logger.warning("dm1 drafter failed for prospect %d: %s", p["id"], e)
                result.errors.append(f"dm1 draft p={p['id']}: {e}")
                continue
            did = db.enqueue_draft(int(p["id"]), "dm1", body)
            _push_to_telegram(telegram, did, "dm1", body, p)
            result.dm1_drafts += 1

        # --- 6. follow-ups (dm2/dm3) + auto-ghost ------------------------------
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

    finally:
        if own_adapter:
            adapter.close()
        if own_telegram and telegram:
            telegram.close()

    return result


def _fetch_posts_for_draft(adapter, prospect, *, limit: int = 3) -> list[dict]:
    """Best-effort: fetch a prospect's recent posts and return them as the dict
    list the drafter expects. Returns empty list on any error so the drafter
    can still try (and may itself bail with INSUFFICIENT_CONTEXT)."""
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
