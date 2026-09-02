from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "outreach.db"
DB_PATH = Path(os.environ["LINKEDIN_DB_PATH"]) if os.environ.get("LINKEDIN_DB_PATH") else _DEFAULT_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    brief_path      TEXT NOT NULL,
    target_icp      TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);

CREATE TABLE IF NOT EXISTS prospects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    linkedin_url    TEXT NOT NULL UNIQUE,
    full_name       TEXT,
    headline        TEXT,
    company         TEXT,
    title           TEXT,
    location        TEXT,
    status          TEXT NOT NULL DEFAULT 'targeted',
    notes           TEXT,
    first_seen_at   TEXT NOT NULL,
    last_action_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);

CREATE TABLE IF NOT EXISTS actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id     INTEGER REFERENCES prospects(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    payload         TEXT,
    result          TEXT,
    dry_run         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_actions_kind_time ON actions(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_actions_prospect ON actions(prospect_id);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id     INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
    direction       TEXT NOT NULL CHECK (direction IN ('outbound','inbound')),
    body            TEXT NOT NULL,
    external_id     TEXT,
    sent_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_prospect ON messages(prospect_id, sent_at);
-- idx_messages_external is created in _POST_MIGRATE_INDEXES after the
-- external_id column is added to pre-existing messages tables.

CREATE TABLE IF NOT EXISTS pending_drafts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id           INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
    kind                  TEXT NOT NULL,
    body                  TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending',
    telegram_message_id   INTEGER,
    drafted_at            TEXT NOT NULL,
    decided_at            TEXT,
    reject_reason         TEXT
);

CREATE INDEX IF NOT EXISTS idx_drafts_status ON pending_drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_prospect ON pending_drafts(prospect_id);

-- A detected event about a prospect: a funding round, a hiring post, a career
-- transition. Signals are the structured record of WHY we are reaching out;
-- prospects.pitch_context becomes a rendered projection of the active one.
--
-- Append-only. Nothing updates a signal's facts — a changed world produces a
-- new signal, so the trail of what we believed when we sent a message stays
-- intact. Only `status` moves, tracking what we did about it.
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id     INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'new',
    confidence      TEXT NOT NULL,
    detected_at     TEXT NOT NULL,
    occurred_at     TEXT,
    expires_at      TEXT,
    payload         TEXT,
    -- Stable identity for the underlying real-world event. The UNIQUE index is
    -- what makes re-scouting the same person idempotent: a second detection of
    -- the same transition collides here instead of producing a duplicate
    -- signal and a duplicate draft.
    dedup_key       TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_signals_prospect ON signals(prospect_id);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status, kind);

-- One supporting fact per row. Every personalized claim in a draft must trace
-- to one of these, which is what makes a sent message auditable months later.
--
-- Append-only and never edited: there are deliberately no update or delete
-- helpers. Evidence that turns out to be wrong is superseded by a new signal,
-- not rewritten.
CREATE TABLE IF NOT EXISTS evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
    claim           TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    source_url      TEXT,
    raw_excerpt     TEXT,
    fetched_at      TEXT NOT NULL,
    -- Hash of raw_excerpt, so we can later tell whether a claim was built from
    -- source text that has since changed upstream.
    checksum        TEXT
);

CREATE INDEX IF NOT EXISTS idx_evidence_signal ON evidence(signal_id);

-- One job in a prospect's history, normalized across providers.
--
-- Stored rather than fetched on demand because detection is a pure function
-- over these rows: re-running a detector after a rule change costs nothing and
-- spends no scraping budget.
--
-- date_precision is carried explicitly because the providers disagree. Unipile
-- emits "1/1/YYYY" when only a year is known; PhantomBuster emits "Feb 2026".
-- A detector asking "did this start within 180 days?" cannot answer honestly
-- from a year, so precision has to travel with the value instead of being
-- inferred from it.
CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id     INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
    company         TEXT NOT NULL,
    company_id      TEXT,
    company_url     TEXT,
    title           TEXT,
    started_at      TEXT,
    ended_at        TEXT,
    is_current      INTEGER NOT NULL DEFAULT 0,
    date_precision  TEXT NOT NULL DEFAULT 'unknown',
    location        TEXT,
    description     TEXT,
    source          TEXT NOT NULL DEFAULT 'unknown',
    raw_json        TEXT,
    fetched_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_prospect ON positions(prospect_id, started_at);

