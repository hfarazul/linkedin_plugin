from __future__ import annotations

import json
import sys

import click
from rich.console import Console
from rich.table import Table

from . import campaigns as campaigns_mod
from . import db, safety
from .adapters import get_adapter
from .config import load as load_config
from .telegram import TelegramClient, TelegramError

console = Console()


def _adapter():
    cfg = load_config()
    db.init_db()
    return cfg, get_adapter(cfg)


@click.group()
def cli() -> None:
    """LinkedIn outreach agent — drives a configurable backend (Playwright or Unipile)."""


@cli.command()
def init() -> None:
    """Create the SQLite DB and required directories."""
    db.init_db()
    console.print(f"[green]✓[/green] DB initialized at {db.DB_PATH}")


@cli.command()
def auth() -> None:
    """Interactive LinkedIn login (Playwright backend only)."""
    cfg = load_config()
    if cfg.backend != "playwright":
        console.print("[yellow]Auth is only required for the Playwright backend. Unipile uses an API key.[/yellow]")
        sys.exit(1)
    from .adapters.playwright_adapter import PlaywrightAdapter
    PlaywrightAdapter(cfg).login_interactive()


@cli.command()
@click.argument("query")
@click.option("--limit", default=10, help="Max prospects to import.")
@click.option("--campaign", default=None, help="Slug of the campaign to attach new prospects to.")
def search(query: str, limit: int, campaign: str | None) -> None:
    """Search LinkedIn people by free-text query and upsert into the DB."""
    cfg, adapter = _adapter()
    campaign_id = None
    if campaign:
        row = db.get_campaign(campaign)
        if not row:
            console.print(f"[red]no campaign with slug {campaign!r}[/red]")
            sys.exit(1)
        campaign_id = int(row["id"])
    try:
        safety.check_cap(cfg, "search")
        hits = adapter.search(query, limit=limit)
        for h in hits:
            pid = db.upsert_prospect(
                linkedin_url=h.linkedin_url,
                full_name=h.full_name,
                headline=h.headline,
                company=h.company,
                title=h.title,
                location=h.location,
                campaign_id=campaign_id,
                provider_id=h.provider_id,
            )
            db.log_action(pid, "search", json.dumps({"query": query, "campaign": campaign}), h.linkedin_url, cfg.dry_run)
        suffix = f" → campaign [bold]{campaign}[/bold]" if campaign else ""
        console.print(f"[green]✓[/green] imported {len(hits)} prospects for query [bold]{query!r}[/bold]{suffix}")
    finally:
        adapter.close()


@cli.command()
@click.argument("prospect_id", type=int)
@click.option("--limit", default=3)
def posts(prospect_id: int, limit: int) -> None:
    """Show recent posts from a prospect (does not consume a daily cap)."""
    cfg, adapter = _adapter()
    try:
        p = db.get_prospect(prospect_id)
        if not p:
            console.print(f"[red]no prospect {prospect_id}[/red]")
            sys.exit(1)
        results = adapter.get_recent_posts(p["linkedin_url"], limit=limit)
        for post in results:
            console.print(f"[cyan]{post.post_urn}[/cyan]  {post.text[:120]}…")
    finally:
        adapter.close()


@cli.command()
@click.argument("prospect_id", type=int)
@click.option("--reaction", default="LIKE", help="LIKE, CELEBRATE, SUPPORT, LOVE, INSIGHTFUL, FUNNY")
def react(prospect_id: int, reaction: str) -> None:
    """React to the prospect's most recent post and mark them as 'reacted'."""
    cfg, adapter = _adapter()
    try:
        safety.check_cap(cfg, "react")
        p = db.get_prospect(prospect_id)
        if not p:
            console.print(f"[red]no prospect {prospect_id}[/red]")
            sys.exit(1)
        posts_ = adapter.get_recent_posts(p["linkedin_url"], limit=1)
        if not posts_:
            console.print("[yellow]no recent posts found[/yellow]")
            return
        post = posts_[0]
        if cfg.dry_run:
            db.log_action(prospect_id, "react", json.dumps({"post": post.post_urn, "reaction": reaction}), "dry_run", True)
            console.print(f"[dim](dry-run)[/dim] would react {reaction} on {post.post_urn}")
            return
        result = adapter.react(post, reaction=reaction)
        db.log_action(prospect_id, "react", json.dumps({"post": post.post_urn, "reaction": reaction}), result, False)
        db.set_status(prospect_id, "reacted")
        safety.human_delay(cfg)
        console.print(f"[green]✓[/green] reacted on {post.post_urn}")
    finally:
        adapter.close()


