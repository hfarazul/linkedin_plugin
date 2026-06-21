"""Integration tests for the `funding-import` CLI command.

Each test runs the CLI as a subprocess against the offline fake adapter and
a temp SQLite DB. We exercise: clean match, no-match, cross-campaign dedup,
dry-run, optional fields, and the structured action log.
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
    """Happy path: company name produces a high-scoring match → prospect
    inserted into the campaign with pitch_context populated."""
    _run(["init"], env)
    result = _run([
        "funding-import",
        "--company", "Acme AI",
        "--round", "seed",
        "--amount", "$2.5M",
        "--investors", "Sequoia, a16z",
        "--description", "AI agent for SMB accounting",
    ], env)

    assert "Imported prospect" in result.stdout
    assert "recently-funded-non-tech" in result.stdout

    prospects = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM prospects")
    assert len(prospects) == 1
    p = prospects[0]
    assert p["status"] == "targeted"
    assert "Recently closed seed $2.5M from Sequoia, a16z" in p["pitch_context"]
    assert "Building Acme AI — AI agent for SMB accounting" in p["pitch_context"]
    assert p["campaign_id"] is not None


def test_cli_auto_syncs_missing_campaign(env: dict[str, str]) -> None:
    """The campaign should be loaded from campaigns/<slug>.md if absent from DB."""
    _run(["init"], env)
    # Verify no campaign in DB pre-run
    rows = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM campaigns")
    assert len(rows) == 0

    result = _run(["funding-import", "--company", "Acme AI"], env)
    assert "auto-synced campaign" in result.stdout

    rows = _db_query(env["LINKEDIN_DB_PATH"],
                     "SELECT slug FROM campaigns WHERE slug = 'recently-funded-non-tech'")
    assert len(rows) == 1


def test_cli_skips_on_no_match(env: dict[str, str]) -> None:
    """When find_founder returns nothing, exit 1, no prospect row, but an
    actions row of result=skipped_no_match is logged."""
    _run(["init"], env)
    env_no_hits = {**env, "LINKEDIN_FAKE_EMPTY_SEARCH": "1"}
    result = _run(
        ["funding-import", "--company", "Nonexistent Co"],
        env_no_hits,
        expect_fail=True,
    )
    assert "No candidates found" in result.stdout

    prospects = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM prospects")
    assert len(prospects) == 0

    actions = _db_query(env["LINKEDIN_DB_PATH"],
                        "SELECT * FROM actions WHERE kind = 'funding-import'")
    assert len(actions) == 1
    payload = json.loads(actions[0]["payload"])
    assert payload["result"] == "skipped_no_match"
    assert payload["company"] == "Nonexistent Co"
    assert actions[0]["prospect_id"] is None


def test_cli_skips_below_threshold(env: dict[str, str]) -> None:
    """When the top candidate scores below 20, exit 1 with 'top score X'
    diagnostic and log skipped_no_match."""
    _run(["init"], env)
    # FakeAdapter will return headlines without "founder"/"ceo" — every
    # candidate rejected, find_founder returns None, CLI hits the no-candidates
    # branch. To exercise the below-threshold branch we use a headline that
    # has founder keyword but lacks the company name.
    env_weak = {**env, "LINKEDIN_FAKE_HEADLINE": "Founder of an unrelated venture"}
    result = _run(
        ["funding-import", "--company", "Acme AI"],
        env_weak,
        expect_fail=True,
    )
    assert "No confident founder match" in result.stdout
    assert "threshold 20" in result.stdout

    prospects = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM prospects")
    assert len(prospects) == 0


def test_cli_skips_on_cross_campaign_dedup(env: dict[str, str]) -> None:
    """If the founder is already a prospect under a DIFFERENT campaign,
    warn and skip — preserve cross-campaign attribution."""
    _run(["init"], env)
    db_path = env["LINKEDIN_DB_PATH"]

    # Create a different campaign and pre-seed the prospect into it.
    # The fake adapter will produce linkedin_url ending in
    # "fake-1-\"acme-ai\"-founder" for the query '"Acme AI" founder'.
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

    result = _run(["funding-import", "--company", "Acme AI"], env)
    assert "already prospect" in result.stdout
    assert "other-camp" in result.stdout
    assert "Skipped" in result.stdout

    # Prospect count unchanged (still just the pre-seeded one)
    prospects = _db_query(db_path, "SELECT * FROM prospects")
    assert len(prospects) == 1
    assert prospects[0]["campaign_id"] == _db_query(
        db_path, "SELECT id FROM campaigns WHERE slug='other-camp'"
    )[0]["id"]

    # Action logged with skipped_dedup result + the existing prospect's id
    actions = _db_query(db_path, "SELECT * FROM actions WHERE kind = 'funding-import'")
    assert len(actions) == 1
    payload = json.loads(actions[0]["payload"])
    assert payload["result"] == "skipped_dedup"
    assert actions[0]["prospect_id"] == prospects[0]["id"]


def test_cli_same_campaign_reimport_overwrites_pitch_context(env: dict[str, str]) -> None:
    """Re-running funding-import on a prospect already in the target campaign
    overwrites pitch_context (refresh stale funding details). Status untouched."""
    _run(["init"], env)
    db_path = env["LINKEDIN_DB_PATH"]

    # First import sets pitch_context one way.
    _run([
        "funding-import",
        "--company", "Acme AI",
        "--round", "seed",
        "--amount", "$1M",
    ], env)

    first = _db_query(db_path, "SELECT id, status, pitch_context FROM prospects")[0]
    first_id = first["id"]
    assert "$1M" in first["pitch_context"]

    # Manually nudge status to simulate progress through the pipeline.
    _db_exec(db_path, "UPDATE prospects SET status = 'connection_sent' WHERE id = ?",
             (first_id,))

    # Re-import with fresher details.
    _run([
        "funding-import",
        "--company", "Acme AI",
        "--round", "seed",
        "--amount", "$2.5M",
        "--investors", "Sequoia",
    ], env)

    second = _db_query(db_path, "SELECT id, status, pitch_context FROM prospects")[0]
    assert second["id"] == first_id          # same row
    assert second["status"] == "connection_sent"  # status preserved
    assert "$2.5M" in second["pitch_context"]     # new context wins
    assert "Sequoia" in second["pitch_context"]
    assert "$1M" not in second["pitch_context"]   # old context gone


def test_cli_dry_run_no_writes(env: dict[str, str]) -> None:
    """--dry-run prints the match + would-be pitch_context, writes nothing."""
    _run(["init"], env)
    result = _run([
        "funding-import",
        "--company", "Acme AI",
        "--round", "seed",
        "--dry-run",
    ], env)

    assert "Match (score" in result.stdout
    assert "(dry-run)" in result.stdout

    prospects = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM prospects")
    assert len(prospects) == 0
    actions = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM actions")
    assert len(actions) == 0


def test_cli_optional_fields_omitted(env: dict[str, str]) -> None:
    """Just --company → pitch_context falls back to 'Recently funded. Building X.'"""
    _run(["init"], env)
    _run(["funding-import", "--company", "Acme AI"], env)

    rows = _db_query(env["LINKEDIN_DB_PATH"], "SELECT pitch_context FROM prospects")
    assert len(rows) == 1
    assert rows[0]["pitch_context"] == "Recently funded. Building Acme AI."


def test_cli_logs_action_with_structured_payload(env: dict[str, str]) -> None:
    """The 'funding-import' action row carries the structured payload —
    company, round, amount, investors, description, campaign, match info."""
    _run(["init"], env)
    _run([
        "funding-import",
        "--company", "Acme AI",
        "--round", "seed",
        "--amount", "$2.5M",
        "--investors", "Sequoia",
        "--description", "AI thing",
    ], env)

    actions = _db_query(env["LINKEDIN_DB_PATH"],
                        "SELECT * FROM actions WHERE kind = 'funding-import'")
    assert len(actions) == 1
    payload = json.loads(actions[0]["payload"])
    assert payload == {
        "company": "Acme AI",
        "round": "seed",
        "amount": "$2.5M",
        "investors": "Sequoia",
        "description": "AI thing",
        "campaign": "recently-funded-non-tech",
        "match_score": payload["match_score"],  # any int — verified separately
        "match_signals": payload["match_signals"],
        "team_check": payload["team_check"],
        "result": "imported",
    }
    assert payload["team_check"]["cto_found"] is False
    assert isinstance(payload["match_score"], int)
    assert payload["match_score"] >= 20
    assert any("attributed" in s for s in payload["match_signals"])


def test_cli_skips_when_team_check_finds_cto(env: dict[str, str]) -> None:
    """When the team check surfaces a current CTO at the company, the import
    is skipped with result=skipped_has_eng_team and no prospect row created.

    We disable the test-mode empty-team-check shortcut so FakeAdapter's
    query-echo headlines (which contain 'CTO' when the query does) flow
    through and trigger the disqualification path naturally.
    """
    env_team = {k: v for k, v in env.items() if k != "LINKEDIN_FAKE_EMPTY_TEAM_CHECK"}
    _run(["init"], env_team)
    result = _run(
        ["funding-import", "--company", "Acme AI"],
        env_team,
        expect_fail=True,
    )
    assert "ICP miss" in result.stdout
    assert "CTO" in result.stdout

    prospects = _db_query(env["LINKEDIN_DB_PATH"], "SELECT * FROM prospects")
    assert len(prospects) == 0

    actions = _db_query(env["LINKEDIN_DB_PATH"],
                        "SELECT * FROM actions WHERE kind = 'funding-import'")
    assert len(actions) == 1
    payload = json.loads(actions[0]["payload"])
    assert payload["result"] == "skipped_has_eng_team"
    assert payload["team_check"]["cto_found"] is True


def test_cli_respects_search_cap(env: dict[str, str]) -> None:
    """If the search cap is exhausted, funding-import exits with a cap error
    (same pattern as `linkedin search`)."""
    env_capped = {**env, "DAILY_MAX_SEARCHES": "1"}
    _run(["init"], env_capped)

    # Burn the single search slot with a regular search.
    _run(["search", "founder", "--limit", "1"], env_capped)

    # Now funding-import should hit the cap.
    result = _run(
        ["funding-import", "--company", "Acme AI"],
        env_capped,
        expect_fail=True,
    )
    assert "daily cap reached" in result.stdout.lower() or "cap" in result.stdout.lower()