-- Outstanding provider work that cannot be waited on inline.
--
-- Unipile answers in milliseconds; PhantomBuster boots a browser and scrapes
-- for seconds to minutes. Blocking on the latter inside daily.py's per-prospect
-- loop would make an hourly cron overrun its own schedule, so async work is
-- submitted on one tick and collected on a later one.
--
-- Synchronous providers still get a row: they are submitted and completed in
-- the same call, which keeps one code path and one audit trail regardless of
-- which provider served the request.
CREATE TABLE IF NOT EXISTS research_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    capability      TEXT NOT NULL,
    provider        TEXT NOT NULL,
    prospect_id     INTEGER REFERENCES prospects(id) ON DELETE CASCADE,
    target          TEXT,               -- identifier the job was launched for
    container_id    TEXT,               -- provider-side run id, async only
    status          TEXT NOT NULL DEFAULT 'running',
    arguments       TEXT,
    result_summary  TEXT,
    error           TEXT,
    submitted_at    TEXT NOT NULL,
    completed_at    TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON research_jobs(status, submitted_at);
CREATE INDEX IF NOT EXISTS idx_jobs_prospect ON research_jobs(prospect_id);
"""

VALID_JOB_STATUSES = ("running", "finished", "failed", "timeout")

# Identity of a position, so re-enriching the same prospect updates rows rather
# than accumulating duplicates. Created after the table for the same reason the
# messages index is: it must survive a DB that predates the column set.
_POSITIONS_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_identity
    ON positions(prospect_id, company, title, started_at);
"""

# Pipeline statuses tracked on prospects.status.
VALID_STATUSES = (
    "targeted",
    "reacted",
    "connection_sent",
    "connected",
    "dm_sent",
    "replied",
    "skipped",
)

# Disposition is the user's classification AFTER conversation begins.
# Independent of pipeline status — a prospect can be 'replied' AND 'interested'.
VALID_DISPOSITIONS = (
    "interested",
    "not_fit",
    "ghosted",
    "won",
    "lost",
    "deferred",
)

VALID_DRAFT_KINDS = ("connect_note", "dm1", "dm2", "dm3", "reply")
VALID_DRAFT_STATUSES = ("pending", "approved", "rejected", "sent")
VALID_CAMPAIGN_STATUSES = ("active", "paused", "archived")

# Where a signal is in its lifecycle. Only this field moves after insert —
# the facts a signal records are immutable.
VALID_SIGNAL_STATUSES = (
    "new",         # detected, not yet qualified
    "qualified",   # passed the ICP gates, eligible to draft from
    "drafted",     # a draft has been produced for it
    "actioned",    # the draft was approved and sent
    "dismissed",   # rejected by a human or by a gate
    "expired",     # aged out; must never produce a draft
)

# Three tiers, not a float: a number invites false precision and an argument
# about thresholds. Each tier maps to a distinct behaviour — 'high' may state
# the signal as fact, 'medium' must hedge, 'low' never reaches the drafter.
VALID_CONFIDENCE = ("high", "medium", "low")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- migrations -------------------------------------------------------------