@cli.command()
@click.argument("prospect_id", type=int)
@click.option("--note", default=None, help="Optional ≤300-char note.")
def connect(prospect_id: int, note: str | None) -> None:
    """Send a connection request."""
    cfg, adapter = _adapter()
    try:
        safety.check_cap(cfg, "connect")
        p = db.get_prospect(prospect_id)
        if not p:
            console.print(f"[red]no prospect {prospect_id}[/red]")
            sys.exit(1)
        if cfg.dry_run:
            db.log_action(prospect_id, "connect", json.dumps({"note": note}), "dry_run", True)
            console.print(f"[dim](dry-run)[/dim] would send connection to {p['linkedin_url']}")
            return
        result = adapter.send_connection(p["linkedin_url"], note=note)
        db.log_action(prospect_id, "connect", json.dumps({"note": note}), result, False)
        db.set_status(prospect_id, "connection_sent")
        safety.human_delay(cfg)
        console.print(f"[green]✓[/green] connection request sent to {p['full_name'] or p['linkedin_url']}")
    finally:
        adapter.close()


@cli.command()
@click.argument("prospect_id", type=int)
@click.argument("body")
def dm(prospect_id: int, body: str) -> None:
    """Send a direct message (must already be connected)."""
    cfg, adapter = _adapter()
    try:
        safety.check_cap(cfg, "dm")
        p = db.get_prospect(prospect_id)
        if not p:
            console.print(f"[red]no prospect {prospect_id}[/red]")
            sys.exit(1)
        if cfg.dry_run:
            db.log_action(prospect_id, "dm", body[:200], "dry_run", True)
            console.print(f"[dim](dry-run)[/dim] would DM: {body[:80]}…")
            return
        result = adapter.send_dm(p["linkedin_url"], body)
        db.log_action(prospect_id, "dm", body[:200], result, False)
        db.record_message(prospect_id, "outbound", body)
        # Bump dm_count + last_dm_at so follow-up scheduler picks this up at
        # the right cadence — same as the bot daemon's approval-send path.
        db.record_dm(prospect_id)
        db.set_status(prospect_id, "dm_sent")
        safety.human_delay(cfg)
        console.print(f"[green]✓[/green] DM sent to {p['full_name'] or p['linkedin_url']}")
    finally:
        adapter.close()


@cli.command()
@click.option("--status", default=None, help="Filter by status.")
@click.option("--limit", default=50)
def pipeline(status: str | None, limit: int) -> None:
    """Show prospects in the pipeline."""
    db.init_db()
    rows = db.list_prospects(status=status, limit=limit)
    if not rows:
        console.print("[dim]no prospects[/dim]")
        return
    t = Table()
    for col in ("id", "name", "title @ company", "status", "last action"):
        t.add_column(col)
    for r in rows:
        t.add_row(
            str(r["id"]),
            r["full_name"] or "—",
            f"{r['title'] or '—'} @ {r['company'] or '—'}",
            r["status"],
            r["last_action_at"] or "—",
        )
    console.print(t)


