"""Render pruning tests: membership, auto-prune, backfill, dry-run."""

from pathlib import Path

from osu_pipeline import database, uploader
from osu_pipeline.cli import main


def _setup(tmp_path: Path, day="2026-09-06"):
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    daily = tmp_path / "daily.mp4"
    daily.write_bytes(b"\x00" * 32)
    database.record_daily(conn, day, str(daily), 2, 100.0, None, None)
    r1 = tmp_path / "r1.mp4"
    r2 = tmp_path / "r2.mp4"
    r1.write_bytes(b"\x00" * 64)
    r2.write_bytes(b"\x00" * 64)
    for i, rp in enumerate((r1, r2), start=1):
        database.insert_replay(
            conn, path=f"{i}.osr", sha256=f"s{i}", size=11, mtime_ns=1,
            played_at="2026-09-06T00:00:00+00:00", day=day)
        jid = conn.execute("SELECT id FROM replays WHERE path = ?", (f"{i}.osr",)).fetchone()["id"]
        database.mark_rendered(conn, jid, str(rp))
    conn.commit()
    conn.close()
    return db, daily, r1, r2


def _cfg(tmp_path: Path, prune=True) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f"database = {str(tmp_path / 'p.sqlite')!r}\n"
        "[youtube]\n"
        f"prune_after_upload = {'true' if prune else 'false'}\n"
    )
    return cfg


def _authed_upload(monkeypatch):
    class _Creds:
        pass

    monkeypatch.setattr(uploader, "load_credentials", lambda p: _Creds())
    monkeypatch.setattr(uploader, "build_service", lambda c: object())
    monkeypatch.setattr(uploader, "upload_video", lambda *a, **k: "vid1")


def test_membership_and_backfill_query(tmp_path: Path):
    db, _, _, _ = _setup(tmp_path)
    conn = database.connect(db)
    try:
        assert database.clips_of_day(conn, "2026-09-06") == []
        database.mark_composited(conn, [1, 2], "2026-09-06")
        clips = database.clips_of_day(conn, "2026-09-06")
        assert sorted(c["id"] for c in clips) == [1, 2]
        assert database.max_uploaded_day(conn) is None
        database.mark_uploaded(conn, "2026-09-06", "youtube", "v", "u")
        assert database.max_uploaded_day(conn) == "2026-09-06"
    finally:
        conn.close()


def test_auto_prune_on_upload_success(tmp_path: Path, monkeypatch, capsys):
    db, daily, r1, r2 = _setup(tmp_path)
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("PIPELINE_YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("PIPELINE_YOUTUBE_CLIENT_SECRET", "sec")
    _authed_upload(monkeypatch)
    conn = database.connect(db)
    database.mark_composited(conn, [1], "2026-09-06")  # only r1 covered
    conn.close()

    assert main(["--config", str(cfg), "upload", "2026-09-06"]) == 0
    assert "pruned 1 render files" in capsys.readouterr().out
    assert not r1.exists() and r2.exists() and daily.exists()
    conn = database.connect(db)
    try:
        row = conn.execute("SELECT status, render_path FROM replays WHERE id = 1").fetchone()
        assert row["status"] == "composited" and row["render_path"]  # audit trail kept
    finally:
        conn.close()


def test_prune_disabled_by_config(tmp_path: Path, monkeypatch, capsys):
    db, daily, r1, r2 = _setup(tmp_path)
    cfg = _cfg(tmp_path, prune=False)
    monkeypatch.setenv("PIPELINE_YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("PIPELINE_YOUTUBE_CLIENT_SECRET", "sec")
    _authed_upload(monkeypatch)
    conn = database.connect(db)
    database.mark_composited(conn, [1], "2026-09-06")
    conn.close()
    assert main(["--config", str(cfg), "upload", "2026-09-06"]) == 0
    assert "pruned" not in capsys.readouterr().out
    assert r1.exists()


def test_prune_day_guards(tmp_path: Path, capsys):
    db, daily, r1, r2 = _setup(tmp_path)
    cfg = _cfg(tmp_path)
    conn = database.connect(db)
    database.mark_composited(conn, [1, 2], "2026-09-06")
    conn.close()
    # refuses without an upload record
    assert main(["--config", str(cfg), "prune", "--day", "2026-09-06"]) == 2
    assert r1.exists() and r2.exists()
    # dry-run reports, deletes nothing (needs upload or --force)
    conn = database.connect(db)
    database.mark_uploaded(conn, "2026-09-06", "youtube", "v", "u")
    conn.close()
    assert main(["--config", str(cfg), "prune", "--day", "2026-09-06", "--dry-run"]) == 0
    assert "would prune 2 files" in capsys.readouterr().out
    assert r1.exists()
    # real run deletes
    assert main(["--config", str(cfg), "prune", "--day", "2026-09-06"]) == 0
    assert not r1.exists() and not r2.exists() and daily.exists()


def test_prune_backfill(tmp_path: Path, capsys):
    db, daily, r1, r2 = _setup(tmp_path, day="2026-09-01")
    cfg = _cfg(tmp_path)
    conn = database.connect(db)
    database.mark_composited(conn, [1, 2])  # legacy rows: no mapping
    database.mark_uploaded(conn, "2026-09-06", "youtube", "v", "u")
    conn.close()
    assert main(["--config", str(cfg), "prune", "--backfill", "--dry-run"]) == 0
    assert r1.exists()
    assert main(["--config", str(cfg), "prune", "--backfill"]) == 0
    assert "at/below uploaded day 2026-09-06" in capsys.readouterr().out
    assert not r1.exists() and not r2.exists() and daily.exists()


def test_backfill_refuses_without_uploads(tmp_path: Path, capsys):
    db, daily, r1, r2 = _setup(tmp_path)
    cfg = _cfg(tmp_path)
    conn = database.connect(db)
    database.mark_composited(conn, [1])
    conn.close()
    assert main(["--config", str(cfg), "prune", "--backfill"]) == 2
    assert r1.exists()
