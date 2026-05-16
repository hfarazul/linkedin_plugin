"""Shared pytest fixtures.

Tests use a temp SQLite DB per test (via `LINKEDIN_DB_PATH` env override),
the fake LinkedIn adapter (`LINKEDIN_BACKEND=fake`), and the FakeTelegramClient
when they need to assert on Telegram interactions without HTTP.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from .fakes import FakeTelegramClient


# ----- env fixture (was in test_smoke.py) -----------------------------------

@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """Per-test isolated env: fake backend, temp DB, generous caps, no delays."""
    db_path = tmp_path / "test.db"
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
        # Telegram creds not used in offline tests, but the config loader is fine
        # with them being absent. Tests that need a TelegramClient should
        # monkeypatch to a FakeTelegramClient anyway.
    }


# ----- in-process DB fixture for tests that don't shell out to the CLI ------

@pytest.fixture
def db_env(env, monkeypatch) -> dict[str, str]:
    """Like `env`, but also exports the vars into the current process so
    in-process imports of linkedin_agent.db see the right DB_PATH."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # linkedin_agent.db reads LINKEDIN_DB_PATH at import time. Force re-eval
    # by reimporting and re-resolving DB_PATH for this test.
    import importlib
    from linkedin_agent import db as db_module
    importlib.reload(db_module)
    db_module.init_db()
    return env


# ----- Telegram fake --------------------------------------------------------

@pytest.fixture
def fake_telegram(monkeypatch) -> FakeTelegramClient:
    """Return a FakeTelegramClient and patch linkedin_agent.telegram.TelegramClient
    so every TelegramClient() construction yields the same fake."""
    fake = FakeTelegramClient()

    def factory(cfg=None):
        return fake

    import linkedin_agent.telegram as tg_mod
    monkeypatch.setattr(tg_mod, "TelegramClient", factory)
    # Also patch any modules that did `from .telegram import TelegramClient`
    # before this fixture ran.
    for mod_name in ("linkedin_agent.bot_daemon", "linkedin_agent.poll", "linkedin_agent.cli"):
        try:
            module = __import__(mod_name, fromlist=["TelegramClient"])
            if hasattr(module, "TelegramClient"):
                monkeypatch.setattr(module, "TelegramClient", factory)
        except ImportError:
            pass
    return fake


# ----- DB helper used in subprocess-based tests ------------------------------

def db_query(db_path: str | Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Run a read query against a test DB and return rows.
    Used by subprocess-style integration tests."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()
