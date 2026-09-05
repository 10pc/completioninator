"""SQLite state database. Single-writer friendly, WAL mode, idempotent inserts."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS replays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    played_at TEXT,
    day TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    render_path TEXT,
    created_at TEXT NOT NULL,
    rendered_at TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE(path, sha256)
);
CREATE INDEX IF NOT EXISTS idx_replays_status_day ON replays(status, day);
CREATE INDEX IF NOT EXISTS idx_replays_day ON replays(day);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_replay(
    conn: sqlite3.Connection,
    *,
    path: str,
    sha256: str,
    size: int,
    mtime_ns: int,
    played_at: str | None,
    day: str | None,
    status: str = "pending",
    error: str | None = None,
) -> bool:
    """Insert one replay. Returns True if inserted, False if duplicate (path, sha256)."""
    try:
        conn.execute(
            """
            INSERT INTO replays
                (path, sha256, size, mtime_ns, played_at, day, status, created_at, error, attempts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (path, sha256, size, mtime_ns, played_at, day, status, _utcnow_iso(), error),
        )
        return True
    except sqlite3.IntegrityError:
        return False  # duplicate -> idempotent no-op


def replay_exists(conn: sqlite3.Connection, path: str, sha256: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM replays WHERE path = ? AND sha256 = ? LIMIT 1", (path, sha256)
    ).fetchone()
    return row is not None


def get_counts(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM replays GROUP BY status").fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    total = conn.execute("SELECT COUNT(*) AS n FROM replays").fetchone()["n"]
    counts["discovered"] = total
    for s in ("pending", "rendering", "rendered", "failed"):
        counts.setdefault(s, 0)
    return counts


def get_day_summary(conn: sqlite3.Connection, day: str) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM replays WHERE day = ? GROUP BY status", (day,)
    ).fetchall()
    summary = {r["status"]: r["n"] for r in rows}
    summary["total"] = sum(summary.values())
    return summary


def get_distinct_days(conn: sqlite3.Connection, limit: int = 10) -> list:
    rows = conn.execute(
        "SELECT day, COUNT(*) AS n FROM replays WHERE day IS NOT NULL "
        "GROUP BY day ORDER BY day DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [{"day": r["day"], "total": r["n"]} for r in rows]