@cli.command()
def status() -> None:
    """One-shot pipeline dashboard — caps, window, pipeline stages,
    replies needing attention, pending Telegram approvals, due follow-ups."""
    from datetime import datetime, timedelta, timezone
    from . import send_window
    from .followup import is_dm2_due, is_dm3_due
    cfg = load_config()
    db.init_db()

    # --- Caps row -----------------------------------------------------------
    caps_pieces = []
    for kind, field in (("react", "daily_max_reactions"),
                         ("connect", "daily_max_connections"),
                         ("dm", "daily_max_dms")):
        used = db.count_actions_last_24h(kind)
        cap = getattr(cfg, field)
        color = "red" if used >= cap else ("yellow" if used > cap * 0.7 else "green")
        caps_pieces.append(f"{kind} [{color}]{used}/{cap}[/{color}]")
    caps_line = " · ".join(caps_pieces)

    # --- Window status ------------------------------------------------------
    if send_window.is_open():
        window_line = f"[green]OPEN[/green] (next close: today 5:00 PM)"
    else:
        when = send_window.format_next_open()
        window_line = f"[yellow]CLOSED[/yellow] (next opens {when})"

    # --- Pipeline counts ----------------------------------------------------
    pipeline_counts = {}
    for s in db.VALID_STATUSES:
        with db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM prospects WHERE status = ?", (s,)).fetchone()
            pipeline_counts[s] = int(row[0])

    # --- Replies needing attention ------------------------------------------
    replied = db.list_prospects(status="replied", limit=10)

    # --- Pending Telegram approvals ----------------------------------------
    pending = db.list_pending_drafts(status="pending")
    approved_queued = db.list_pending_drafts(status="approved")

    # --- Due follow-ups -----------------------------------------------------
    now = datetime.now(timezone.utc)
    due_followups = []
    for p in db.list_prospects(status="dm_sent", limit=1000):
        if is_dm3_due(p, now):
            due_followups.append((p["full_name"], "DM3"))
        elif is_dm2_due(p, now):
            due_followups.append((p["full_name"], "DM2"))

    # --- Render -------------------------------------------------------------
    console.print()
    console.print(f"  [bold]Caps today[/bold]    {caps_line}")
    console.print(f"  [bold]Window[/bold]        {window_line}")
    console.print()
    t = Table(show_header=True, header_style="bold")
    t.add_column("Stage")
    t.add_column("Count", justify="right")
    for s in db.VALID_STATUSES:
        count = pipeline_counts[s]
        if count == 0 and s in ("skipped",):
            continue
        marker = "  ⚠" if (s == "replied" and count > 0) else "   "
        t.add_row(f"{marker} {s}", str(count))
    console.print(t)

    if replied:
        console.print()
        console.print(f"[bold yellow]⚠ {len(replied)} {'reply' if len(replied)==1 else 'replies'} needing attention[/bold yellow]")
        for r in replied[:5]:
            who = r["full_name"] or r["linkedin_url"]
            when = r["last_action_at"] or ""
            company = f" ({r['company']})" if r['company'] else ""
            console.print(f"    • {who}{company} — {when[:16]}")

    if pending:
        console.print()
        console.print(f"📤 [bold]{len(pending)}[/bold] draft(s) pending approval in Telegram")
    if approved_queued:
        console.print(f"⏸️  [bold]{len(approved_queued)}[/bold] approved-but-queued (will send next window)")

    if due_followups:
        console.print()
        console.print(f"📅 [bold]{len(due_followups)}[/bold] follow-up(s) due:")
        for name, kind in due_followups[:5]:
            console.print(f"    • {name} ({kind})")

    console.print()


@cli.command()
def caps() -> None:
    """Show today's usage against the daily caps."""
    cfg = load_config()
    db.init_db()
    t = Table(title="Daily usage (last 24h)")
    t.add_column("kind"); t.add_column("used"); t.add_column("cap")
    for kind, field in (
        ("search", "daily_max_searches"),
        ("react", "daily_max_reactions"),
        ("connect", "daily_max_connections"),
        ("dm", "daily_max_dms"),
    ):
        used = db.count_actions_last_24h(kind)
        cap = getattr(cfg, field)
        color = "red" if used >= cap else ("yellow" if used > cap * 0.7 else "green")
        t.add_row(kind, f"[{color}]{used}[/{color}]", str(cap))
    console.print(t)


# ----------------------------------------------------------------- campaigns

@cli.group()
def campaign() -> None:
    """Manage outreach campaigns (each campaign has its own pitch brief)."""


@campaign.command("create")
@click.argument("slug")
@click.option("--name", default=None, help="Display name (defaults to slug).")
def campaign_create(slug: str, name: str | None) -> None:
    """Scaffold campaigns/<slug>.md and insert the campaign row."""
    db.init_db()
    try:
        path = campaigns_mod.scaffold_brief(slug, name=name)
    except FileExistsError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    brief = campaigns_mod.load_brief(slug)
    db.upsert_campaign(slug=brief.slug, name=brief.name, brief_path=str(brief.path), target_icp=brief.target_icp)
    console.print(f"[green]✓[/green] created campaign [bold]{slug}[/bold] at {path}")
    console.print("[dim]Next: edit the brief, then `linkedin campaign sync` (or just `daily` which syncs first).[/dim]")


