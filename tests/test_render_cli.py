"""End-to-end `render` CLI test with stub danser + stubbed beatmap fetch."""

import sys
from pathlib import Path

from osu_pipeline import beatmaps, database
from osu_pipeline.cli import main
from tests.test_renderer import _stub_renderer


def test_render_cli_happy_path(tmp_path: Path, monkeypatch):
    root = tmp_path / "replays"
    root.mkdir()
    (root / "play.osr").write_bytes(b"fake-replay")
    work = tmp_path / "work"
    out = tmp_path / "rendered"
    logs = tmp_path / "logs"
    songs = tmp_path / "songs"
    songs.mkdir()
    db = tmp_path / "p.sqlite"

    database.init_db(db)
    conn = database.connect(db)
    database.insert_replay(
        conn, path="play.osr", sha256="s", size=11, mtime_ns=1,
        played_at="2026-09-06T00:00:00+00:00", day="2026-09-06",
        beatmap_hash="deadbeef",
    )
    conn.commit()
    conn.close()

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f"replays = {str(root)!r}\n"
        f"database = {str(db)!r}\n"
        f"working = {str(work)!r}\n"
        f"rendered = {str(out)!r}\n"
        f"logs = {str(logs)!r}\n"
        "[render]\n"
        'danser_home = "/opt/danser"\n'
        "min_free_disk_gb = 0\n"
        "[beatmaps]\n"
        f"songs_dir = {str(songs)!r}\n"
    )

    stub = _stub_renderer(tmp_path)
    import osu_pipeline.cli as cli_mod

    monkeypatch.setattr(cli_mod, "DanserRenderer", lambda **kw: stub)
    monkeypatch.setattr(
        beatmaps, "ensure_beatmap", lambda *a, **k: (123, songs / "123.osz")
    )

    assert main(["--config", str(cfg), "render", "--limit", "1"]) == 0

    conn = database.connect(db)
    try:
        row = conn.execute("SELECT * FROM replays").fetchone()
        assert row["status"] == "rendered"
        assert row["render_path"] and Path(row["render_path"]).exists()
        assert row["beatmapset_id"] == 123
    finally:
        conn.close()
    log_files = list(logs.glob("job-*.log"))
    assert len(log_files) == 1 and log_files[0].stat().st_size > 0

    # queue empty now: second run renders nothing
    assert main(["--config", str(cfg), "render", "--limit", "5"]) == 0


def _seed_row(db: Path, **kw) -> None:
    database.init_db(db)
    conn = database.connect(db)
    row = {"path": "play.osr", "sha256": "s", "size": 11, "mtime_ns": 1,
           "played_at": "2026-09-06T00:00:00+00:00", "day": "2026-09-06"}
    row.update(kw)
    database.insert_replay(conn, **row)
    conn.commit()
    conn.close()


def _write_cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f"replays = {str(tmp_path / 'replays')!r}\n"
        f"database = {str(tmp_path / 'p.sqlite')!r}\n"
        f"working = {str(tmp_path / 'work')!r}\n"
        f"rendered = {str(tmp_path / 'rendered')!r}\n"
        f"logs = {str(tmp_path / 'logs')!r}\n"
        "[render]\n"
        'danser_home = "/opt/danser"\n'
        "min_free_disk_gb = 0\n"
        "[beatmaps]\n"
        f"songs_dir = {str(tmp_path / 'songs')!r}\n"
    )
    return cfg


def test_stop_flag_exits_cleanly_without_claiming(tmp_path: Path, monkeypatch, capsys):
    db = tmp_path / "p.sqlite"
    _seed_row(db)
    cfg = _write_cfg(tmp_path)
    stub = _stub_renderer(tmp_path)
    import osu_pipeline.cli as cli_mod

    monkeypatch.setattr(cli_mod, "DanserRenderer", lambda **kw: stub)
    assert main(["--config", str(cfg), "stop"]) == 0
    assert main(["--config", str(cfg), "render", "--limit", "5"]) == 0
    assert "stop requested" in capsys.readouterr().out
    assert not (tmp_path / "stop-render").exists()  # flag consumed
    conn = database.connect(db)
    try:
        assert database.get_counts(conn)["pending"] == 1
    finally:
        conn.close()