# SQLite has no `ALTER TABLE ADD COLUMN IF NOT EXISTS`. We introspect via
# PRAGMA table_info and add columns conditionally so init_db stays idempotent
# across schema versions.
_PROSPECT_COLUMNS = {
    "campaign_id":   "INTEGER REFERENCES campaigns(id)",
    "disposition":   "TEXT",
    "last_dm_at":    "TEXT",
    "dm_count":      "INTEGER NOT NULL DEFAULT 0",
    "pitch_context": "TEXT",
    # LinkedIn-internal id (ACo…). Populated from search hits / on-demand
    # resolution so the poll loop can map inbound messages to prospect rows.
    "provider_id":   "TEXT",
    # ---- enrichment columns (populated by enrichment.enrich()) -------------
    "public_identifier":         "TEXT",
    "network_distance":          "TEXT",   # FIRST_DEGREE | SECOND_DEGREE | THIRD_DEGREE | DISTANCE_X
    "mutual_connections_count":  "INTEGER",
    "follower_count":            "INTEGER",
    "connections_count":         "INTEGER",
    "is_premium":                "INTEGER",  # 0/1
    "is_open_profile":           "INTEGER",  # 0/1
    "is_creator":                "INTEGER",  # 0/1
    "is_influencer":             "INTEGER",  # 0/1
    "is_relationship":           "INTEGER",  # 0/1 — already connected on LinkedIn
    "pronoun":                   "TEXT",     # "She/Her", "He/Him", etc.
    "last_post_at":              "TEXT",     # ISO timestamp of most recent post
    "enriched_at":               "TEXT",     # when enrichment last ran
}

_MESSAGE_COLUMNS = {
    "external_id": "TEXT",
    # Channel groundwork. Everything existing is LinkedIn, and the default
    # keeps it that way, so no read path changes. Keeping both channels in one
    # table means drafter.build_input()'s prior_messages query keeps working
    # unchanged and a reply draft can see the whole relationship, not half of it.
    "channel":     "TEXT NOT NULL DEFAULT 'linkedin'",
    "subject":     "TEXT",   # email only; NULL for LinkedIn messages
    "thread_id":   "TEXT",   # provider-side conversation id
}

_DRAFT_COLUMNS = {
    "channel":   "TEXT NOT NULL DEFAULT 'linkedin'",
    "subject":   "TEXT",
    # The signal that justified this draft. The audit link that lets us answer
    # "why did we send this?" from the draft alone.
    "signal_id": "INTEGER REFERENCES signals(id)",
}


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, decl in columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _migrate(conn: sqlite3.Connection) -> None:
    _add_missing_columns(conn, "prospects", _PROSPECT_COLUMNS)
    _add_missing_columns(conn, "messages", _MESSAGE_COLUMNS)
    _add_missing_columns(conn, "pending_drafts", _DRAFT_COLUMNS)


# The unique partial index on messages.external_id can only be created after
# the column exists, so it runs separately from SCHEMA after the migration.
_POST_MIGRATE_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_external
    ON messages(external_id) WHERE external_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_prospects_provider
    ON prospects(provider_id) WHERE provider_id IS NOT NULL;
"""


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.executescript(_POST_MIGRATE_INDEXES)
        conn.executescript(_POSITIONS_INDEX)


# --- prospects --------------------------------------------------------------

def upsert_prospect(
    linkedin_url: str,
    full_name: str | None = None,
    headline: str | None = None,
    company: str | None = None,
    title: str | None = None,
    location: str | None = None,
    campaign_id: int | None = None,
    pitch_context: str | None = None,
    provider_id: str | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute("SELECT id FROM prospects WHERE linkedin_url = ?", (linkedin_url,))
        row = cur.fetchone()
        if row:
            conn.execute(
                """UPDATE prospects
                   SET full_name     = COALESCE(?, full_name),
                       headline      = COALESCE(?, headline),
                       company       = COALESCE(?, company),
                       title         = COALESCE(?, title),
                       location      = COALESCE(?, location),
                       campaign_id   = COALESCE(?, campaign_id),
                       pitch_context = COALESCE(?, pitch_context),
                       provider_id   = COALESCE(?, provider_id)
                   WHERE id = ?""",
                (full_name, headline, company, title, location, campaign_id, pitch_context, provider_id, row["id"]),
            )
            return int(row["id"])
        cur = conn.execute(
            """INSERT INTO prospects
               (linkedin_url, full_name, headline, company, title, location,
                campaign_id, pitch_context, provider_id, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (linkedin_url, full_name, headline, company, title, location,
             campaign_id, pitch_context, provider_id, now()),
        )
        return int(cur.lastrowid)


