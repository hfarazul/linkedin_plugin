"""Tests for the send-window helper and the daemon's window-aware approval path."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time

from linkedin_agent import send_window


# ===== unit: is_open ========================================================

@pytest.mark.unit
@pytest.mark.parametrize("when, expected", [
    ("2026-05-18 10:00:00", True),    # Monday 10am
    ("2026-05-18 09:00:00", True),    # Monday 9am exactly (start, inclusive)
    ("2026-05-18 16:59:59", True),    # Monday just before close
    ("2026-05-18 17:00:00", False),   # Monday 17:00 exactly (closed)
    ("2026-05-18 08:59:59", False),   # Monday 8:59am
    ("2026-05-22 14:00:00", True),    # Friday 2pm
    ("2026-05-23 14:00:00", False),   # Saturday 2pm
    ("2026-05-24 14:00:00", False),   # Sunday 2pm
    ("2026-05-18 03:00:00", False),   # Monday 3am
])
def test_is_open_at_various_times(when, expected):
    with freeze_time(when):
        assert send_window.is_open() is expected


# ===== unit: next_open_time =================================================

@pytest.mark.unit
def test_next_open_during_window_returns_now():
    with freeze_time("2026-05-18 10:00:00") as frozen:
        now = datetime.now()
        assert send_window.next_open_time() == now


@pytest.mark.unit
def test_next_open_from_friday_evening_is_monday_9am():
    with freeze_time("2026-05-22 18:00:00"):    # Friday 6pm
        expected = datetime(2026, 5, 25, 9, 0, 0)
        assert send_window.next_open_time() == expected


@pytest.mark.unit
def test_next_open_from_saturday_is_monday_9am():
    with freeze_time("2026-05-23 14:00:00"):    # Saturday 2pm
        expected = datetime(2026, 5, 25, 9, 0, 0)
        assert send_window.next_open_time() == expected


@pytest.mark.unit
def test_next_open_from_monday_dawn_is_monday_9am():
    with freeze_time("2026-05-18 07:00:00"):    # Monday 7am
        expected = datetime(2026, 5, 18, 9, 0, 0)
        assert send_window.next_open_time() == expected


@pytest.mark.unit
def test_format_next_open_is_human_readable():
    with freeze_time("2026-05-23 14:00:00"):    # Saturday 2pm → expect Monday 9:00 AM
        s = send_window.format_next_open()
        assert "Mon" in s
        assert "9:00" in s


# ===== 24/7 mode (LINKEDIN_DISABLE_SEND_WINDOW) =============================

@pytest.mark.unit
@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "Yes"])
def test_is_disabled_recognizes_truthy_values(monkeypatch, value):
    monkeypatch.setenv("LINKEDIN_DISABLE_SEND_WINDOW", value)
    assert send_window.is_disabled() is True


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_is_disabled_falsy_or_unset(monkeypatch, value):
    monkeypatch.setenv("LINKEDIN_DISABLE_SEND_WINDOW", value)
    assert send_window.is_disabled() is False


@pytest.mark.unit
def test_disabled_window_is_always_open(monkeypatch):
    """When 24/7 mode is on, is_open returns True even on Saturday at 3 AM."""
    monkeypatch.setenv("LINKEDIN_DISABLE_SEND_WINDOW", "1")
    with freeze_time("2026-05-23 03:00:00"):   # Saturday 3am — normally closed
        assert send_window.is_open() is True


@pytest.mark.unit
def test_fake_window_override_still_wins_over_disable(monkeypatch):
    """LINKEDIN_FAKE_WINDOW (for tests) must override LINKEDIN_DISABLE_SEND_WINDOW
    so tests can still force the closed branch even with 24/7 mode set."""
    monkeypatch.setenv("LINKEDIN_DISABLE_SEND_WINDOW", "1")
    monkeypatch.setenv("LINKEDIN_FAKE_WINDOW", "closed")
    assert send_window.is_open() is False


@pytest.mark.unit
def test_format_next_open_says_now_when_disabled(monkeypatch):
    monkeypatch.setenv("LINKEDIN_DISABLE_SEND_WINDOW", "1")
    assert send_window.format_next_open() == "now"


# ===== integration: send-approved CLI =======================================

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run_cli(args, env, *, expect_fail=False):
    result = subprocess.run(
        [sys.executable, "-m", "linkedin_agent", *args],
        cwd=ROOT, env={**os.environ, **env},
        capture_output=True, text=True,
    )
    if expect_fail:
        assert result.returncode != 0, f"unexpected success\n{result.stdout}\n{result.stderr}"
    else:
        assert result.returncode == 0, f"failed rc={result.returncode}\n{result.stdout}\n{result.stderr}"
    return result


def _setup_approved_draft(env, body="test body"):
    """Common: create DB + prospect + manually-approved draft."""
    import sqlite3
    _run_cli(["init"], env)
    _run_cli(["search", "test query", "--limit", "1"], env)
    _run_cli(["_debug-enqueue", "1", "dm1", body, "--no-push"], env)
    conn = sqlite3.connect(env["LINKEDIN_DB_PATH"])
    conn.execute("UPDATE pending_drafts SET status='approved' WHERE id=1")
    conn.commit()
    conn.close()


@pytest.mark.integration
def test_send_approved_picks_up_queued_during_window(env):
    """During the send window (forced open via env var), an approved draft
    transitions to 'sent' and an action row gets logged."""
    env = {**env, "LINKEDIN_FAKE_WINDOW": "open"}
    _setup_approved_draft(env)

    result = _run_cli(["send-approved"], env)
    assert "sent draft #1" in result.stdout

    import sqlite3
    conn = sqlite3.connect(env["LINKEDIN_DB_PATH"])
    assert conn.execute("SELECT status FROM pending_drafts WHERE id=1").fetchone()[0] == "sent"
    actions = conn.execute("SELECT kind FROM actions WHERE kind='dm'").fetchall()
    assert len(actions) == 1
    conn.close()


@pytest.mark.integration
def test_send_approved_blocks_outside_window(env):
    """Outside the window, send-approved is a no-op and the draft stays queued."""
    env = {**env, "LINKEDIN_FAKE_WINDOW": "closed"}
    _setup_approved_draft(env, body="should not send")

    result = _run_cli(["send-approved"], env)
    assert "window closed" in result.stdout.lower()

    import sqlite3
    conn = sqlite3.connect(env["LINKEDIN_DB_PATH"])
    assert conn.execute("SELECT status FROM pending_drafts WHERE id=1").fetchone()[0] == "approved"
    assert not conn.execute("SELECT kind FROM actions WHERE kind='dm'").fetchall()
    conn.close()


@pytest.mark.integration
def test_send_approved_force_overrides_closed_window(env):
    """--force bypasses the closed-window check."""
    env = {**env, "LINKEDIN_FAKE_WINDOW": "closed"}
    _setup_approved_draft(env, body="force send")

    result = _run_cli(["send-approved", "--force"], env)
    assert "sent draft #1" in result.stdout
