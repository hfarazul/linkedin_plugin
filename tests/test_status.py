"""Tests for the status dashboard CLI subcommand."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run_cli(args, env, *, expect_fail=False):
    result = subprocess.run(
        [sys.executable, "-m", "linkedin_agent", *args],
        cwd=ROOT, env={**os.environ, **env},
        capture_output=True, text=True,
    )
    if expect_fail:
        assert result.returncode != 0, f"unexpected success: {result.stdout}\n{result.stderr}"
    else:
        assert result.returncode == 0, f"failed rc={result.returncode}\n{result.stdout}\n{result.stderr}"
    return result


def _seed(env, sql):
    import sqlite3
    conn = sqlite3.connect(env["LINKEDIN_DB_PATH"])
    for stmt in sql.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.commit()
    conn.close()


@pytest.mark.integration
def test_status_empty_db(env):
    _run_cli(["init"], env)
    result = _run_cli(["status"], env)
    # All stage counts are zero; output still renders without error
    assert "Caps today" in result.stdout
    assert "react 0/30" in result.stdout
    assert "0" in result.stdout    # at least one zero count
    assert "Stage" in result.stdout


@pytest.mark.integration
def test_status_shows_caps_used(env):
    _run_cli(["init"], env)
    _run_cli(["search", "q1", "--limit", "2"], env)        # logs 2 search actions
    _run_cli(["react", "1"], env)                          # logs 1 react

    result = _run_cli(["status"], env)
    assert "react 1/30" in result.stdout
    assert "connect 0/20" in result.stdout


@pytest.mark.integration
def test_status_shows_pipeline_counts(env):
    _run_cli(["init"], env)
    # Seed prospects across stages
    _run_cli(["search", "q", "--limit", "3"], env)         # 3 targeted prospects
    _seed(env, """
        UPDATE prospects SET status='reacted' WHERE id=2;
        UPDATE prospects SET status='dm_sent', dm_count=1, last_dm_at='2026-05-10T10:00:00+00:00' WHERE id=3;
    """)
    result = _run_cli(["status"], env)
    # targeted: 1, reacted: 1, dm_sent: 1
    assert "targeted" in result.stdout
    assert "reacted" in result.stdout
    assert "dm_sent" in result.stdout


@pytest.mark.integration
def test_status_flags_replies_needing_attention(env):
    _run_cli(["init"], env)
    _run_cli(["search", "q", "--limit", "1"], env)
    _seed(env, "UPDATE prospects SET status='replied', full_name='Test Replier', company='Test Co' WHERE id=1")

    result = _run_cli(["status"], env)
    assert "needing attention" in result.stdout
    assert "Test Replier" in result.stdout


@pytest.mark.integration
def test_status_shows_pending_approvals(env):
    _run_cli(["init"], env)
    _run_cli(["search", "q", "--limit", "1"], env)
    _run_cli(["_debug-enqueue", "1", "dm1", "draft body", "--no-push"], env)

    result = _run_cli(["status"], env)
    assert "pending approval" in result.stdout


@pytest.mark.integration
def test_status_window_open_label(env):
    env = {**env, "LINKEDIN_FAKE_WINDOW": "open"}
    _run_cli(["init"], env)
    result = _run_cli(["status"], env)
    assert "OPEN" in result.stdout


@pytest.mark.integration
def test_status_window_closed_label(env):
    env = {**env, "LINKEDIN_FAKE_WINDOW": "closed"}
    _run_cli(["init"], env)
    result = _run_cli(["status"], env)
    assert "CLOSED" in result.stdout