@campaign.command("sync")
def campaign_sync() -> None:
    """Re-read every campaigns/*.md file and upsert its frontmatter into the DB.
    The markdown files are the source of truth; the DB mirrors them."""
    db.init_db()
    files = campaigns_mod.list_brief_files()
    if not files:
        console.print("[yellow]no campaign files found in campaigns/[/yellow]")
        return
    synced = []
    for path in files:
        slug = path.stem
        try:
            brief = campaigns_mod.load_brief(slug)
        except Exception as e:
            console.print(f"[red]skipped {path}: {e}[/red]")
            continue
        cid = db.upsert_campaign(slug=brief.slug, name=brief.name, brief_path=str(brief.path), target_icp=brief.target_icp)
        if brief.status in db.VALID_CAMPAIGN_STATUSES:
            db.set_campaign_status(cid, brief.status)
        synced.append(brief.slug)
    console.print(f"[green]✓[/green] synced {len(synced)} campaign(s): {', '.join(synced)}")


@campaign.command("list")
def campaign_list() -> None:
    """Show all campaigns from the DB."""
    db.init_db()
    rows = db.list_campaigns()
    if not rows:
        console.print("[dim]no campaigns yet — try `linkedin campaign create <slug>`[/dim]")
        return
    t = Table()
    for col in ("id", "slug", "name", "status", "target_icp", "brief"):
        t.add_column(col)
    for r in rows:
        t.add_row(
            str(r["id"]),
            r["slug"],
            r["name"],
            r["status"],
            (r["target_icp"] or "—")[:60],
            r["brief_path"],
        )
    console.print(t)


@campaign.command("show")
@click.argument("slug")
def campaign_show(slug: str) -> None:
    """Print the full brief for a campaign."""
    db.init_db()
    try:
        brief = campaigns_mod.load_brief(slug)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    console.print(f"[bold]{brief.name}[/bold] [dim]({brief.slug}, {brief.status})[/dim]")
    if brief.target_icp:
        console.print(f"[cyan]ICP:[/cyan] {brief.target_icp}")
    console.print()
    console.print(brief.brief)


@campaign.command("archive")
@click.argument("slug")
def campaign_archive(slug: str) -> None:
    """Mark a campaign archived (existing prospects stay, no new work happens)."""
    db.init_db()
    row = db.get_campaign(slug)
    if not row:
        console.print(f"[red]no campaign {slug!r}[/red]")
        sys.exit(1)
    db.set_campaign_status(int(row["id"]), "archived")
    console.print(f"[green]✓[/green] archived {slug}")


@campaign.command("assign")
@click.argument("prospect_id", type=int)
@click.argument("slug")
def campaign_assign(prospect_id: int, slug: str) -> None:
    """Attach a prospect to a campaign."""
    db.init_db()
    row = db.get_campaign(slug)
    if not row:
        console.print(f"[red]no campaign {slug!r}[/red]")
        sys.exit(1)
    p = db.get_prospect(prospect_id)
    if not p:
        console.print(f"[red]no prospect {prospect_id}[/red]")
        sys.exit(1)
    db.upsert_prospect(linkedin_url=p["linkedin_url"], campaign_id=int(row["id"]))
    console.print(f"[green]✓[/green] prospect {prospect_id} → campaign {slug}")


# ------------------------------------------------------------------ telegram

@cli.command("telegram-test")
def telegram_test() -> None:
    """Send a test message via Telegram to confirm credentials work."""
    cfg = load_config()
    try:
        tg = TelegramClient(cfg)
    except TelegramError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    try:
        mid = tg.send_message(
            "🧪 *Test message* from `linkedin-agent telegram-test` — your Telegram setup works."
        )
        console.print(f"[green]✓[/green] sent (message_id={mid}); check Telegram.")
    finally:
        tg.close()


