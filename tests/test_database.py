from pathlib import Path

from osu_pipeline import database


def test_insert_is_idempotent_and_counts(tmp_path: Path):
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    try:
        assert database.insert_replay(
            conn, path="a.osr", sha256="abc", size=10, mtime_ns=1,
            played_at="2026-09-05T00:00:00+00:00", day="2026-09-05",
        ) is True
        conn.commit()
        # duplicate (path, sha256) -> no-op
        assert database.insert_replay(
            conn, path="a.osr", sha256="abc", size=10, mtime_ns=1,
            played_at="2026-09-05T00:00:00+00:00", day="2026-09-05",
        ) is False
        # same path, new hash -> new row
        assert database.insert_replay(
            conn, path="a.osr", sha256="def", size=11, mtime_ns=2,
            played_at="2026-09-05T00:00:00+00:00", day="2026-09-05",
        ) is True
        conn.commit()
        counts = database.get_counts(conn)
        assert counts["discovered"] == 2
        assert counts["pending"] == 2
        day = database.get_day_summary(conn, "2026-09-05")
        assert day["total"] == 2
    finally:
        conn.close()


def test_restart_persistence(tmp_path: Path):
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    database.insert_replay(
        conn, path="x.osr", sha256="1", size=5, mtime_ns=1,
        played_at="2026-09-05T00:00:00+00:00", day="2026-09-05",
    )
    conn.commit()
    conn.close()
    # "restart": fresh connection sees the row
    conn2 = database.connect(db)
    try:
        assert database.replay_exists(conn2, "x.osr", "1") is True
        assert database.get_counts(conn2)["discovered"] == 1
    finally:
        conn2.close()
