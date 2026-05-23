from __future__ import annotations

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
}


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, decl in columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _migrate(conn: sqlite3.Connection) -> None:
    _add_missing_columns(conn, "prospects", _PROSPECT_COLUMNS)
    _add_missing_columns(conn, "messages", _MESSAGE_COLUMNS)


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


def get_prospect_by_linkedin_url(linkedin_url: str) -> sqlite3.Row | None:
    with connect() as conn:
        cur = conn.execute("SELECT * FROM prospects WHERE linkedin_url = ?", (linkedin_url,))
        return cur.fetchone()


def set_pitch_context(prospect_id: int, pitch_context: str) -> None:
    """Force-overwrite pitch_context. upsert_prospect uses COALESCE so it
    won't replace an existing value; this is the explicit setter for callers
    that want the new value to win (e.g. funding-import refreshing stale
    funding details)."""
    with connect() as conn:
        conn.execute(
            "UPDATE prospects SET pitch_context = ? WHERE id = ?",
            (pitch_context, prospect_id),
        )


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
) -> int | None:
    """Insert a message row. Returns the new row id, or None if external_id
    collides (deduplication during polling)."""
    with connect() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO messages (prospect_id, direction, body, external_id, sent_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (prospect_id, direction, body, external_id, now()),
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
