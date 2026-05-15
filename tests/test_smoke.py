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


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """Per-test isolated env: fake backend, temp DB, generous caps, no delays."""
    db_path = tmp_path / "smoke.db"
    return {
        "LINKEDIN_BACKEND": "fake",
        "LINKEDIN_DB_PATH": str(db_path),
        "DAILY_MAX_REACTIONS": "30",
        "DAILY_MAX_CONNECTIONS": "20",
        "DAILY_MAX_DMS": "10",
        "DAILY_MAX_SEARCHES": "50",
        "ACTION_DELAY_MIN": "0",
        "ACTION_DELAY_MAX": "0",
        "DRY_RUN": "0",
    }


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

    rows = _db_query(env["LINKEDIN_DB_PATH"], "SELECT status FROM prospects WHERE id=1")
    assert rows[0]["status"] == "dm_sent"

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
