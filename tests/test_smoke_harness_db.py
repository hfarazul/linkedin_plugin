"""The smoke harness must never write to the real database.

It points LINKEDIN_DB_PATH at a temporary file before importing `db`. That
only works if nothing imported `db` first, because `db` reads the variable
once, at import. Commit a96c90c moved `evidence_context` — which imports
`db` — to module scope, and from then on every harness run wrote its
prospects and drafts into data/outreach.db. On a deployment host those are
test profiles sitting at `targeted`, in front of the hourly cron.

The suite never noticed: the `db_env` fixture reloads `db`, so tests always
see the right path whatever the harness does.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "scripts" / "smoke_e2e.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("smoke_e2e_db_check", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_loading_the_harness_does_not_import_the_database() -> None:
    """In a fresh interpreter, so no earlier test has imported `db` already."""
    probe = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('h', r'{HARNESS}')\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "print('linkedin_agent.db' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, cwd=ROOT, check=True)
    assert out.stdout.strip() == "False", (
        "loading scripts/smoke_e2e.py imported linkedin_agent.db, which fixes "
        "DB_PATH before the harness can point it at a throwaway file")


@pytest.mark.unit
def test_the_throwaway_database_wins_even_if_db_was_imported_first(
        monkeypatch, tmp_path) -> None:
    from linkedin_agent import db

    real = tmp_path / "outreach.db"
    monkeypatch.setattr(db, "DB_PATH", real)       # as if imported early
    monkeypatch.setenv("LINKEDIN_DB_PATH", str(real))

    used = _load_harness()._use_throwaway_db()

    assert db.DB_PATH == used
    assert db.DB_PATH != real
    assert Path(tempfile.gettempdir()) in used.parents


@pytest.mark.integration
def test_a_harness_run_writes_only_to_the_throwaway_database(tmp_path) -> None:
    """End to end, offline: an unknown provider stops the run right after the
    database is created, before anything touches the network. The configured
    database must not be created, and the run must not crash on the way out
    — the first version of this fix stopped assigning the path the closing
    summary prints, and only a real run showed it."""
    import os

    configured = tmp_path / "configured.db"
    env = dict(os.environ, LINKEDIN_DB_PATH=str(configured),
               LINKEDIN_PRIMARY_PROVIDER="no-such-provider",
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    out = subprocess.run(
        [sys.executable, str(HARNESS), "--profile",
         "https://www.linkedin.com/in/nobody-harness-check/"],
        capture_output=True, text=True, cwd=ROOT, env=env, timeout=120)
    output = out.stdout + out.stderr

    assert "schema initialised at e2e_smoke.db" in output
    assert not configured.exists()
    assert "Traceback" not in output, output[-2000:]