@cli.command("telegram-push-draft")
@click.argument("draft_id", type=int)
def telegram_push_draft(draft_id: int) -> None:
    """Push an existing pending_draft row to Telegram for approval.
    Used by the cron path; exposed here for manual testing."""
    cfg = load_config()
    db.init_db()
    draft = db.get_draft(draft_id)
    if not draft:
        console.print(f"[red]no draft {draft_id}[/red]")
        sys.exit(1)
    if draft["status"] != "pending":
        console.print(f"[yellow]draft is already {draft['status']!r}; pushing again anyway[/yellow]")
    prospect = db.get_prospect(draft["prospect_id"])
    campaign_name = None
    if prospect and prospect["campaign_id"]:
        campaign_row = db.get_campaign(int(prospect["campaign_id"]))
        campaign_name = campaign_row["name"] if campaign_row else None
    tg = TelegramClient(cfg)
    try:
        mid = tg.push_draft_for_approval(
            draft_id=draft_id,
            kind=draft["kind"],
            body=draft["body"],
            prospect_name=prospect["full_name"] if prospect else None,
            prospect_company=prospect["company"] if prospect else None,
            prospect_url=prospect["linkedin_url"] if prospect else None,
            campaign_name=campaign_name,
        )
        db.set_draft_telegram_id(draft_id, mid)
        console.print(f"[green]✓[/green] draft {draft_id} pushed → telegram_message_id={mid}")
    finally:
        tg.close()


@cli.command("_debug-enqueue")
@click.argument("prospect_id", type=int)
@click.argument("kind", type=click.Choice(["connect_note", "dm1", "dm2", "dm3"]))
@click.argument("body")
@click.option("--push/--no-push", default=True, help="Also push the draft to Telegram immediately.")
def debug_enqueue(prospect_id: int, kind: str, body: str, push: bool) -> None:
    """Test helper: enqueue a draft with hand-written text, optionally push to Telegram.
    Avoids invoking the drafter so you can iterate on the bot end-to-end without spending Claude calls."""
    cfg = load_config()
    db.init_db()
    p = db.get_prospect(prospect_id)
    if not p:
        console.print(f"[red]no prospect {prospect_id}[/red]")
        sys.exit(1)
    draft_id = db.enqueue_draft(prospect_id, kind, body)
    console.print(f"[green]✓[/green] draft #{draft_id} enqueued ({kind}, {len(body)} chars)")
    if push:
        campaign_name = None
        if p["campaign_id"]:
            campaign_row = db.get_campaign(int(p["campaign_id"]))
            campaign_name = campaign_row["name"] if campaign_row else None
        tg = TelegramClient(cfg)
        try:
            mid = tg.push_draft_for_approval(
                draft_id=draft_id,
                kind=kind,
                body=body,
                prospect_name=p["full_name"],
                prospect_company=p["company"],
                prospect_url=p["linkedin_url"],
                campaign_name=campaign_name,
            )
            db.set_draft_telegram_id(draft_id, mid)
            console.print(f"[green]✓[/green] pushed to telegram (message_id={mid})")
        finally:
            tg.close()


@cli.command()
@click.option("--prospect-id", type=int, default=None, help="Enrich just one prospect.")
@click.option("--all-stale", is_flag=True, help="Enrich all prospects whose enrichment is older than 7 days (or never).")
@click.option("--limit", default=None, type=int, help="Cap how many to enrich in one run.")
def enrich(prospect_id, all_stale, limit) -> None:
    """Fetch full LinkedIn profile + most-recent-post timestamp for prospects
    and persist the signal (network distance, mutual connections, premium status,
    follower count, etc.)."""
    from .enrichment import enrich as enrich_one, enrich_stale
    cfg = load_config()
    db.init_db()

    if prospect_id is not None:
        ok = enrich_one(cfg, prospect_id)
        console.print(f"[{'green' if ok else 'red'}]{'✓' if ok else '✗'}[/] prospect {prospect_id}")
        return

    if all_stale:
        result = enrich_stale(cfg, limit=limit)
        console.print(
            f"[green]✓[/green] enriched: [bold]{result.enriched}[/bold] · "
            f"failed: {result.failed} (e.g. locked profiles) · "
            f"errors: {len(result.errors)}"
        )
        return

    console.print("[yellow]nothing to do — pass --prospect-id <N> or --all-stale[/yellow]")