def get_prospect_by_provider_id(provider_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        cur = conn.execute("SELECT * FROM prospects WHERE provider_id = ?", (provider_id,))
        return cur.fetchone()


def set_status(prospect_id: int, status: str) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status {status!r}; expected one of {VALID_STATUSES}")
    with connect() as conn:
        conn.execute(
            "UPDATE prospects SET status = ?, last_action_at = ? WHERE id = ?",
            (status, now(), prospect_id),
        )


def set_disposition(prospect_id: int, disposition: str) -> None:
    if disposition not in VALID_DISPOSITIONS:
        raise ValueError(f"invalid disposition {disposition!r}; expected one of {VALID_DISPOSITIONS}")
    with connect() as conn:
        conn.execute(
            "UPDATE prospects SET disposition = ?, last_action_at = ? WHERE id = ?",
            (disposition, now(), prospect_id),
        )


def record_dm(prospect_id: int) -> None:
    """Called after a DM is successfully sent. Bumps dm_count and last_dm_at."""
    with connect() as conn:
        conn.execute(
            "UPDATE prospects SET dm_count = dm_count + 1, last_dm_at = ? WHERE id = ?",
            (now(), prospect_id),
        )


def list_prospects(
    status: str | None = None,
    campaign_id: int | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if campaign_id is not None:
        clauses.append("campaign_id = ?")
        params.append(campaign_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        cur = conn.execute(
            f"SELECT * FROM prospects {where} ORDER BY last_action_at DESC NULLS LAST LIMIT ?",
            params,
        )
        return list(cur.fetchall())


def get_prospect(prospect_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        cur = conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,))
        return cur.fetchone()


# --- actions ----------------------------------------------------------------

def log_action(prospect_id: int | None, kind: str, payload: str | None, result: str | None, dry_run: bool) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO actions (prospect_id, kind, payload, result, dry_run, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (prospect_id, kind, payload, result, 1 if dry_run else 0, now()),
        )


def count_actions_last_24h(kind: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            """SELECT COUNT(*) FROM actions
               WHERE kind = ? AND dry_run = 0
               AND created_at >= datetime('now', '-1 day')""",
            (kind,),
        )
        return int(cur.fetchone()[0])


# --- messages ---------------------------------------------------------------

def record_message(
    prospect_id: int,
    direction: str,
    body: str,
    external_id: str | None = None,
    *,
    channel: str = "linkedin",
    subject: str | None = None,
    thread_id: str | None = None,
    sent_at: str | None = None,
) -> int | None:
    """Insert a message row. Returns the new row id, or None if external_id
    collides (deduplication during polling).

    The keyword-only arguments are additive and all default to today's
    behaviour, so every existing call site keeps working untouched.

    `sent_at` accepts the provider's own timestamp. Defaulting it to now() —
    which is what happens when it is omitted — records when we *observed* a
    message rather than when it was sent, and the two diverge badly with a
    batch scraper that may surface a reply hours after it arrived.
    """
    with connect() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO messages
                   (prospect_id, direction, body, external_id, channel,
                    subject, thread_id, sent_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (prospect_id, direction, body, external_id, channel,
                 subject, thread_id, sent_at or now()),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None  # duplicate external_id — already seen


# --- campaigns --------------------------------------------------------------

def upsert_campaign(slug: str, name: str, brief_path: str, target_icp: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (slug,))
        row = cur.fetchone()
        if row:
            conn.execute(
                """UPDATE campaigns
                   SET name       = ?,
                       brief_path = ?,
                       target_icp = COALESCE(?, target_icp)
                   WHERE id = ?""",
                (name, brief_path, target_icp, row["id"]),
            )
            return int(row["id"])
        cur = conn.execute(
            """INSERT INTO campaigns (slug, name, brief_path, target_icp, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (slug, name, brief_path, target_icp, now()),
        )
        return int(cur.lastrowid)


def set_campaign_status(campaign_id: int, status: str) -> None:
    if status not in VALID_CAMPAIGN_STATUSES:
        raise ValueError(f"invalid campaign status {status!r}; expected one of {VALID_CAMPAIGN_STATUSES}")
    with connect() as conn:
        conn.execute("UPDATE campaigns SET status = ? WHERE id = ?", (status, campaign_id))


def list_campaigns(status: str | None = None) -> list[sqlite3.Row]:
    with connect() as conn:
        if status:
            cur = conn.execute(
                "SELECT * FROM campaigns WHERE status = ? ORDER BY created_at DESC",
                (status,),
            )
        else:
            cur = conn.execute("SELECT * FROM campaigns ORDER BY created_at DESC")
        return list(cur.fetchall())


def get_campaign(slug_or_id: str | int) -> sqlite3.Row | None:
    with connect() as conn:
        if isinstance(slug_or_id, int) or (isinstance(slug_or_id, str) and slug_or_id.isdigit()):
            cur = conn.execute("SELECT * FROM campaigns WHERE id = ?", (int(slug_or_id),))
        else:
            cur = conn.execute("SELECT * FROM campaigns WHERE slug = ?", (slug_or_id,))
        return cur.fetchone()


# --- pending drafts ---------------------------------------------------------

def enqueue_draft(prospect_id: int, kind: str, body: str) -> int:
    if kind not in VALID_DRAFT_KINDS:
        raise ValueError(f"invalid draft kind {kind!r}; expected one of {VALID_DRAFT_KINDS}")
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO pending_drafts (prospect_id, kind, body, drafted_at)
               VALUES (?, ?, ?, ?)""",
            (prospect_id, kind, body, now()),
        )
        return int(cur.lastrowid)


def update_draft_body(draft_id: int, new_body: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE pending_drafts SET body = ? WHERE id = ?", (new_body, draft_id))


def set_draft_telegram_id(draft_id: int, telegram_message_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE pending_drafts SET telegram_message_id = ? WHERE id = ?",
            (telegram_message_id, draft_id),
        )


def set_draft_status(draft_id: int, status: str, reject_reason: str | None = None) -> None:
    if status not in VALID_DRAFT_STATUSES:
        raise ValueError(f"invalid draft status {status!r}; expected one of {VALID_DRAFT_STATUSES}")
    with connect() as conn:
        conn.execute(
            """UPDATE pending_drafts
               SET status = ?, decided_at = ?, reject_reason = COALESCE(?, reject_reason)
               WHERE id = ?""",
            (status, now(), reject_reason, draft_id),
        )


def list_pending_drafts(prospect_id: int | None = None, status: str = "pending") -> list[sqlite3.Row]:
    with connect() as conn:
        if prospect_id is not None:
            cur = conn.execute(
                """SELECT * FROM pending_drafts
                   WHERE status = ? AND prospect_id = ?
                   ORDER BY drafted_at""",
                (status, prospect_id),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM pending_drafts WHERE status = ? ORDER BY drafted_at",
                (status,),
            )
        return list(cur.fetchall())


def get_draft(draft_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        cur = conn.execute("SELECT * FROM pending_drafts WHERE id = ?", (draft_id,))
        return cur.fetchone()


def cancel_pending_drafts_for(prospect_id: int, reason: str) -> int:
    """Used when a reply lands — cancel any not-yet-sent drafts for the prospect."""
    with connect() as conn:
        cur = conn.execute(
            """UPDATE pending_drafts
               SET status = 'rejected', decided_at = ?, reject_reason = ?
               WHERE prospect_id = ? AND status IN ('pending', 'approved')""",
            (now(), reason, prospect_id),
        )
        return cur.rowcount


# --- positions --------------------------------------------------------------


def replace_positions(prospect_id: int, positions: list) -> int:
    """Persist a prospect's job history, replacing what a previous run stored.

    Replace rather than append: a profile is a snapshot, and a person editing
    or removing a role should not leave a stale row behind for a detector to
    reason from. Positions carry no independent history of their own — the
    audit trail lives in `evidence`, which quotes what was true when a message
    went out.

    Takes provider-neutral Position objects (providers.capabilities.Position),
    so both vendors write identical rows.
    """
    stamp = now()
    with connect() as conn:
        conn.execute("DELETE FROM positions WHERE prospect_id = ?", (prospect_id,))
        written = 0
        for p in positions:
            try:
                conn.execute(
                    """INSERT INTO positions
                       (prospect_id, company, company_id, company_url, title,
                        started_at, ended_at, is_current, date_precision,
                        location, description, source, raw_json, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (prospect_id, p.company, p.company_id, p.company_url, p.title,
                     p.start_date, p.end_date, 1 if p.is_current else 0,
                     p.date_precision, p.location, p.description, p.source,
                     json.dumps(p.raw) if p.raw else None, stamp),
                )
                written += 1
            except sqlite3.IntegrityError:
                # Same company/title/start twice in one payload — keep the first.
                continue
        return written


def list_positions(prospect_id: int) -> list[sqlite3.Row]:
    """Newest first. Positions with no parseable start sort last rather than
    appearing recent."""
    with connect() as conn:
        cur = conn.execute(
            """SELECT * FROM positions WHERE prospect_id = ?
               ORDER BY started_at IS NULL, started_at DESC""",
            (prospect_id,),
        )
        return list(cur.fetchall())


# --- research jobs ----------------------------------------------------------


def create_job(capability: str, provider: str, *, prospect_id: int | None = None,
               target: str | None = None, container_id: str | None = None,
               arguments: str | None = None, status: str = "running") -> int:
    if status not in VALID_JOB_STATUSES:
        raise ValueError(f"invalid job status {status!r}; expected one of {VALID_JOB_STATUSES}")
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO research_jobs
               (capability, provider, prospect_id, target, container_id,
                arguments, status, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (capability, provider, prospect_id, target, container_id,
             arguments, status, now()),
        )
        return int(cur.lastrowid)


def finish_job(job_id: int, *, status: str, result_summary: str | None = None,
               error: str | None = None) -> None:
    if status not in VALID_JOB_STATUSES:
        raise ValueError(f"invalid job status {status!r}; expected one of {VALID_JOB_STATUSES}")
    with connect() as conn:
        conn.execute(
            """UPDATE research_jobs
               SET status = ?, result_summary = ?, error = ?, completed_at = ?
               WHERE id = ?""",
            (status, result_summary, error, now(), job_id),
        )


def bump_job_attempts(job_id: int) -> int:
    """Count poll attempts so a wedged container can be timed out rather than
    polled forever on every cron tick."""
    with connect() as conn:
        conn.execute(
            "UPDATE research_jobs SET attempts = attempts + 1 WHERE id = ?", (job_id,))
        row = conn.execute(
            "SELECT attempts FROM research_jobs WHERE id = ?", (job_id,)).fetchone()
        return int(row["attempts"]) if row else 0


def list_open_jobs(capability: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
    with connect() as conn:
        if capability:
            cur = conn.execute(
                """SELECT * FROM research_jobs WHERE status = 'running'
                   AND capability = ? ORDER BY submitted_at LIMIT ?""",
                (capability, limit))
        else:
            cur = conn.execute(
                """SELECT * FROM research_jobs WHERE status = 'running'
                   ORDER BY submitted_at LIMIT ?""", (limit,))
        return list(cur.fetchall())


def get_job(job_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM research_jobs WHERE id = ?", (job_id,)).fetchone()


def has_open_job(capability: str, prospect_id: int) -> bool:
    """Prevents re-submitting work already in flight — the async equivalent of
    the `_has_pending_draft` guard in daily.py."""
    with connect() as conn:
        cur = conn.execute(
            """SELECT 1 FROM research_jobs
               WHERE status = 'running' AND capability = ? AND prospect_id = ?
               LIMIT 1""", (capability, prospect_id))
        return cur.fetchone() is not None


def list_jobs(status: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    with connect() as conn:
        if status:
            cur = conn.execute(
                "SELECT * FROM research_jobs WHERE status = ? "
                "ORDER BY submitted_at DESC LIMIT ?", (status, limit))
        else:
            cur = conn.execute(
                "SELECT * FROM research_jobs ORDER BY submitted_at DESC LIMIT ?",
                (limit,))
        return list(cur.fetchall())


# --- signals + evidence -----------------------------------------------------
#
# Signals record WHY we are contacting someone; evidence records HOW WE KNOW.
# Both are append-only. There are deliberately no update or delete helpers for
# evidence: a message we already sent must stay explainable by exactly the
# facts that were true when we sent it.


def create_signal(
    prospect_id: int,
    kind: str,
    confidence: str,
    dedup_key: str,
    *,
    payload: str | None = None,
    occurred_at: str | None = None,
    expires_at: str | None = None,
    status: str = "new",
) -> int | None:
    """Insert a signal. Returns the new row id, or None when `dedup_key`
    collides with an existing signal.

    A collision is the normal, expected outcome of re-scouting somebody we
    already know about — it means "already detected", not "error". Callers
    should skip, not raise. Mirrors record_message()'s external_id handling.
    """
    if confidence not in VALID_CONFIDENCE:
        raise ValueError(f"invalid confidence {confidence!r}; expected one of {VALID_CONFIDENCE}")
    if status not in VALID_SIGNAL_STATUSES:
        raise ValueError(f"invalid signal status {status!r}; expected one of {VALID_SIGNAL_STATUSES}")
    with connect() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO signals
                   (prospect_id, kind, status, confidence, detected_at,
                    occurred_at, expires_at, payload, dedup_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (prospect_id, kind, status, confidence, now(),
                 occurred_at, expires_at, payload, dedup_key),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None


def set_signal_status(signal_id: int, status: str) -> None:
    """The only mutable field on a signal — its lifecycle position."""
    if status not in VALID_SIGNAL_STATUSES:
        raise ValueError(f"invalid signal status {status!r}; expected one of {VALID_SIGNAL_STATUSES}")
    with connect() as conn:
        conn.execute("UPDATE signals SET status = ? WHERE id = ?", (status, signal_id))


def add_evidence(
    signal_id: int,
    claim: str,
    source_type: str,
    *,
    source_url: str | None = None,
    raw_excerpt: str | None = None,
    fetched_at: str | None = None,
) -> int:
    """Attach one supporting fact to a signal.

    `claim` is a short natural-language assertion ("Started as Founder at Acme
    in March 2026") because that is the string both the drafter and the
    grounding check consume. `raw_excerpt` holds the verbatim source text it
    was derived from; its checksum lets us detect later that the upstream
    source changed.
    """
    checksum = None
    if raw_excerpt is not None:
        checksum = hashlib.sha256(raw_excerpt.encode("utf-8")).hexdigest()[:16]
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO evidence
               (signal_id, claim, source_type, source_url, raw_excerpt, fetched_at, checksum)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (signal_id, claim, source_type, source_url, raw_excerpt,
             fetched_at or now(), checksum),
        )
        return int(cur.lastrowid)


def get_signal(signal_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()


def get_signal_by_dedup_key(dedup_key: str) -> sqlite3.Row | None:
    """Used after a create_signal() collision to report which signal already
    covers the event."""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM signals WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()


def list_evidence(signal_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM evidence WHERE signal_id = ? ORDER BY id", (signal_id,)
        )
        return list(cur.fetchall())


def get_signal_with_evidence(signal_id: int) -> tuple[sqlite3.Row, list[sqlite3.Row]] | None:
    """The audit view: a signal plus everything supporting it. Returns None if
    the signal does not exist."""
    signal = get_signal(signal_id)
    if signal is None:
        return None
    return signal, list_evidence(signal_id)


def list_signals(
    kind: str | None = None,
    status: str | None = None,
    prospect_id: int | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    clauses, params = [], []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if prospect_id is not None:
        clauses.append("prospect_id = ?")
        params.append(prospect_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        cur = conn.execute(
            f"SELECT * FROM signals {where} ORDER BY detected_at DESC LIMIT ?", params
        )
        return list(cur.fetchall())
