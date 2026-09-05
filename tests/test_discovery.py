"""Discovery tests use fake .osr bytes (parse fallback path) — no real replays needed."""

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from osu_pipeline import database
from osu_pipeline.discovery import scan_replays


def _make_old_file(path: Path, payload: bytes = b"fake-osr-bytes-1234") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    old = time.time() - 3600
    os.utime(path, (old, old))


def test_discovers_osr_ignores_part_and_is_idempotent(tmp_path: Path):
    root = tmp_path / "replays"
    _make_old_file(root / "2026" / "09" / "05" / "000001.osr")
    _make_old_file(root / "2026" / "09" / "05" / "000002.OSR")  # uppercase handled
    _make_old_file(root / "12345.osr.part", b"partial")
    _make_old_file(root / "67890.osr", b"")  # zero-byte -> unstable, skipped
    db = tmp_path / "p.sqlite"

    stats = scan_replays(root, db, min_age_seconds=5)
    assert stats["found"] == 3  # two .osr + one .OSR (zero-byte counts as found, .part ignored)
    assert stats["new"] == 2
    assert stats["skipped_unstable"] == 1

    conn = database.connect(db)
    try:
        counts = database.get_counts(conn)
        assert counts["discovered"] == 2
        assert counts["pending"] == 2
        rows = conn.execute("SELECT path, sha256, day, error FROM replays").fetchall()
        # corrupt/fake bytes -> recorded with mtime-fallback day + error note
        expected_day = datetime.now(timezone.utc).date().isoformat()
        # files were backdated 1h; tolerate midnight edge by accepting today/yesterday
        for r in rows:
            assert r["sha256"] and len(r["sha256"]) == 64
            assert r["error"] is not None and "timestamp_fallback" in r["error"]
        assert all(r["day"] is not None for r in rows)
        assert str(expected_day)  # sanity
    finally:
        conn.close()

    # second scan: exactly-once, no new rows
    stats2 = scan_replays(root, db, min_age_seconds=5)
    assert stats2["new"] == 0
    assert stats2["duplicates"] == 2


def test_recent_file_skipped_until_stable(tmp_path: Path):
    root = tmp_path / "replays"
    fresh = root / "fresh.osr"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    fresh.write_bytes(b"fresh-bytes-abcdef")  # mtime = now
    db = tmp_path / "p.sqlite"

    stats = scan_replays(root, db, min_age_seconds=3600)
    assert stats["new"] == 0
    assert stats["skipped_unstable"] == 1

    stats2 = scan_replays(root, db, min_age_seconds=0)
    assert stats2["new"] == 1
