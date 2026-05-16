"""Tests for setup.sh in non-interactive mode.

These are real bash invocations against the script. We can't fully exercise
the interactive prompts (Unipile prompts, Telegram bot creation), so we set
LINKEDIN_SETUP_NONINTERACTIVE=1 to skip prompts and assume the .env we
copy into the temp directory is already populated.

What we DO verify:
- Script runs to completion with rc=0
- Python venv is created
- pip install -e .[dev] succeeds
- linkedin init creates the SQLite DB
- Re-running is idempotent (no errors second pass)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _setup_repo_clone(tmp_path: Path) -> Path:
    """Copy the bare minimum project files into tmp_path so setup.sh has
    something to install. We deliberately avoid copying .venv and data/."""
    dest = tmp_path / "clone"
    dest.mkdir()
    for item in [
        "linkedin_agent", "tests", ".claude", "campaigns", "scripts",
        "pyproject.toml", ".env.example", ".gitignore", "setup.sh",
        "CLAUDE.md", "README.md", "docs",
    ]:
        src = ROOT / item
        if not src.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dest / item)
        else:
            shutil.copy2(src, dest / item)
    return dest


@pytest.mark.integration
@pytest.mark.slow
def test_setup_sh_runs_to_completion(tmp_path):
    """Non-interactive setup: venv created, deps installed, DB initialized."""
    repo = _setup_repo_clone(tmp_path)

    # Seed a usable .env so the prompts get skipped (we only test the
    # mechanical pieces — venv/deps/DB — not the credential capture).
    env_path = repo / ".env"
    env_path.write_text(
        "LINKEDIN_BACKEND=fake\n"
        "UNIPILE_API_KEY=fake-key\n"
        "UNIPILE_ACCOUNT_ID=fake-acct\n"
        "UNIPILE_DSN=api.example.com:1234\n"
        "TELEGRAM_BOT_TOKEN=fake-token\n"
        "TELEGRAM_CHAT_ID=12345\n"
        "DAILY_MAX_REACTIONS=30\n"
        "DAILY_MAX_CONNECTIONS=20\n"
        "DAILY_MAX_DMS=10\n"
        "DAILY_MAX_SEARCHES=50\n"
        "ACTION_DELAY_MIN=0\n"
        "ACTION_DELAY_MAX=0\n"
        "DRY_RUN=0\n"
    )

    result = subprocess.run(
        ["bash", "setup.sh"],
        cwd=repo,
        env={**os.environ, "LINKEDIN_SETUP_NONINTERACTIVE": "1"},
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        pytest.fail(f"setup.sh failed rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    # Verify outputs
    assert (repo / ".venv").exists(), "venv not created"
    assert (repo / ".venv" / "bin" / "python").exists(), "venv python missing"
    assert (repo / "data" / "outreach.db").exists(), "DB not initialized"
    # .env should still have our seeded values (script kept it)
    assert "UNIPILE_API_KEY=fake-key" in env_path.read_text()


@pytest.mark.integration
@pytest.mark.slow
def test_setup_sh_idempotent(tmp_path):
    """Running setup.sh a second time should not error and should not nuke .env."""
    repo = _setup_repo_clone(tmp_path)
    (repo / ".env").write_text(
        "LINKEDIN_BACKEND=fake\nTELEGRAM_BOT_TOKEN=existing-token\nTELEGRAM_CHAT_ID=9999\n"
    )

    env = {**os.environ, "LINKEDIN_SETUP_NONINTERACTIVE": "1"}
    r1 = subprocess.run(["bash", "setup.sh"], cwd=repo, env=env, capture_output=True, text=True, timeout=300)
    assert r1.returncode == 0, f"first run failed: {r1.stderr}"

    r2 = subprocess.run(["bash", "setup.sh"], cwd=repo, env=env, capture_output=True, text=True, timeout=300)
    assert r2.returncode == 0, f"second run failed: {r2.stderr}"

    # Original .env values preserved across runs
    env_content = (repo / ".env").read_text()
    assert "TELEGRAM_BOT_TOKEN=existing-token" in env_content
