"""Integration tests for the `hiring-import` CLI command.

Mirrors `test_funding_import_cli.py` — same test patterns, same `env` fixture,
same FakeAdapter knobs. The hiring-import command reuses funding_lookup
internals (find_founder, check_team), so most coverage carries over; these
tests just verify the command-level wiring + the new pitch_context shape.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------- helpers --------------------------------------

def _run(args: list[str], env: dict[str, str], *,
         expect_fail: bool = False) -> subprocess.CompletedProcess:
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


def _db_query(db_path: str, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()


def _db_exec(db_path: str, sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# ----------------------------- tests ----------------------------------------

def test_cli_imports_clean_match(env: dict[str, str]) -> None:
    """Happy path: company name → high-score match → prospect inserted with
    hiring-flavored pitch_context."""
    _run(["init"], env)
    result = _run([
        "hiring-import",
        "--company", "Acme AI",
        "--role", "first engineer",
        "--posted", "today",
        "--description", "AI-native accounting for SMBs",
    ], env)

    assert "Imported prospect" in result.stdout
    assert "hiring-first-engineer" in result.stdout

    prospects = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM prospects")
    assert len(prospects) == 1
    p = prospects[0]
    assert p["status"] == "targeted"
    assert "Hiring first engineer (posted today)" in p["pitch_context"]
    assert "Building Acme AI — AI-native accounting for SMBs" in p["pitch_context"]
    assert p["campaign_id"] is not None


def test_cli_auto_syncs_missing_campaign(env: dict[str, str]) -> None:
    """The hiring-first-engineer campaign should be loaded from markdown if
    absent from DB."""
    _run(["init"], env)
    rows = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM campaigns")
    assert len(rows) == 0

    result = _run(["hiring-import", "--company", "Acme AI"], env)
    assert "auto-synced campaign" in result.stdout

    rows = _db_query(env["LINKEDIN_DB_PATH"],
                     "SELECT slug FROM campaigns WHERE slug = 'hiring-first-engineer'")
    assert len(rows) == 1


def test_cli_skips_on_no_match(env: dict[str, str]) -> None:
    """find_founder returns nothing → exit 1, no prospect, action logged."""
    _run(["init"], env)
    env_no_hits = {**env, "LINKEDIN_FAKE_EMPTY_SEARCH": "1"}
    result = _run(
        ["hiring-import", "--company", "Nonexistent Co"],
        env_no_hits, expect_fail=True,
    )
    assert "No candidates found" in result.stdout

    prospects = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM prospects")
    assert len(prospects) == 0

    actions = _db_query(env["LINKEDIN_DB_PATH"],
                        "SELECT * FROM actions WHERE kind = 'hiring-import'")
    assert len(actions) == 1
    payload = json.loads(actions[0]["payload"])
    assert payload["result"] == "skipped_no_match"
    assert payload["company"] == "Nonexistent Co"
    assert actions[0]["prospect_id"] is None


def test_cli_skips_below_threshold(env: dict[str, str]) -> None:
    """Top candidate scores < 20 → exit 1 with diagnostic, log skipped_no_match."""
    _run(["init"], env)
    env_weak = {**env, "LINKEDIN_FAKE_HEADLINE": "Founder of an unrelated venture"}
    result = _run(
        ["hiring-import", "--company", "Acme AI"],
        env_weak, expect_fail=True,
    )
    assert "No confident founder match" in result.stdout
    assert "threshold 20" in result.stdout

    prospects = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM prospects")
    assert len(prospects) == 0


def test_cli_skips_on_cross_campaign_dedup(env: dict[str, str]) -> None:
    """Founder already in a DIFFERENT campaign → warn and skip."""
    _run(["init"], env)
    db_path = env["LINKEDIN_DB_PATH"]
    other_url = 'https://www.linkedin.com/in/fake-1-"acme-ai"-founder'
    _db_exec(
        db_path,
        """INSERT INTO campaigns (slug, name, brief_path, target_icp, status, created_at)
           VALUES ('other-camp', 'Other Campaign', 'campaigns/other.md', '...', 'active',
                   datetime('now'))""",
    )
    _db_exec(
        db_path,
        """INSERT INTO prospects
           (linkedin_url, full_name, status, campaign_id, first_seen_at)
           VALUES (?, 'Fake Person 1', 'connection_sent',
                   (SELECT id FROM campaigns WHERE slug='other-camp'),
                   datetime('now'))""",
        (other_url,),
    )

    result = _run(["hiring-import", "--company", "Acme AI"], env)
    assert "already prospect" in result.stdout
    assert "other-camp" in result.stdout

    prospects = _db_query(db_path, "SELECT * FROM prospects")
    assert len(prospects) == 1

    actions = _db_query(db_path, "SELECT * FROM actions WHERE kind = 'hiring-import'")
    assert len(actions) == 1
    payload = json.loads(actions[0]["payload"])
    assert payload["result"] == "skipped_dedup"


def test_cli_same_campaign_reimport_overwrites_pitch_context(env: dict[str, str]) -> None:
    """Re-running hiring-import on same campaign refreshes pitch_context;
    status untouched."""
    _run(["init"], env)
    db_path = env["LINKEDIN_DB_PATH"]

    _run([
        "hiring-import", "--company", "Acme AI",
        "--role", "first engineer", "--posted", "yesterday",
    ], env)
    first = _db_query(db_path, "SELECT id, status, pitch_context FROM prospects")[0]
    first_id = first["id"]
    assert "first engineer" in first["pitch_context"]
    assert "yesterday" in first["pitch_context"]

    # Move them forward in the pipeline manually
    _db_exec(db_path, "UPDATE prospects SET status = 'connection_sent' WHERE id = ?",
             (first_id,))

    # Re-import with a fresher signal
    _run([
        "hiring-import", "--company", "Acme AI",
        "--role", "senior engineer", "--posted", "today",
    ], env)

    second = _db_query(db_path, "SELECT id, status, pitch_context FROM prospects")[0]
    assert second["id"] == first_id                  # same row
    assert second["status"] == "connection_sent"     # status preserved
    assert "senior engineer" in second["pitch_context"]
    assert "today" in second["pitch_context"]
    assert "yesterday" not in second["pitch_context"]


def test_cli_dry_run_no_writes(env: dict[str, str]) -> None:
    """--dry-run prints match + would-be pitch_context, writes nothing."""
    _run(["init"], env)
    result = _run([
        "hiring-import", "--company", "Acme AI",
        "--role", "first engineer", "--dry-run",
    ], env)
    assert "Match (score" in result.stdout
    assert "(dry-run)" in result.stdout

    prospects = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM prospects")
    assert len(prospects) == 0
    actions = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM actions")
    assert len(actions) == 0


def test_cli_optional_fields_omitted(env: dict[str, str]) -> None:
    """Just --company → pitch_context falls back to 'Hiring engineering. Building X.'"""
    _run(["init"], env)
    _run(["hiring-import", "--company", "Acme AI"], env)

    rows = _db_query(env["LINKEDIN_DB_PATH"], "SELECT pitch_context FROM prospects")
    assert len(rows) == 1
    assert rows[0]["pitch_context"] == "Hiring engineering. Building Acme AI."


def test_cli_logs_action_with_structured_payload(env: dict[str, str]) -> None:
    """The 'hiring-import' action row carries the full structured payload."""
    _run(["init"], env)
    _run([
        "hiring-import",
        "--company", "Acme AI",
        "--role", "first engineer",
        "--posted", "today",
        "--description", "AI thing",
        "--source-url", "https://example.com/post/123",
    ], env)

    actions = _db_query(env["LINKEDIN_DB_PATH"],
                        "SELECT * FROM actions WHERE kind = 'hiring-import'")
    assert len(actions) == 1
    payload = json.loads(actions[0]["payload"])
    assert payload == {
        "company": "Acme AI",
        "role": "first engineer",
        "posted": "today",
        "description": "AI thing",
        "source_url": "https://example.com/post/123",
        "campaign": "hiring-first-engineer",
        "match_score": payload["match_score"],
        "match_signals": payload["match_signals"],
        "team_check": payload["team_check"],
        "result": "imported",
    }
    assert isinstance(payload["match_score"], int)
    assert payload["match_score"] >= 20
    assert payload["team_check"]["cto_found"] is False
    assert any("attributed" in s for s in payload["match_signals"])


def test_cli_skips_when_team_check_finds_cto(env: dict[str, str]) -> None:
    """Disable the test-mode empty-team-check shortcut so FakeAdapter's
    query-echo headlines (which contain 'CTO' when the query does) trigger
    the disqualification path. Hiring-import shares the same team_check as
    funding-import, so the same logic applies."""
    env_team = {k: v for k, v in env.items() if k != "LINKEDIN_FAKE_EMPTY_TEAM_CHECK"}
    _run(["init"], env_team)
    result = _run(
        ["hiring-import", "--company", "Acme AI"],
        env_team, expect_fail=True,
    )
    assert "ICP miss" in result.stdout
    assert "CTO" in result.stdout

    prospects = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM prospects")
    assert len(prospects) == 0

    actions = _db_query(env["LINKEDIN_DB_PATH"],
                        "SELECT * FROM actions WHERE kind = 'hiring-import'")
    assert len(actions) == 1
    payload = json.loads(actions[0]["payload"])
    assert payload["result"] == "skipped_has_eng_team"
    assert payload["team_check"]["cto_found"] is True


def test_cli_respects_search_cap(env: dict[str, str]) -> None:
    """If the search cap is exhausted, hiring-import exits with a cap error."""
    env_capped = {**env, "DAILY_MAX_SEARCHES": "1"}
    _run(["init"], env_capped)
    _run(["search", "founder", "--limit", "1"], env_capped)
    result = _run(
        ["hiring-import", "--company", "Acme AI"],
        env_capped, expect_fail=True,
    )
    assert "daily cap reached" in result.stdout.lower() or "cap" in result.stdout.lower()
