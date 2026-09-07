"""Exclude command tests: parks old eras without touching live states."""

from pathlib import Path

from osu_pipeline import database
from osu_pipeline.cli import main


def _seed(db: Path, day: str, status: str, name: str) -> None:
    database.init_db(db)
    conn = database.connect(db)
    database.insert_replay(
        conn, path=f"{name}.osr", sha256=name, size=11, mtime_ns=1,
        played_at=f"{day}T00:00:00+00:00", day=day, status=status)
    if status in ("rendered", "composited"):
        jid = conn.execute("SELECT id FROM replays WHERE path = ?",
                           (f"{name}.osr",)).fetchone()["id"]
        conn.execute("UPDATE replays SET render_path = ? WHERE id = ?",
                     (f"/renders/{name}.mp4", jid))
    conn.commit()
    conn.close()


def _cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f"database = {str(tmp_path / 'p.sqlite')!r}\n"
    )
    return cfg


def test_exclude_dry_run_changes_nothing(tmp_path: Path, capsys):
    db = tmp_path / "p.sqlite"
    _seed(db, "2026-08-01", "rendered", "old")
    _seed(db, "2026-09-06", "rendered", "new")
    _seed(db, "2026-08-01", "pending", "pend")
    _seed(db, "2026-08-01", "failed", "fail")
    cfg = _cfg(tmp_path)
    assert main(["--config", str(cfg), "exclude", "--before", "2026-09-01", "--dry-run"]) == 0
    assert "would exclude 1 replays" in capsys.readouterr().out
    conn = database.connect(db)
    try:
        counts = database.get_counts(conn)
        assert counts["rendered"] == 2 and counts["excluded"] == 0
    finally:
        conn.close()


def test_exclude_parks_only_rendered_and_composited(tmp_path: Path, capsys):
    db = tmp_path / "p.sqlite"
    _seed(db, "2026-08-01", "rendered", "old")
    _seed(db, "2026-08-01", "composited", "comp")
    _seed(db, "2026-09-06", "rendered", "new")
    _seed(db, "2026-08-01", "pending", "pend")
    _seed(db, "2026-08-01", "failed", "fail")
    _seed(db, "2026-08-01", "unrenderable", "unr")
    cfg = _cfg(tmp_path)
    assert main(["--config", str(cfg), "exclude", "--before", "2026-09-01"]) == 0
    assert "excluded 2 replays" in capsys.readouterr().out
    conn = database.connect(db)
    try:
        counts = database.get_counts(conn)
        assert counts["excluded"] == 2
        assert counts["pending"] == 1 and counts["failed"] == 1
        assert counts["unrenderable"] == 1 and counts["rendered"] == 1
        # parked rows surface nowhere: compose input has only the new clip...
        assert [r["path"] for r in database.get_uncomposited(conn)] == ["new.osr"]
        # ...and claims still find the live pending row
        assert database.claim_pending(conn)["path"] == "pend.osr"
    finally:
        conn.close()