@cli.command()
@click.option("--limit", default=50, help="Max messages to fetch per poll.")
@click.option("--notify/--no-notify", default=True, help="Push Telegram notifications for new replies.")
def poll(limit: int, notify: bool) -> None:
    """Fetch recent messages from Unipile; record new inbound replies, halt
    follow-up sequences, and notify in Telegram."""
    from .poll import poll_once
    cfg = load_config()
    result = poll_once(cfg, limit=limit, notify=notify)
    console.print(
        f"[green]✓[/green] polled {result.fetched} messages · "
        f"new inbound: [bold]{result.new_inbound}[/bold] · "
        f"matched prospects: {result.matched_prospects} · "
        f"unknown senders skipped: {result.skipped_unknown_sender} · "
        f"notifications: {result.notifications_sent}"
    )


@cli.command()
@click.option("--no-telegram", is_flag=True, help="Skip pushing drafts to Telegram (DB only).")
def followup(no_telegram: bool) -> None:
    """Find prospects whose DM2/DM3 follow-up is due, draft, and enqueue.
    Auto-ghost prospects with no reply after DM3 + 14 days."""
    from .followup import run_followup_cycle
    from .drafter import draft as drafter_fn
    cfg = load_config()
    db.init_db()

    telegram = None
    if not no_telegram:
        try:
            telegram = TelegramClient(cfg)
        except TelegramError as e:
            console.print(f"[yellow]telegram disabled ({e}) — running without notifications[/yellow]")

    try:
        result = run_followup_cycle(cfg, drafter=drafter_fn, telegram=telegram)
        console.print(
            f"[green]✓[/green] followup cycle: "
            f"DM2 enqueued: [bold]{result.dm2_enqueued}[/bold] · "
            f"DM3 enqueued: [bold]{result.dm3_enqueued}[/bold] · "
            f"auto-ghosted: {result.ghosted} · "
            f"skipped (existing draft): {result.drafts_skipped_existing} · "
            f"drafter failures: {result.drafts_failed}"
        )
    finally:
        if telegram:
            telegram.close()


@cli.command()
@click.option("--no-telegram", is_flag=True, help="Skip pushing drafts / summary to Telegram.")
def daily(no_telegram: bool) -> None:
    """Run the full daily cycle: sync campaigns, poll inbound, react, draft
    connections + DMs + follow-ups, flush approved-but-not-sent. Cron entry."""
    from .daily import run_daily
    cfg = load_config()
    telegram = None
    if not no_telegram:
        try:
            telegram = TelegramClient(cfg)
        except TelegramError as e:
            console.print(f"[yellow]telegram disabled ({e}) — running quietly[/yellow]")
    try:
        result = run_daily(cfg, telegram=telegram, notify_summary=(telegram is not None))
        console.print(f"[green]✓[/green] daily run complete")
        console.print(result.summary())
    finally:
        if telegram:
            telegram.close()


@cli.command("send-approved")
@click.option("--force", is_flag=True, help="Send even outside the 9-5 Mon-Fri window.")
def send_approved(force: bool) -> None:
    """Flush any pending_drafts in status='approved' through Unipile.
    Respects the send window unless --force."""
    from .bot_daemon import send_draft_via_adapter
    from . import send_window
    cfg = load_config()
    db.init_db()

    if not force and not send_window.is_open():
        when = send_window.format_next_open()
        console.print(f"[yellow]send window closed — next opens {when}. Use --force to override.[/yellow]")
        sys.exit(0)

    drafts = db.list_pending_drafts(status="approved")
    if not drafts:
        console.print("[dim]no approved drafts waiting[/dim]")
        return

    adapter = get_adapter(cfg)
    sent = 0
    failed = 0
    try:
        for d in drafts:
            try:
                send_draft_via_adapter(cfg, adapter, d, source="send-approved")
                console.print(f"  [green]✓[/green] sent draft #{d['id']} ({d['kind']})")
                sent += 1
            except safety.RateLimitExceeded as e:
                console.print(f"  [yellow]⚠[/yellow] draft #{d['id']}: {e}")
                break
            except Exception as e:
                console.print(f"  [red]✗[/red] draft #{d['id']}: {e}")
                failed += 1
    finally:
        adapter.close()
    console.print(f"[green]✓[/green] flushed {sent} draft(s), {failed} failed")


@cli.command("bot-run")
def bot_run() -> None:
    """Run the Telegram bot daemon — long-polls for approval taps and edit replies.
    Blocks. Run in a tmux session or under launchd/systemd for production use."""
    from .bot_daemon import run as run_daemon
    run_daemon()


if __name__ == "__main__":
    cli()
