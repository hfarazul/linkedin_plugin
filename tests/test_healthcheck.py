"""Tests for the `linkedin healthcheck` subcommand and the daily_completed
action that powers it."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run(args, env, *, expect_fail=False):
    result = subprocess.run(
        [sys.executable, "-m", "linkedin_agent", *args],
        cwd=ROOT, env={**os.environ, **env},
        capture_output=True, text=True,
    )
    if expect_fail:
        assert result.returncode != 0, f"expected fail\n{result.stdout}\n{result.stderr}"
    else:
        assert result.returncode == 0, f"failed rc={result.returncode}\n{result.stdout}\n{result.stderr}"
    return result


# ===== daily logs daily_completed ===========================================

@pytest.mark.integration
def test_daily_logs_daily_completed_action(db_env, fake_telegram):
    """The daily orchestrator must log a 'daily_completed' action at the end
    of its run so healthcheck can verify it fired."""
    import sqlite3
    from linkedin_agent.adapters import get_adapter
    from linkedin_agent import daily as daily_mod

    cfg = type("CFG", (), {
        "backend": "fake",
        "daily_max_reactions": 30, "daily_max_connections": 20,
        "daily_max_dms": 10, "daily_max_searches": 50,
        "action_delay_min": 0, "action_delay_max": 0, "dry_run": False,
        "unipile_api_key": None, "unipile_account_id": None, "unipile_dsn": None,
        "telegram_bot_token": None, "telegram_chat_id": None,
        "playwright_state_path": None,
    })()
    adapter = get_adapter(cfg)
    try:
        daily_mod.run_daily(
            cfg, adapter=adapter, telegram=fake_telegram,
            drafter=lambda k, p, recent_posts=None: f"stub {k} {p}",
        )
    finally:
        adapter.close()

    conn = sqlite3.connect(db_env["LINKEDIN_DB_PATH"])
    row = conn.execute(
        "SELECT result, payload FROM actions WHERE kind='daily_completed'"
    ).fetchone()
    conn.close()
    assert row is not None, "daily_completed action not logged"
    assert row[0] == "ok"
    # Payload is JSON with counts
    import json
    payload = json.loads(row[1])
    assert "reactions" in payload
    assert "errors" in payload


# ===== healthcheck CLI ======================================================

@pytest.mark.integration
def test_healthcheck_passes_when_recent_daily_completed_exists(env):
    """Window open + recent daily_completed → exit 0."""
    env = {**env, "LINKEDIN_FAKE_WINDOW": "open"}
    _run(["init"], env)

    # Seed a recent daily_completed action
    import sqlite3
    conn = sqlite3.connect(env["LINKEDIN_DB_PATH"])
    conn.execute(
        "INSERT INTO actions (kind, payload, result, dry_run, created_at) "
        "VALUES ('daily_completed', '{}', 'ok', 0, datetime('now', '-15 minutes'))"
    )
    conn.commit()
    conn.close()

    result = _run(["healthcheck", "--quiet"], env)
    assert "last daily run at" in result.stdout


@pytest.mark.integration
def test_healthcheck_fails_when_no_recent_daily_completed(env):
    """Window open + no daily_completed in window → exit 1."""
    env = {**env, "LINKEDIN_FAKE_WINDOW": "open"}
    _run(["init"], env)

    # Seed a STALE daily_completed (2h ago, outside 90min default window)
    import sqlite3
    conn = sqlite3.connect(env["LINKEDIN_DB_PATH"])
    conn.execute(
        "INSERT INTO actions (kind, payload, result, dry_run, created_at) "
        "VALUES ('daily_completed', '{}', 'ok', 0, datetime('now', '-120 minutes'))"
    )
    conn.commit()
    conn.close()

    result = _run(["healthcheck", "--quiet"], env, expect_fail=True)
    assert "healthcheck failed" in result.stdout


@pytest.mark.integration
def test_healthcheck_skips_outside_window(env):
    """Outside business hours, healthcheck is a no-op (exit 0)."""
    env = {**env, "LINKEDIN_FAKE_WINDOW": "closed"}
    _run(["init"], env)
    result = _run(["healthcheck", "--quiet"], env)
    assert "skipped" in result.stdout.lower()


@pytest.mark.integration
def test_healthcheck_custom_max_age(env):
    """--max-age-minutes shortens the acceptable window."""
    env = {**env, "LINKEDIN_FAKE_WINDOW": "open"}
    _run(["init"], env)

    # Seed an action 30 minutes ago
    import sqlite3
    conn = sqlite3.connect(env["LINKEDIN_DB_PATH"])
    conn.execute(
        "INSERT INTO actions (kind, payload, result, dry_run, created_at) "
        "VALUES ('daily_completed', '{}', 'ok', 0, datetime('now', '-30 minutes'))"
    )
    conn.commit()
    conn.close()

    # 60min window includes it: pass
    _run(["healthcheck", "--max-age-minutes", "60", "--quiet"], env)
    # 15min window excludes it: fail
    _run(["healthcheck", "--max-age-minutes", "15", "--quiet"], env, expect_fail=True)
