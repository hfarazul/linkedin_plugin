"""End-to-end smoke test for the linkedin_agent CLI.

Runs every subcommand against the offline `fake` backend and a temp SQLite DB.
No network traffic, no LinkedIn calls. Verifies:
- DB schema initializes
- search imports prospects and logs an action
- pipeline reports them
- posts returns canned data
- react in dry-run logs the action but does not transition status
- react with dry-run off transitions status and respects caps
- connect / dm transition status and log actions
- caps reflects usage and blocks once exceeded
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run(args: list[str], env: dict[str, str], *, expect_fail: bool = False) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **env}
    result = subprocess.run(
        [sys.executable, "-m", "linkedin_agent", *args],
        cwd=ROOT,
        env=full_env,
        capture_output=True,
        text=True,
    )
    if expect_fail:
        assert result.returncode != 0, (
            f"expected failure but got rc=0\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    else:
        assert result.returncode == 0, (
            f"command {args} failed rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


# env fixture lives in tests/conftest.py — shared across test files.


def _db_query(db_path: str, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()


# --------------------------------------------------------------------------- init

def test_init_creates_db(env: dict[str, str]) -> None:
    _run(["init"], env)
    rows = _db_query(env["LINKEDIN_DB_PATH"], "SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r["name"] for r in rows}
    assert {"prospects", "actions", "messages"}.issubset(tables)


# --------------------------------------------------------------------------- search

def test_search_imports_prospects(env: dict[str, str]) -> None:
    _run(["init"], env)
    result = _run(["search", "fintech founder", "--limit", "3"], env)
    assert "imported 3 prospects" in result.stdout

    rows = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM prospects ORDER BY id")
    assert len(rows) == 3
    assert all(r["status"] == "targeted" for r in rows)
    assert all("fake-" in r["linkedin_url"] for r in rows)

    actions = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM actions WHERE kind='search'")
    assert len(actions) == 3  # one per prospect imported
    assert json.loads(actions[0]["payload"])["query"] == "fintech founder"


# --------------------------------------------------------------------------- pipeline

def test_pipeline_lists_prospects(env: dict[str, str]) -> None:
    _run(["init"], env)
    _run(["search", "ai engineer", "--limit", "2"], env)
    result = _run(["pipeline"], env)
    assert "Fake Person 1" in result.stdout
    assert "targeted" in result.stdout

    filtered = _run(["pipeline", "--status", "targeted"], result.stdout and env)
    assert "Fake Person 1" in filtered.stdout

    none = _run(["pipeline", "--status", "replied"], env)
    assert "no prospects" in none.stdout


# --------------------------------------------------------------------------- posts

def test_posts_returns_canned_data(env: dict[str, str]) -> None:
    _run(["init"], env)
    _run(["search", "designer", "--limit", "1"], env)
    result = _run(["posts", "1", "--limit", "2"], env)
    assert "Sample post 1" in result.stdout
    assert "urn:li:activity" in result.stdout


# --------------------------------------------------------------------------- react

def test_react_live_transitions_status_and_logs(env: dict[str, str]) -> None:
    _run(["init"], env)
    _run(["search", "founder", "--limit", "1"], env)
    result = _run(["react", "1"], env)
    assert "reacted on" in result.stdout

    rows = _db_query(env["LINKEDIN_DB_PATH"], "SELECT status FROM prospects WHERE id=1")
    assert rows[0]["status"] == "reacted"

    actions = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM actions WHERE kind='react'")
    assert len(actions) == 1
    assert actions[0]["dry_run"] == 0


def test_react_dry_run_logs_but_does_not_transition(env: dict[str, str]) -> None:
    env = {**env, "DRY_RUN": "1"}
    _run(["init"], env)
    _run(["search", "founder", "--limit", "1"], env)
    result = _run(["react", "1"], env)
    assert "(dry-run) would react" in result.stdout

    rows = _db_query(env["LINKEDIN_DB_PATH"], "SELECT status FROM prospects WHERE id=1")
    assert rows[0]["status"] == "targeted"  # untouched

    actions = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM actions WHERE kind='react'")
    assert len(actions) == 1
    assert actions[0]["dry_run"] == 1


# --------------------------------------------------------------------------- connect

def test_connect_transitions_status_and_records_note(env: dict[str, str]) -> None:
    _run(["init"], env)
    _run(["search", "pm", "--limit", "1"], env)
    note = "Loved your post on B2B rails — would value swapping notes."
    result = _run(["connect", "1", "--note", note], env)
    assert "connection request sent" in result.stdout

    rows = _db_query(env["LINKEDIN_DB_PATH"], "SELECT status FROM prospects WHERE id=1")
    assert rows[0]["status"] == "connection_sent"

    actions = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM actions WHERE kind='connect'")
    assert len(actions) == 1
    payload = json.loads(actions[0]["payload"])
    assert payload["note"] == note


# --------------------------------------------------------------------------- dm

def test_dm_transitions_status_and_records_message(env: dict[str, str]) -> None:
    _run(["init"], env)
    _run(["search", "vp eng", "--limit", "1"], env)
    body = "Thanks for connecting! Saw your post on platform teams — curious how you measure throughput."
    result = _run(["dm", "1", body], env)
    assert "DM sent to" in result.stdout

    rows = _db_query(env["LINKEDIN_DB_PATH"],
                     "SELECT status, dm_count, last_dm_at FROM prospects WHERE id=1")
    assert rows[0]["status"] == "dm_sent"
    # Regression guard: CLI dm must bump dm_count + last_dm_at so the
    # follow-up scheduler can find this prospect for DM2.
    assert rows[0]["dm_count"] == 1
    assert rows[0]["last_dm_at"] is not None

    msgs = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM messages WHERE prospect_id=1")
    assert len(msgs) == 1
    assert msgs[0]["body"] == body
    assert msgs[0]["direction"] == "outbound"


# --------------------------------------------------------------------------- caps

def test_caps_reflects_usage(env: dict[str, str]) -> None:
    _run(["init"], env)
    _run(["search", "founder", "--limit", "2"], env)
    _run(["react", "1"], env)
    _run(["react", "2"], env)
    result = _run(["caps"], env)
    # Both reactions should appear; search count should be ≥ 2 (one log per imported prospect).
    assert "react" in result.stdout
    assert "search" in result.stdout
    # 2/30 reactions used
    assert "2" in result.stdout and "30" in result.stdout


def test_cooldowns_lists_and_revives(env: dict[str, str]) -> None:
    """Cooldowns CLI lists prospects with a cooldown_until set; --revive-expired
    flips the cleared ones back to 'reacted' so daily can re-draft them."""
    from datetime import datetime, timedelta, timezone
    _run(["init"], env)
    _run(["search", "founder", "--limit", "2"], env)
    # Two prospects exist now (ids 1, 2). Put one in active cooldown, one expired.
    import sqlite3
    future = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn = sqlite3.connect(env["LINKEDIN_DB_PATH"])
    conn.execute("UPDATE prospects SET cooldown_until = ?, status = 'connection_sent' WHERE id = 1", (future,))
    conn.execute("UPDATE prospects SET cooldown_until = ?, status = 'connection_sent' WHERE id = 2", (past,))
    conn.commit()
    conn.close()

    # Listing shows both
    result = _run(["cooldowns"], env)
    assert "Fake Person 1" in result.stdout
    assert "Fake Person 2" in result.stdout
    assert "expired" in result.stdout  # for prospect 2

    # Revive-expired: only prospect 2 flips back; prospect 1 still in cooldown
    result = _run(["cooldowns", "--revive-expired"], env)
    assert "revived 1 prospect" in result.stdout

    rows = _db_query(env["LINKEDIN_DB_PATH"],
                     "SELECT id, status, cooldown_until FROM prospects ORDER BY id")
    by_id = {r["id"]: r for r in rows}
    assert by_id[1]["status"] == "connection_sent"          # untouched (future cooldown)
    assert by_id[1]["cooldown_until"] is not None
    assert by_id[2]["status"] == "reacted"                  # revived
    assert by_id[2]["cooldown_until"] is None


def test_caps_blocks_when_exceeded(env: dict[str, str]) -> None:
    env = {**env, "DAILY_MAX_REACTIONS": "1"}
    _run(["init"], env)
    _run(["search", "founder", "--limit", "2"], env)
    _run(["react", "1"], env)  # uses the one allowed reaction
    result = _run(["react", "2"], env, expect_fail=True)
    combined = result.stdout + result.stderr
    assert "daily cap" in combined.lower() or "RateLimitExceeded" in combined


# --------------------------------------------------------------------------- full playbook

def test_full_playbook_walks_pipeline(env: dict[str, str]) -> None:
    """End-to-end: search → react → connect → dm. Verify every status hop."""
    _run(["init"], env)
    _run(["search", "fintech founder", "--limit", "1"], env)

    def status() -> str:
        return _db_query(env["LINKEDIN_DB_PATH"], "SELECT status FROM prospects WHERE id=1")[0]["status"]

    assert status() == "targeted"
    _run(["react", "1"], env)
    assert status() == "reacted"
    _run(["connect", "1", "--note", "Saw your post on rails — would love to compare notes."], env)
    assert status() == "connection_sent"
    _run(["dm", "1", "Thanks for connecting — quick question on your B2B rails setup."], env)
    assert status() == "dm_sent"

    # Action log should record exactly one entry per real action (search logs one per imported prospect).
    counts = _db_query(
        env["LINKEDIN_DB_PATH"],
        "SELECT kind, COUNT(*) c FROM actions WHERE dry_run=0 GROUP BY kind",
    )
    by_kind = {r["kind"]: r["c"] for r in counts}
    assert by_kind == {"search": 1, "react": 1, "connect": 1, "dm": 1}


# --------------------------------------------------------------------------- campaigns


def test_campaign_create_list_show(env: dict[str, str]) -> None:
    """campaign create scaffolds the markdown file + DB row; list/show display it."""
    import time
    _run(["init"], env)

    # campaign create uses the project's campaigns/ directory; the brief is a
    # side effect on disk. Use a short, unique slug so it fits in Rich's table.
    slug = f"t{int(time.time()*1000) % 100000}"
    try:
        _run(["campaign", "create", slug, "--name", "Test Campaign"], env)

        result = _run(["campaign", "list"], env)
        # Slug may be truncated in table display — assert on name and full slug
        # in raw stdout (Rich wraps but doesn't truncate short slugs).
        assert "Test Campaign" in result.stdout
        assert slug in result.stdout

        result = _run(["campaign", "show", slug], env)
        assert "Test Campaign" in result.stdout
        assert "Pitch" in result.stdout
    finally:
        from linkedin_agent import campaigns as campaigns_mod
        p = campaigns_mod.brief_path_for(slug)
        if p.exists():
            p.unlink()


def test_campaign_sync_picks_up_status_changes(env: dict[str, str]) -> None:
    """Editing the markdown frontmatter and running `sync` updates the DB row."""
    import time
    _run(["init"], env)
    slug = f"s{int(time.time()*1000) % 100000}"
    try:
        _run(["campaign", "create", slug], env)

        # Manually edit the brief to mark it paused
        from linkedin_agent import campaigns as campaigns_mod
        path = campaigns_mod.brief_path_for(slug)
        content = path.read_text()
        path.write_text(content.replace("status: active", "status: paused"))

        _run(["campaign", "sync"], env)

        rows = _db_query(env["LINKEDIN_DB_PATH"], "SELECT status FROM campaigns WHERE slug = ?", (slug,))
        assert rows[0]["status"] == "paused"
    finally:
        from linkedin_agent import campaigns as campaigns_mod
        p = campaigns_mod.brief_path_for(slug)
        if p.exists():
            p.unlink()


def test_search_with_campaign_attaches_prospects(env: dict[str, str]) -> None:
    """`search --campaign <slug>` sets campaign_id on imported prospects."""
    import time
    _run(["init"], env)
    slug = f"a{int(time.time()*1000) % 100000}"
    try:
        _run(["campaign", "create", slug], env)
        _run(["search", "test query", "--limit", "2", "--campaign", slug], env)
        rows = _db_query(
            env["LINKEDIN_DB_PATH"],
            "SELECT p.id, p.full_name FROM prospects p JOIN campaigns c ON p.campaign_id = c.id WHERE c.slug = ?",
            (slug,),
        )
        assert len(rows) == 2
    finally:
        from linkedin_agent import campaigns as campaigns_mod
        p = campaigns_mod.brief_path_for(slug)
        if p.exists():
            p.unlink()


def test_search_posts_imports_authors_with_pitch_context(env: dict[str, str]) -> None:
    """search-posts imports the post AUTHOR as the prospect and stashes the
    post text as pitch_context — so the drafter can reference what they wrote.
    """
    import time
    _run(["init"], env)
    slug = f"sp{int(time.time()*1000) % 100000}"
    try:
        _run(["campaign", "create", slug], env)
        _run([
            "search-posts", "looking for technical co-founder",
            "--limit", "3", "--campaign", slug,
            "--date-posted", "past_month",
            "--author-keywords", "founder",
        ], env)
        rows = _db_query(
            env["LINKEDIN_DB_PATH"],
            """SELECT p.id, p.full_name, p.pitch_context
               FROM prospects p JOIN campaigns c ON p.campaign_id = c.id
               WHERE c.slug = ?""",
            (slug,),
        )
        assert len(rows) == 3, f"expected 3 imports, got {len(rows)}"
        # Every imported prospect has pitch_context populated with the post text
        for r in rows:
            assert r["pitch_context"], f"prospect {r['id']} missing pitch_context"
            assert "non-tech founder" in r["pitch_context"].lower(), (
                f"pitch_context for {r['id']} doesn't look like the post: {r['pitch_context']!r}"
            )
    finally:
        from linkedin_agent import campaigns as campaigns_mod
        p = campaigns_mod.brief_path_for(slug)
        if p.exists():
            p.unlink()


# --------------------------------------------------------------------------- draft state machine


def test_debug_enqueue_creates_pending_draft(env: dict[str, str]) -> None:
    """_debug-enqueue inserts a pending_drafts row without invoking the drafter."""
    _run(["init"], env)
    _run(["search", "q", "--limit", "1"], env)
    _run(["_debug-enqueue", "1", "dm1", "hand-written body", "--no-push"], env)

    rows = _db_query(env["LINKEDIN_DB_PATH"], "SELECT kind, status, body FROM pending_drafts")
    assert len(rows) == 1
    assert rows[0]["kind"] == "dm1"
    assert rows[0]["status"] == "pending"
    assert rows[0]["body"] == "hand-written body"


def test_send_approved_completes_draft_lifecycle(env: dict[str, str]) -> None:
    """Enqueue → mark approved → send-approved (window forced open) → status='sent'."""
    env = {**env, "LINKEDIN_FAKE_WINDOW": "open"}
    _run(["init"], env)
    _run(["search", "q", "--limit", "1"], env)
    _run(["_debug-enqueue", "1", "dm1", "test message body", "--no-push"], env)

    # Move draft to approved (the daemon would normally do this on tap)
    import sqlite3
    conn = sqlite3.connect(env["LINKEDIN_DB_PATH"])
    conn.execute("UPDATE pending_drafts SET status='approved' WHERE id=1")
    conn.commit()
    conn.close()

    _run(["send-approved"], env)

    rows = _db_query(env["LINKEDIN_DB_PATH"], "SELECT status FROM pending_drafts WHERE id=1")
    assert rows[0]["status"] == "sent"

    # Prospect should be marked dm_sent now
    rows = _db_query(env["LINKEDIN_DB_PATH"], "SELECT status, dm_count FROM prospects WHERE id=1")
    assert rows[0]["status"] == "dm_sent"
    assert rows[0]["dm_count"] == 1


# --------------------------------------------------------------------------- status


def test_status_renders_without_error(env: dict[str, str]) -> None:
    _run(["init"], env)
    _run(["search", "q", "--limit", "2"], env)
    result = _run(["status"], env)
    assert "Caps today" in result.stdout
    assert "targeted" in result.stdout


# --------------------------------------------------------------------------- followup


def test_followup_no_candidates_is_a_noop(env: dict[str, str]) -> None:
    """No prospects in dm_sent → followup runs cleanly, drafts nothing."""
    _run(["init"], env)
    result = _run(["followup", "--no-telegram"], env)
    assert "DM2 enqueued: 0" in result.stdout
    assert "DM3 enqueued: 0" in result.stdout
