"""Periodic replay scanner: NAS replay dir -> SQLite. Milestone 1 only.

Rules:
- Recursive scan for files whose suffix is exactly `.osr` (case-insensitive).
- Never touch `.part` / `.osr.part` / anything still transferring.
- Stability gate: skip zero-byte files and files modified more recently
  than `min_age_seconds` (marginal rsync-atomicity protection).
- Hash with SHA-256 (streamed) only after the stability gate passes.
- Identity = (relative POSIX path, sha256). Re-scans are idempotent no-ops.
- played_at/day come from the .osr timestamp via osrparse, normalized to UTC.
  Corrupt/unparseable files are still recorded (pending + error) so they are
  never silently dropped; day/ played_at stay NULL.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from . import database
from .models import STATUS_PENDING

log = logging.getLogger(__name__)

_CHUNK = 65536


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_played_at(osr_path: Path, fallback_mtime_ns: int) -> tuple[str | None, str | None, str | None]:
    """Return (played_at_iso, day, error). Falls back to file mtime (UTC) on parse failure."""
    try:
        from osrparse import Replay  # local import so tests can run without it until installed

        replay = Replay.from_path(osr_path)
        ts = replay.timestamp
        if isinstance(ts, datetime):
            dt = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
            dt_utc = dt.astimezone(timezone.utc)
        elif isinstance(ts, (int, float)):
            # Some forks expose ticks/ms; treat large ints as .NET ticks, else ms epoch.
            if ts > 1e14:  # .NET ticks (100ns since 0001-01-01)
                dt_utc = datetime(1, 1, 1, tzinfo=timezone.utc) + __import__("datetime").timedelta(microseconds=ts // 10)
            else:
                dt_utc = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        else:
            raise TypeError(f"unexpected osrparse timestamp type: {type(ts)}")
        played_at = dt_utc.isoformat()
        return played_at, dt_utc.date().isoformat(), None
    except Exception as exc:  # noqa: BLE001 - record-and-continue by design
        mtime = datetime.fromtimestamp(fallback_mtime_ns / 1e9, tz=timezone.utc)
        log.warning("osrparse failed for %s (%s); falling back to mtime", osr_path, exc)
        return mtime.isoformat(), mtime.date().isoformat(), f"timestamp_fallback: {exc}"


def is_stable_candidate(path: Path, stat_result, min_age_seconds: int, now_ns: int) -> bool:
    if stat_result.st_size == 0:
        return False
    age_ns = now_ns - stat_result.st_mtime_ns
    return age_ns >= min_age_seconds * 1_000_000_000


def scan_replays(
    replay_root: Path,
    db_path: Path,
    min_age_seconds: int = 5,
) -> dict:
    """One full scan pass. Returns stats dict. Safe to re-run; crash-safe."""
    replay_root = Path(replay_root)
    stats = {"found": 0, "new": 0, "skipped_unstable": 0, "parse_fallbacks": 0, "duplicates": 0}

    if not replay_root.exists():
        raise FileNotFoundError(f"Replay directory not found: {replay_root}")

    database.init_db(db_path)
    conn = database.connect(db_path)
    try:
        now_ns = time.time_ns()
        # Walk everything, filter suffix manually for case-insensitivity.
        for candidate in replay_root.rglob("*"):
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() != ".osr":
                continue  # ignores .part, .osr.part, etc.
            stats["found"] += 1

            try:
                st = candidate.stat()
            except OSError as exc:
                log.warning("stat failed for %s: %s", candidate, exc)
                stats["skipped_unstable"] += 1
                continue

            if not is_stable_candidate(candidate, st, min_age_seconds, now_ns):
                stats["skipped_unstable"] += 1
                continue

            digest = sha256_of(candidate)
            rel = candidate.relative_to(replay_root).as_posix()

            if database.replay_exists(conn, rel, digest):
                stats["duplicates"] += 1
                continue

            played_at, day, error = _extract_played_at(candidate, st.st_mtime_ns)
            if error:
                stats["parse_fallbacks"] += 1

            inserted = database.insert_replay(
                conn,
                path=rel,
                sha256=digest,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                played_at=played_at,
                day=day,
                status=STATUS_PENDING,
                error=error,
            )
            if inserted:
                stats["new"] += 1
                conn.commit()  # commit per insert: kill-safe, next scan resumes
            else:
                stats["duplicates"] += 1
    finally:
        conn.close()

    log.info("scan complete: %s", stats)
    return stats
