"""SQLite state database. Single-writer friendly, WAL mode, idempotent inserts."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
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
    claimed_at TEXT,
    UNIQUE(path, sha256)
);
CREATE INDEX IF NOT EXISTS idx_replays_status_day ON replays(status, day);
CREATE INDEX IF NOT EXISTS idx_replays_day ON replays(day);
CREATE TABLE IF NOT EXISTS daily (
    day TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    clips INTEGER NOT NULL,
    seconds REAL,
    span TEXT,
    passed TEXT,
    left TEXT,
    pct TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    day TEXT NOT NULL,
    platform TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    remote_id TEXT,
    remote_url TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    uploaded_at TEXT,
    PRIMARY KEY (day, platform)
);
CREATE TABLE IF NOT EXISTS daily_clips (
    day TEXT NOT NULL,
    replay_id INTEGER NOT NULL,
    PRIMARY KEY (day, replay_id)
);
"""

# Milestone 2 columns; applied idempotently to M1 databases.
MIGRATIONS = [
    ("replays", "ALTER TABLE replays ADD COLUMN beatmap_hash TEXT"),
    ("replays", "ALTER TABLE replays ADD COLUMN beatmapset_id INTEGER"),
    ("replays", "ALTER TABLE replays ADD COLUMN claimed_at TEXT"),
    ("daily", "ALTER TABLE daily ADD COLUMN passed TEXT"),
    ("daily", "ALTER TABLE daily ADD COLUMN left TEXT"),
    ("daily", "ALTER TABLE daily ADD COLUMN pct TEXT"),
]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=10000;")  # concurrent workers wait instead of SQLITE_BUSY
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        for table, stmt in MIGRATIONS:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            col = stmt.split("ADD COLUMN")[1].strip().split()[0]
            if col not in existing:
                conn.execute(stmt)
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
    beatmap_hash: str | None = None,
) -> bool:
    """Insert one replay. Returns True if inserted, False if duplicate (path, sha256)."""
    try:
        conn.execute(
            """
            INSERT INTO replays
                (path, sha256, size, mtime_ns, played_at, day, status, created_at, error, attempts, beatmap_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (path, sha256, size, mtime_ns, played_at, day, status, _utcnow_iso(), error, beatmap_hash),
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
    for s in ("pending", "rendering", "rendered", "failed", "unrenderable", "excluded",
              "composited"):
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


# --- Render queue (Milestone 2) ---

def claim_pending(conn: sqlite3.Connection, retries: int = 10) -> dict | None:
    """Atomically claim the oldest pending replay. Returns the row, or None if empty.

    Lost races (two workers selecting the same row) are retried internally,
    so None reliably means the queue is empty — callers can stop on it.
    Stamps claimed_at so a later run can tell live claims from stale ones.
    """
    import time

    for _ in range(max(1, retries)):
        row = conn.execute(
            "SELECT * FROM replays WHERE status = 'pending' ORDER BY day, id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE replays SET status = 'rendering', attempts = attempts + 1, "
            "claimed_at = ? WHERE id = ? AND status = 'pending'",
            (_utcnow_iso(), row["id"]),
        )
        conn.commit()
        if cur.rowcount:
            return dict(conn.execute("SELECT * FROM replays WHERE id = ?", (row["id"],)).fetchone())
        time.sleep(0.05)
    return None


def reset_stale_rendering(conn: sqlite3.Connection, older_than_minutes: float = 180) -> int:
    """Return interrupted `rendering` jobs to `pending` — but only ones whose
    claim is older than the cutoff (or never stamped, i.e. pre-migration rows).

    A blind reset double-renders jobs another live worker/container started
    minutes ago; the age gate means only genuinely dead claims come back.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
    cur = conn.execute(
        "UPDATE replays SET status = 'pending' WHERE status = 'rendering' "
        "AND (claimed_at IS NULL OR claimed_at < ?)",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


def requeue_failed(conn: sqlite3.Connection) -> int:
    """Return `failed` jobs to `pending` so they can be retried after a fix."""
    cur = conn.execute("UPDATE replays SET status = 'pending', error = NULL WHERE status = 'failed'")
    conn.commit()
    return cur.rowcount


def requeue_one(conn: sqlite3.Connection, replay_id: int, error: str) -> None:
    """Return one job to `pending` with a note (transient failure: retry soon)."""
    conn.execute(
        "UPDATE replays SET status = 'pending', error = ? WHERE id = ?", (error, replay_id)
    )
    conn.commit()


def find_staging_rows(conn: sqlite3.Connection, staging: str = "staging") -> list:
    """Rows indexed while their file sat in the sync landing zone (any depth)."""
    rows = conn.execute(
        "SELECT id, path, sha256, status, attempts, render_path FROM replays "
        "WHERE path LIKE ? ORDER BY id",
        (f"%{staging}%",),
    ).fetchall()
    return [dict(r) for r in rows if staging in Path(r["path"]).parts[:-1]]


def repoint_replay(conn: sqlite3.Connection, replay_id: int, new_path: str, error: str) -> None:
    """Move a row to its post-sync path and release it back to pending."""
    conn.execute(
        "UPDATE replays SET path = ?, status = 'pending', attempts = 0, error = ? WHERE id = ?",
        (new_path, error, replay_id),
    )
    conn.commit()


def delete_replay(conn: sqlite3.Connection, replay_id: int) -> None:
    conn.execute("DELETE FROM replays WHERE id = ?", (replay_id,))
    conn.commit()


def get_day_jobs(conn: sqlite3.Connection, day: str, status: str) -> list:
    """Rows for one day+status (progress overview, failure triage)."""
    rows = conn.execute(
        "SELECT id, path, attempts, beatmapset_id, render_path, error FROM replays "
        "WHERE day = ? AND status = ? ORDER BY id",
        (day, status),
    ).fetchall()
    return [dict(r) for r in rows]


def set_beatmap(conn: sqlite3.Connection, replay_id: int, beatmap_hash: str | None, beatmapset_id: int | None) -> None:
    conn.execute(
        "UPDATE replays SET beatmap_hash = ?, beatmapset_id = ? WHERE id = ?",
        (beatmap_hash, beatmapset_id, replay_id),
    )
    conn.commit()


def mark_rendered(conn: sqlite3.Connection, replay_id: int, render_path: str) -> None:
    conn.execute(
        "UPDATE replays SET status = 'rendered', render_path = ?, rendered_at = ?, error = NULL "
        "WHERE id = ?",
        (render_path, _utcnow_iso(), replay_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, replay_id: int, error: str) -> None:
    conn.execute(
        "UPDATE replays SET status = 'failed', error = ? WHERE id = ?", (error, replay_id)
    )
    conn.commit()


def mark_unrenderable(conn: sqlite3.Connection, replay_id: int, error: str) -> None:
    """Terminal skip: never claimed, never requeued (e.g. map updated since play)."""
    conn.execute(
        "UPDATE replays SET status = 'unrenderable', error = ? WHERE id = ?",
        (error, replay_id),
    )
    conn.commit()


def exclude_before(conn: sqlite3.Connection, day: str, dry_run: bool = False) -> list[dict]:
    """Park rendered/composited rows older than a day as excluded (testing eras,
    replays you never want in a grid again). Never touches pending/failed/etc.
    Returns the affected rows (without committing when dry_run)."""
    rows = conn.execute(
        "SELECT id, path, day, status FROM replays "
        "WHERE day < ? AND status IN ('rendered', 'composited') ORDER BY day, id",
        (day,),
    ).fetchall()
    out = [dict(r) for r in rows]
    if not dry_run and out:
        placeholders = ",".join("?" for _ in out)
        conn.execute(
            f"UPDATE replays SET status = 'excluded' WHERE id IN ({placeholders})",
            [r["id"] for r in out],
        )
        conn.commit()
    return out


# --- Daily batches (Milestone 4) ---

def get_uncomposited(conn: sqlite3.Connection) -> list:
    """Rendered clips not yet in any daily video, oldest day first (rolling batch)."""
    rows = conn.execute(
        "SELECT id, path, day, render_path FROM replays "
        "WHERE status = 'rendered' AND render_path IS NOT NULL ORDER BY day, id"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_composited(conn: sqlite3.Connection, replay_ids: list[int], day: str | None = None) -> None:
    if not replay_ids:
        return
    placeholders = ",".join("?" for _ in replay_ids)
    conn.execute(
        f"UPDATE replays SET status = 'composited' WHERE id IN ({placeholders})", replay_ids
    )
    if day is not None:
        conn.executemany(
            "INSERT OR IGNORE INTO daily_clips (day, replay_id) VALUES (?, ?)",
            [(day, rid) for rid in replay_ids],
        )
    conn.commit()


def clips_of_day(conn: sqlite3.Connection, day: str) -> list[dict]:
    """Render files mapped to a daily video (for post-upload pruning)."""
    rows = conn.execute(
        "SELECT r.id, r.render_path FROM daily_clips dc JOIN replays r ON r.id = dc.replay_id "
        "WHERE dc.day = ?",
        (day,),
    ).fetchall()
    return [dict(r) for r in rows]


def composited_clips(conn: sqlite3.Connection) -> list[dict]:
    """All composited clips with render files (backfill + audits)."""
    rows = conn.execute(
        "SELECT id, day, render_path FROM replays "
        "WHERE status = 'composited' AND render_path IS NOT NULL ORDER BY day, id"
    ).fetchall()
    return [dict(r) for r in rows]


def max_uploaded_day(conn: sqlite3.Connection, platform: str = "youtube") -> str | None:
    row = conn.execute(
        "SELECT MAX(day) AS m FROM uploads WHERE platform = ? AND status = 'uploaded'",
        (platform,),
    ).fetchone()
    return row["m"] if row else None


def record_daily(conn: sqlite3.Connection, day: str, path: str, clips: int,
                 seconds: float | None, span: str | None,
                 stats: dict | None = None) -> None:
    """Record a finished daily video, including the completion snapshot baked in."""
    stats = stats or {}
    conn.execute(
        "INSERT INTO daily (day, path, clips, seconds, span, passed, left, pct, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(day) DO UPDATE SET path=excluded.path, clips=excluded.clips, "
        "seconds=excluded.seconds, span=excluded.span, passed=excluded.passed, "
        "left=excluded.left, pct=excluded.pct, created_at=excluded.created_at",
        (day, path, clips, seconds, span,
         stats.get("passed"), stats.get("left"), stats.get("pct"), _utcnow_iso()),
    )
    conn.commit()


def get_latest_daily(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM daily ORDER BY day DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# --- Uploads (Milestone 6) ---

def get_upload(conn: sqlite3.Connection, day: str, platform: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM uploads WHERE day = ? AND platform = ?", (day, platform)
    ).fetchone()
    return dict(row) if row else None


def mark_uploading(conn: sqlite3.Connection, day: str, platform: str) -> None:
    conn.execute(
        "INSERT INTO uploads (day, platform, status, created_at) VALUES (?, ?, 'uploading', ?) "
        "ON CONFLICT(day, platform) DO UPDATE SET status='uploading', error=NULL",
        (day, platform, _utcnow_iso()),
    )
    conn.commit()


def mark_uploaded(conn: sqlite3.Connection, day: str, platform: str,
                  remote_id: str, remote_url: str) -> None:
    conn.execute(
        "INSERT INTO uploads (day, platform, status, remote_id, remote_url, uploaded_at, created_at) "
        "VALUES (?, ?, 'uploaded', ?, ?, ?, ?) "
        "ON CONFLICT(day, platform) DO UPDATE SET status='uploaded', remote_id=excluded.remote_id, "
        "remote_url=excluded.remote_url, uploaded_at=excluded.uploaded_at, error=NULL",
        (day, platform, remote_id, remote_url, _utcnow_iso(), _utcnow_iso()),
    )
    conn.commit()


def mark_upload_failed(conn: sqlite3.Connection, day: str, platform: str, error: str) -> None:
    conn.execute(
        "INSERT INTO uploads (day, platform, status, error, created_at) VALUES (?, ?, 'failed', ?, ?) "
        "ON CONFLICT(day, platform) DO UPDATE SET status='failed', error=excluded.error",
        (day, platform, error, _utcnow_iso()),
    )
    conn.commit()


def recent_uploads(conn: sqlite3.Connection, limit: int = 5) -> list:
    rows = conn.execute(
        "SELECT day, platform, status, remote_url FROM uploads "
        "ORDER BY created_at DESC LIMIT ?", (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def pending_upload_day(conn: sqlite3.Connection, platform: str) -> str | None:
    """Latest daily video with no successful upload on this platform (backlog retry)."""
    row = conn.execute(
        "SELECT d.day FROM daily d LEFT JOIN uploads u "
        "ON u.day = d.day AND u.platform = ? AND u.status = 'uploaded' "
        "WHERE u.day IS NULL ORDER BY d.day DESC LIMIT 1",
        (platform,),
    ).fetchone()
    return row["day"] if row else None
