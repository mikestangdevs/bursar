"""Bursar — shared state for the internal compute exchange.

SQLite in WAL mode at ``~/.hermes/bursar/bursar.db``. This is the single
source of truth that the dispatcher (writer), the cron settlement tick
(writer), and the Trading-Floor dashboard plugin (reader, tailing the
``events`` table over a WebSocket) all share. WAL lets the dashboard read
while the dispatcher writes, so the three surfaces cannot drift — the exact
mechanism ``hermes_cli/kanban_db`` uses.

Tables
------
queries           one row per query that entered the exchange (the order book).
ledger            one settlement row per serviced query (the metered bill).
budgets           per-team envelopes with a hard pre-execution cap.
events            append-only stream the dashboard WS tails for live updates.
market_snapshots  periodic rollups for the burn-down / bill-over-time charts.

Concurrency: WAL + ``BEGIN IMMEDIATE`` (see :func:`write_txn`) + a long
busy_timeout so SQLite serializes writers via the WAL lock rather than
raising ``database is locked``.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

DEFAULT_BUSY_TIMEOUT_MS = 10_000


def hermes_home() -> Path:
    """Resolve ~/.hermes, honoring HERMES_HOME like the rest of the repo."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def db_path() -> Path:
    """Location of bursar.db. Override with ``BURSAR_DB`` for tests."""
    env = os.environ.get("BURSAR_DB")
    if env:
        return Path(env).expanduser()
    return hermes_home() / "bursar" / "bursar.db"


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS queries (
    id            TEXT PRIMARY KEY,
    team          TEXT NOT NULL,
    tenant        TEXT NOT NULL DEFAULT 'local',  -- isolation boundary (org/deployment). PROCESS-level (BURSAR_TENANT), unlike team which is per-session. Dedup-SERVE never crosses it; default 'local' = one tenant (single-user reuse intact)
    source        TEXT NOT NULL DEFAULT 'demo',  -- 'demo' (firehose) | 'live' (real gate traffic)
    session_id    TEXT,                 -- Hermes conversation id (live rows; for dedup jump-links)
    turn_id       TEXT,                 -- Hermes turn id (live rows): all LLM calls of one user turn share it; groups a turn's rows exactly (NULL for demo/agentloop)
    prompt        TEXT NOT NULL,
    embedding     TEXT,                 -- packed float32 bytes, the dedup vector
    embed_model   TEXT,                 -- which embedding space this vector lives in (e.g. 'hash-256' | 'st-...-384'); only same-tag vectors are comparable
    value         REAL,                 -- business value score (0..100)
    tier          TEXT,                 -- trivial | standard | high_stakes
    rationale     TEXT,                 -- why the scorer assigned that value
    est_tokens    INTEGER,
    reasoning_tokens INTEGER,           -- reasoning/thinking tokens the real call spent (0/NULL if none/unknown)
    est_cost      REAL,                 -- predicted $ at the chosen model
    fee           REAL,                 -- internal trading fee ($)
    vpt           REAL,                 -- value-per-token rank key
    chosen_model  TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
                  -- pending|serviced|deduped|starved|queued|rejected
    dedup_of      TEXT,                 -- query.id this collapsed into (if deduped)
    temporal_class TEXT,                -- T-series: timeless|evolving|live|stateful (set on live re-ask decisions; NULL otherwise)
    reuse_mode    TEXT,                 -- T-series: 'comparison'|'plain' when this row reused a prior answer via augment; NULL otherwise
    result        TEXT,                 -- the answer text (for cache reuse)
    created_at    INTEGER NOT NULL,
    decided_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_queries_status  ON queries(status);
CREATE INDEX IF NOT EXISTS idx_queries_team     ON queries(team);
-- NOTE: idx_queries_tenant is NOT here — like idx_queries_source it is built in
-- _migrate() AFTER the tenant column is guaranteed to exist, so executescript on
-- a pre-tenant DB (CREATE TABLE IF NOT EXISTS is a no-op there) can't reference a
-- column that doesn't exist yet.
CREATE INDEX IF NOT EXISTS idx_queries_created  ON queries(created_at);

CREATE TABLE IF NOT EXISTS ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id        TEXT NOT NULL,
    team            TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'demo',  -- mirrors queries.source for source-scoped billing
    model           TEXT,
    tokens          INTEGER,
    token_cost      REAL,               -- $ spent on inference
    fee             REAL,               -- $ internal trading fee
    total           REAL,               -- token_cost + fee
    stripe_event_id TEXT,               -- meter event id (NULL if local-only)
    settled         INTEGER NOT NULL DEFAULT 0,  -- 1 once Stripe confirms
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_team ON ledger(team);

CREATE TABLE IF NOT EXISTS budgets (
    team        TEXT PRIMARY KEY,
    period      TEXT,                   -- e.g. "2026-sprint-12"
    cap         REAL NOT NULL,          -- hard pre-execution ceiling ($)
    spent       REAL NOT NULL DEFAULT 0,
    reset_at    INTEGER,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id    TEXT,
    kind        TEXT NOT NULL,
    payload     TEXT,                   -- JSON
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_id ON events(id);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    serviced        INTEGER NOT NULL DEFAULT 0,
    deduped         INTEGER NOT NULL DEFAULT 0,
    starved         INTEGER NOT NULL DEFAULT 0,
    queued          INTEGER NOT NULL DEFAULT 0,
    total_bill      REAL NOT NULL DEFAULT 0,
    total_saved     REAL NOT NULL DEFAULT 0,   -- $ avoided via dedup/starve
    created_at      INTEGER NOT NULL
);

-- Cross-process control flags. The dashboard backend (a separate process from
-- the agent PTY child) writes these; the live gate (bursar_gate, in the agent
-- process) reads them. The shared WAL DB is the only channel both processes
-- have, so a desktop UI toggle here flips agent-side enforcement without env
-- vars or a restart. Currently: key='enforce' value='1'|'0'.
CREATE TABLE IF NOT EXISTS control (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  INTEGER NOT NULL
);

-- Cache-the-work: which files a turn READ, with a content hash, so a later
-- re-ask can verify freshness OUT OF BAND (re-hash the file, 0 LLM tokens)
-- instead of having the agent re-read it. Keyed by turn_id (the live gate's
-- stable turn identity). The big saving the answer-cache alone can't get: an
-- agent re-reading an unchanged file pays full tokens to re-process it; here
-- Bursar can tell the agent "unchanged — don't re-read" with proof.
CREATE TABLE IF NOT EXISTS work_cache (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id      TEXT NOT NULL,
    session_id   TEXT,
    tool_name    TEXT NOT NULL,      -- 'read_file' | 'terminal' | 'search_files' | 'mcp_read'
    path         TEXT,               -- resolved absolute path (file-read tools)
    content_hash TEXT,               -- sha256 of the file's bytes at read time
    mtime        REAL,               -- st_mtime at read time (H8 fast pre-check)
    size         INTEGER,            -- st_size at read time (H8 fast pre-check)
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_cache_turn ON work_cache(turn_id);
-- content_hash index (H9): find the same bytes under a new path on rename/move.
CREATE INDEX IF NOT EXISTS idx_work_cache_hash ON work_cache(content_hash);
"""


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------

_INITIALIZED: set[str] = set()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for DBs created before a column existed.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing table, so a new
    column in SCHEMA_SQL never reaches a DB from a prior version. Each entry
    here is an idempotent ``ADD COLUMN`` guarded by a column-presence check;
    the ``DEFAULT`` backfills existing rows (all pre-source rows are firehose
    traffic, hence 'demo'). Safe to run on every init."""
    add_if_missing = {
        # session_id = the Hermes conversation a live query belongs to, so a
        # dedup hit can deep-link back to where the original answer lives
        # (the desktop session route is /<session_id>). NULL for demo rows.
        "queries": [("source", "TEXT NOT NULL DEFAULT 'demo'"), ("session_id", "TEXT"),
                    # reasoning_tokens: the thinking tokens the real call spent, so a
                    # dedup hit can say "you already paid to think about this once."
                    ("reasoning_tokens", "INTEGER"),
                    # embed_model: the embedding space a row's vector lives in, so the
                    # dedup window never cosines a 256-dim hashing vector against a
                    # 384-dim semantic one. Legacy rows predate semantic dedup → all
                    # hashing, backfilled below.
                    ("embed_model", "TEXT"),
                    # turn_id: groups every LLM call of one user turn (live rows),
                    # so a turn's cost/answer summary is exact even when the prompt
                    # key would otherwise drift mid-turn. NULL for demo/agentloop.
                    ("turn_id", "TEXT"),
                    # T-series temporal router: the per-query temporal class
                    # (timeless|evolving|live|stateful), set on live re-ask
                    # decisions, and the reuse outcome ('comparison'|'plain') when
                    # the row reused a prior answer via augment. Both NULL on plain
                    # first-asks / demo rows, so the floor can badge only real reuse.
                    ("temporal_class", "TEXT"), ("reuse_mode", "TEXT"),
                    # tenant: the isolation boundary (org/deployment), set PROCESS-wide
                    # from BURSAR_TENANT (default 'local'). Unlike team (per-session,
                    # so it can't gate cross-session reuse), tenant is the unit dedup
                    # must never SERVE across — team B's reworded question can't be
                    # handed team A's cached answer. Legacy rows backfill to 'local'
                    # via the column DEFAULT, keeping single-tenant reuse intact.
                    ("tenant", "TEXT NOT NULL DEFAULT 'local'")],
        "ledger": [("source", "TEXT NOT NULL DEFAULT 'demo'")],
        # mtime+size let the freshness check skip a full re-hash when a stat shows
        # the file is byte-for-byte the same shape (H8). NULL on pre-H8 rows → the
        # check falls back to hashing, which is correct, just not fast.
        "work_cache": [("mtime", "REAL"), ("size", "INTEGER")],
    }
    for table, cols in add_if_missing.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    # Built here (not SCHEMA_SQL) so it also lands on a migrated pre-H9 DB.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_cache_hash ON work_cache(content_hash)")
    # Backfill: any row without an embed_model tag predates semantic dedup, so its
    # vector is a hashing vector. Tag it so the same-space filter still finds it.
    conn.execute(
        "UPDATE queries SET embed_model = ? WHERE embed_model IS NULL AND embedding IS NOT NULL",
        (f"hash-256",),
    )
    # Indexes on the source column live here (not SCHEMA_SQL) so they're built
    # only after the column is guaranteed to exist on a migrated old DB.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_queries_source ON queries(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_source ON ledger(source)")
    # Built here too (not only SCHEMA_SQL) so it lands on a migrated old DB once the
    # tenant column is guaranteed to exist.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_queries_tenant ON queries(tenant)")
    conn.commit()


def connect() -> sqlite3.Connection:
    """Open (initializing on first use) the bursar DB in WAL mode.

    Idempotent: the schema is created once per process per path. WAL +
    busy_timeout let the dashboard reader coexist with the dispatcher
    writer without ``database is locked`` errors.
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    resolved = str(path.resolve())
    if resolved not in _INITIALIZED:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        _migrate(conn)
        _INITIALIZED.add(resolved)
    # N6 — opt-in retention sweep. No-op (zero overhead) unless
    # BURSAR_RETENTION_DAYS>0; throttled internally so this hot path is cheap.
    maybe_purge_expired(conn)
    return conn


def init_db() -> Path:
    """Create the schema if needed; return the path used."""
    conn = connect()
    try:
        return db_path()
    finally:
        conn.close()


@contextlib.contextmanager
def connect_closing():
    """Open a connection and guarantee it is closed (avoids FD leaks in
    long-lived processes that route every op through ``connect()``)."""
    conn = connect()
    try:
        yield conn
    finally:
        with contextlib.suppress(Exception):
            conn.close()


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection):
    """An IMMEDIATE write transaction. SQLite serializes writers via the
    WAL lock, so a check-then-write inside this block is atomic against
    concurrent dispatcher/cron writers."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --------------------------------------------------------------------------
# IDs + helpers
# --------------------------------------------------------------------------

def new_query_id() -> str:
    # 8 random bytes (1.8e19 space): birthday-collision probability stays
    # negligible even at 500k ids/run. 4 bytes (~4.3e9) collides ~once at
    # 100k and ~29x at 500k — surfaced by the scale bench.
    return "q_" + secrets.token_hex(8)


def now() -> int:
    return int(time.time())


# --------------------------------------------------------------------------
# N6 — retention. Prompts/answers persist plaintext in the DB; at scale that
# wants a retention policy. Default OFF (the demo never deletes): purge only runs
# when BURSAR_RETENTION_DAYS > 0. Encryption-at-rest is a DEPLOYMENT concern
# (SQLCipher / an encrypted volume), not implemented here — see SCALE-HARDENING.md.
# We deliberately do NOT redact the stored prompt/answer: the answer text IS the
# cache, so redacting it would corrupt dedup-serve. Redaction belongs at the
# ingest/logging layer, never on the reusable cache.
# --------------------------------------------------------------------------
_LAST_PURGE_MONOTONIC = 0.0
_PURGE_LOCK = threading.Lock()


def retention_days() -> int:
    """Configured retention window in days (``BURSAR_RETENTION_DAYS``); 0/unset =
    keep forever (default). Never raises."""
    try:
        return max(0, int(os.environ.get("BURSAR_RETENTION_DAYS", "0") or "0"))
    except ValueError:
        return 0


def purge_expired(conn: sqlite3.Connection, *, days: Optional[int] = None,
                  source: Optional[str] = "live") -> dict:
    """Delete rows older than the retention window, plus their ledger and events.

    ``days`` defaults to ``retention_days()``; ``<= 0`` is a no-op. ``source``
    scopes the purge ('live' by default so the synthetic firehose demo data is
    untouched; ``None`` purges every source). Dependent rows (ledger, events) are
    deleted BEFORE the queries they reference, all in one IMMEDIATE transaction.
    Returns the per-table deleted counts."""
    d = retention_days() if days is None else max(0, int(days))
    if d <= 0:
        return {"queries": 0, "ledger": 0, "events": 0}
    cutoff = now() - d * 86400
    src_clause = "" if source is None else " AND source = ?"
    sub = f"SELECT id FROM queries WHERE created_at < ?{src_clause}"
    params = [cutoff] + ([source] if source is not None else [])
    with write_txn(conn):
        led = conn.execute(f"DELETE FROM ledger WHERE query_id IN ({sub})", params).rowcount
        ev = conn.execute(f"DELETE FROM events WHERE query_id IN ({sub})", params).rowcount
        q = conn.execute(
            f"DELETE FROM queries WHERE created_at < ?{src_clause}", params).rowcount
    return {"queries": int(q or 0), "ledger": int(led or 0), "events": int(ev or 0)}


def maybe_purge_expired(conn: sqlite3.Connection) -> None:
    """Opt-in, throttled retention sweep for on-connect / periodic callers.

    No-op unless ``BURSAR_RETENTION_DAYS > 0``; then runs at most once per
    ``BURSAR_RETENTION_SWEEP_S`` (default 1h) per process, so wiring it into the
    hot ``connect()`` path can't run a delete on every gate call. Never raises —
    retention housekeeping must never break a turn (fail-open)."""
    global _LAST_PURGE_MONOTONIC
    if retention_days() <= 0:
        return
    try:
        sweep_s = float(os.environ.get("BURSAR_RETENTION_SWEEP_S", "3600") or "3600")
    except ValueError:
        sweep_s = 3600.0
    mono = time.monotonic()
    with _PURGE_LOCK:
        if mono - _LAST_PURGE_MONOTONIC < sweep_s:
            return
        _LAST_PURGE_MONOTONIC = mono
    try:
        purge_expired(conn)
    except Exception:
        pass


def emit_event(
    conn: sqlite3.Connection,
    kind: str,
    *,
    query_id: Optional[str] = None,
    payload: Optional[dict] = None,
) -> int:
    """Append an event row (the stream the dashboard WS tails).

    Call inside an open ``write_txn`` when part of a larger write, or
    standalone (it autocommits) for fire-and-forget signals.
    """
    cur = conn.execute(
        "INSERT INTO events (query_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        (query_id, kind, json.dumps(payload) if payload is not None else None, now()),
    )
    return int(cur.lastrowid)


def record_work(conn: sqlite3.Connection, *, turn_id: str, session_id: Optional[str],
                tool_name: str, path: Optional[str], content_hash: Optional[str],
                mtime: Optional[float] = None, size: Optional[int] = None) -> None:
    """Record that a turn read *path* with content *content_hash* (+ mtime/size for
    the H8 fast pre-check). Idempotent per (turn_id, path): a turn that reads the
    same file in chunks records it once, refreshing the hash. Autocommits."""
    if not turn_id or not tool_name:
        return
    if path is not None:
        conn.execute(
            "DELETE FROM work_cache WHERE turn_id=? AND path IS ?", (turn_id, path)
        )
    conn.execute(
        "INSERT INTO work_cache (turn_id, session_id, tool_name, path, content_hash, "
        "mtime, size, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (turn_id, session_id, tool_name, path, content_hash, mtime, size, now()),
    )
    conn.commit()


def work_for_turn(conn: sqlite3.Connection, turn_id: str) -> list[dict]:
    """The files (path + hash + mtime/size) a turn read — its 'work', for freshness."""
    if not turn_id:
        return []
    rows = conn.execute(
        "SELECT tool_name, path, content_hash, mtime, size FROM work_cache "
        "WHERE turn_id=? AND path IS NOT NULL ORDER BY id",
        (turn_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def paths_with_hash(conn: sqlite3.Connection, content_hash: str,
                    *, limit: int = 8) -> list[str]:
    """Distinct paths ever recorded with this exact content (H9 rename/move): if a
    tracked path is now gone, the same bytes may live under one of these. Most
    recent first; the caller still verifies the candidate exists + re-hashes."""
    if not content_hash:
        return []
    rows = conn.execute(
        "SELECT path, MAX(id) m FROM work_cache WHERE content_hash=? AND path IS NOT NULL "
        "GROUP BY path ORDER BY m DESC LIMIT ?",
        (content_hash, int(limit)),
    ).fetchall()
    return [r["path"] for r in rows]


def prune_work_cache(conn: sqlite3.Connection, *, keep_seconds: Optional[int] = None,
                     max_rows: Optional[int] = None) -> int:
    """Bound work_cache growth (H7). Drops rows older than *keep_seconds* (tie this
    to the dedup recency window — freshness for a turn that can no longer be matched
    is dead weight), then enforces a hard *max_rows* cap (oldest-id first). Returns
    rows deleted. Autocommits. Fail-safe: never touches in-window data."""
    deleted = 0
    if keep_seconds is not None and keep_seconds > 0:
        cur = conn.execute("DELETE FROM work_cache WHERE created_at < ?",
                           (now() - int(keep_seconds),))
        deleted += cur.rowcount or 0
    if max_rows is not None and max_rows > 0:
        total = conn.execute("SELECT COUNT(*) c FROM work_cache").fetchone()["c"]
        if total > max_rows:
            cur = conn.execute(
                "DELETE FROM work_cache WHERE id IN ("
                "SELECT id FROM work_cache ORDER BY id ASC LIMIT ?)",
                (total - max_rows,),
            )
            deleted += cur.rowcount or 0
    if deleted:
        conn.commit()
    return deleted


def events_since(conn: sqlite3.Connection, cursor: int, limit: int = 200) -> tuple[int, list[dict]]:
    """Return (new_cursor, rows) for events with id > cursor. Mirrors the
    kanban ``_fetch_new`` tail used by the dashboard WebSocket."""
    rows = conn.execute(
        "SELECT id, query_id, kind, payload, created_at FROM events "
        "WHERE id > ? ORDER BY id ASC LIMIT ?",
        (cursor, limit),
    ).fetchall()
    out: list[dict] = []
    new_cursor = cursor
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except Exception:
            payload = None
        out.append(
            {
                "id": r["id"],
                "query_id": r["query_id"],
                "kind": r["kind"],
                "payload": payload,
                "created_at": r["created_at"],
            }
        )
        new_cursor = r["id"]
    return new_cursor, out


# --------------------------------------------------------------------------
# Control flags (cross-process: dashboard writes, live gate reads)
# --------------------------------------------------------------------------

def get_control(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    """Read a control flag. Returns ``default`` when the key is unset."""
    row = conn.execute("SELECT value FROM control WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def set_control(conn: sqlite3.Connection, key: str, value: Optional[str]) -> None:
    """Upsert a control flag (autocommits). Stored as text; ``None`` clears it
    to NULL rather than deleting the row, so readers see a stable key."""
    conn.execute(
        "INSERT INTO control (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now()),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------

def upsert_budget(
    conn: sqlite3.Connection,
    team: str,
    cap: float,
    *,
    period: Optional[str] = None,
    reset_at: Optional[int] = None,
    reset_spent: bool = False,
) -> None:
    """Create or update a team's budget envelope. ``reset_spent`` zeroes the
    consumed amount (period rollover)."""
    with write_txn(conn):
        existing = conn.execute("SELECT spent FROM budgets WHERE team = ?", (team,)).fetchone()
        spent = 0.0 if (existing is None or reset_spent) else existing["spent"]
        conn.execute(
            "INSERT INTO budgets (team, period, cap, spent, reset_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(team) DO UPDATE SET cap=excluded.cap, period=excluded.period, "
            "spent=excluded.spent, reset_at=excluded.reset_at, updated_at=excluded.updated_at",
            (team, period, float(cap), float(spent), reset_at, now()),
        )


def get_budget(conn: sqlite3.Connection, team: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM budgets WHERE team = ?", (team,)).fetchone()
    return dict(row) if row else None


def list_budgets(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM budgets ORDER BY team")]


# --------------------------------------------------------------------------
# Convenience read helpers used by the dashboard API
# --------------------------------------------------------------------------

def list_queries(conn: sqlite3.Connection, *, status: Optional[str] = None,
                 source: Optional[str] = None, limit: int = 500) -> list[dict]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if source:
        clauses.append("source = ?")
        params.append(source)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM queries{where} ORDER BY created_at DESC LIMIT ?", params
    ).fetchall()
    return [dict(r) for r in rows]


def list_ledger(conn: sqlite3.Connection, *, team: Optional[str] = None,
                source: Optional[str] = None, limit: int = 500) -> list[dict]:
    clauses, params = [], []
    if team:
        clauses.append("team = ?")
        params.append(team)
    if source:
        clauses.append("source = ?")
        params.append(source)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM ledger{where} ORDER BY id DESC LIMIT ?", params
    ).fetchall()
    return [dict(r) for r in rows]


def list_snapshots(conn: sqlite3.Connection, *, limit: int = 500) -> list[dict]:
    """Return market snapshots oldest-first (chronological), capped at the
    most recent ``limit`` rows — the time series the dashboard plots for the
    burn-down / bill-over-time charts. ``starved`` is the rejected-lane count
    (named for the trading metaphor: starved of budget/value)."""
    rows = conn.execute(
        "SELECT * FROM (SELECT * FROM market_snapshots ORDER BY id DESC LIMIT ?) "
        "ORDER BY id ASC",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    p = init_db()
    print(f"bursar.db initialized at {p}")