def test_progress_overview(tmp_path: Path, capsys):
    db = tmp_path / "p.sqlite"
    _seed_row(db, path="a.osr", sha256="1", status="rendered")
    _seed_row(db, path="b.osr", sha256="2", status="failed", error="boom happened")
    _seed_row(db, path="c.osr", sha256="3")  # pending
    cfg = _write_cfg(tmp_path)
    assert main(["--config", str(cfg), "progress", "2026-09-06"]) == 0
    out = capsys.readouterr().out
    assert "Day 2026-09-06" in out and "33% rendered" in out
    assert "failed:" in out and "boom happened" in out
    assert main(["--config", str(cfg), "progress", "1999-01-01"]) == 0
    assert "no replays" in capsys.readouterr().out


def test_keyboard_interrupt_exits_130(tmp_path: Path, monkeypatch):
    db = tmp_path / "p.sqlite"
    _seed_row(db)
    cfg = _write_cfg(tmp_path)
    stub = _stub_renderer(tmp_path)
    import osu_pipeline.cli as cli_mod

    monkeypatch.setattr(cli_mod, "DanserRenderer", lambda **kw: stub)
    monkeypatch.setattr(
        database, "claim_pending",
        lambda conn: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert main(["--config", str(cfg), "render", "--limit", "5"]) == 130


def test_transient_beatmap_failure_requeues(tmp_path: Path, monkeypatch, capsys):
    import osu_pipeline.cli as cli_mod

    db = tmp_path / "p.sqlite"
    (tmp_path / "replays").mkdir()
    ((tmp_path / "replays") / "play.osr").write_bytes(b"fake-replay")
    _seed_row(db)
    cfg = _write_cfg(tmp_path)
    stub = _stub_renderer(tmp_path)
    monkeypatch.setattr(cli_mod, "DanserRenderer", lambda **kw: stub)

    def _boom(*a, **k):
        raise beatmaps.BeatmapError("mirror pressure", transient=True)

    monkeypatch.setattr(beatmaps, "ensure_beatmap", _boom)
    assert main(["--config", str(cfg), "render", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "transient" in out and "retried=1" in out
    conn = database.connect(db)
    try:
        row = conn.execute("SELECT status FROM replays").fetchone()
        assert row["status"] == "pending"  # not failed: will retry
    finally:
        conn.close()


def test_danser_beatmap_not_found_diagnosis(tmp_path: Path, monkeypatch):
    from osu_pipeline.cli import _diagnose_render_failure
    from osu_pipeline.renderer import RenderResult

    class Cfg:
        beatmap_mirror = "http://mirror"
        beatmap_backend = "hinamizawa"

    bad = RenderResult(False, None, "stdout...\nBeatmap not found, closing...\n", error="exit 1")
    monkeypatch.setattr(beatmaps, "set_checksums", lambda *a, **k: {"otherhash"})
    msg = _diagnose_render_failure(Cfg(), "deadbeef", 2353587, bad)
    assert "updated since play" in msg

    monkeypatch.setattr(beatmaps, "set_checksums", lambda *a, **k: {"deadbeef"})
    assert "retry may help" in _diagnose_render_failure(Cfg(), "deadbeef", 2353587, bad)

    ok = RenderResult(False, None, "some other log", error="boom")
    assert _diagnose_render_failure(Cfg(), "deadbeef", 1, ok) == "boom"


def test_render_disk_guard_stops_run(tmp_path: Path, monkeypatch, capsys):
    import collections
    import shutil

    import osu_pipeline.cli as cli_mod

    root = tmp_path / "replays"
    root.mkdir()
    (root / "play.osr").write_bytes(b"fake-replay")
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    database.insert_replay(
        conn, path="play.osr", sha256="s", size=11, mtime_ns=1,
        played_at="2026-09-06T00:00:00+00:00", day="2026-09-06",
    )
    conn.commit()
    conn.close()

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f"replays = {str(root)!r}\n"
        f"database = {str(db)!r}\n"
        f"working = {str(tmp_path / 'work')!r}\n"
        f"rendered = {str(tmp_path / 'rendered')!r}\n"
        f"logs = {str(tmp_path / 'logs')!r}\n"
    )

    stub = _stub_renderer(tmp_path)
    monkeypatch.setattr(cli_mod, "DanserRenderer", lambda **kw: stub)
    Usage = collections.namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(100, 99, 0.001))

    assert main(["--config", str(cfg), "render", "--limit", "5"]) == 0
    assert "disk guard" in capsys.readouterr().out
    conn = database.connect(db)
    try:
        assert database.get_counts(conn)["pending"] == 1  # nothing claimed
    finally:
        conn.close()
