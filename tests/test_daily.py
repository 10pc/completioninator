"""`daily` close-out tests: discover -> render -> compose-if-new-renders."""

import sys
from pathlib import Path

import pytest

from osu_pipeline import beatmaps, compositor, database
from osu_pipeline.cli import main
from tests.test_renderer import _stub_renderer


def _base_cfg(tmp_path: Path) -> Path:
    (tmp_path / "replays").mkdir(exist_ok=True)
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
        "[video]\n"
        f"daily = {str(tmp_path / 'daily')!r}\n"
    )
    return cfg


def test_daily_empty_queue_skips_compose(tmp_path: Path, monkeypatch, capsys):
    cfg = _base_cfg(tmp_path)
    stub = _stub_renderer(tmp_path)
    import osu_pipeline.cli as cli_mod

    monkeypatch.setattr(cli_mod, "DanserRenderer", lambda **kw: stub)
    assert main(["--config", str(cfg), "daily", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "discovered new=0" in out and "composed=skipped" in out


def test_daily_no_danser_aborts(tmp_path: Path, capsys):
    cfg = _base_cfg(tmp_path)
    assert main(["--config", str(cfg), "daily", "--limit", "5"]) == 2
    assert "danser not found" in capsys.readouterr().err


def test_daily_renders_then_composes(tmp_path: Path, monkeypatch, capsys):
    cfg = _base_cfg(tmp_path)
    root = tmp_path / "replays"
    (root / "play.osr").write_bytes(b"fake-replay")
    old = __import__("time").time() - 3600
    __import__("os").utime(root / "play.osr", (old, old))
    db = tmp_path / "p.sqlite"

    stub = _stub_renderer(tmp_path)
    import osu_pipeline.cli as cli_mod

    monkeypatch.setattr(cli_mod, "DanserRenderer", lambda **kw: stub)
    monkeypatch.setattr(
        beatmaps, "ensure_beatmap",
        lambda *a, **k: (123, tmp_path / "songs" / "123.osz"))

    monkeypatch.setattr(compositor, "check_binaries", lambda: ("ffmpeg", "ffprobe"))

    def _probe(ffprobe, src):
        return compositor.Clip(id=-1, path=Path(src), day=None, duration=60.0, has_audio=False)

    monkeypatch.setattr(compositor, "probe_clip", _probe)
    monkeypatch.setattr(
        compositor, "encode_segment",
        lambda ffmpeg, seg, graph, out_path, *a: Path(out_path).write_bytes(b"seg"))
    monkeypatch.setattr(
        compositor, "encode_morph",
        lambda ffmpeg, span, ordered, *a, **k: Path(a[4]).write_bytes(b"morph"))
    monkeypatch.setattr(
        compositor, "concat_segments",
        lambda ffmpeg, segs, out_path, workdir, timeout=600: Path(out_path).write_bytes(b"joined"))
    monkeypatch.setattr(
        compositor, "encode_audio_mix",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no audio expected")))

    def _mux(ffmpeg, video, audio, out_path, timeout=600):
        assert audio is None
        Path(out_path).write_bytes(b"final")

    monkeypatch.setattr(compositor, "mux_audio_video", _mux)
    from osu_pipeline import completion as completion_mod
    monkeypatch.setattr(
        completion_mod, "fetch_completion",
        lambda url, timeout=30: (_ for _ in ()).throw(completion_mod.CompletionError("off")))
    monkeypatch.setattr(
        compositor, "encode_outro",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no outro expected")))

    assert main(["--config", str(cfg), "daily", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "rendered=1" in out and "composed=ok" in out
    conn = database.connect(db)
    try:
        assert database.get_counts(conn)["composited"] == 1
    finally:
        conn.close()


def test_daily_composes_leftover_uncomposited(tmp_path: Path, monkeypatch, capsys):
    """Queue drained by earlier runs must still produce a video (the 03:00 bug)."""
    cfg = _base_cfg(tmp_path)
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    rdir = tmp_path / "rendered" / "2026-09-06"
    rdir.mkdir(parents=True)
    for i in range(2):
        (rdir / f"{i}.mp4").write_bytes(b"vid")
        database.insert_replay(
            conn, path=f"p{i}.osr", sha256=f"s{i}", size=1, mtime_ns=1,
            played_at="2026-09-06T00:00:00+00:00", day="2026-09-06")
        rid = conn.execute("SELECT id FROM replays WHERE path = ?",
                           (f"p{i}.osr",)).fetchone()["id"]
        database.mark_rendered(conn, rid, str(rdir / f"{i}.mp4"))
    conn.close()

    import osu_pipeline.cli as cli_mod
    from osu_pipeline import completion as completion_mod

    stub = _stub_renderer(tmp_path)
    monkeypatch.setattr(cli_mod, "DanserRenderer", lambda **kw: stub)
    monkeypatch.setattr(compositor, "check_binaries", lambda: ("ffmpeg", "ffprobe"))

    def _probe(ffprobe, src):
        return compositor.Clip(id=-1, path=Path(src), day=None, duration=60.0, has_audio=False)

    monkeypatch.setattr(compositor, "probe_clip", _probe)
    monkeypatch.setattr(
        compositor, "encode_segment",
        lambda ffmpeg, seg, graph, out_path, *a: Path(out_path).write_bytes(b"seg"))
    monkeypatch.setattr(
        compositor, "concat_segments",
        lambda ffmpeg, segs, out_path, workdir, timeout=600: Path(out_path).write_bytes(b"joined"))

    def _mux(ffmpeg, video, audio, out_path, timeout=600):
        assert audio is None
        Path(out_path).write_bytes(b"final")

    monkeypatch.setattr(compositor, "mux_audio_video", _mux)
    monkeypatch.setattr(
        completion_mod, "fetch_completion",
        lambda url, timeout=30: (_ for _ in ()).throw(completion_mod.CompletionError("off")))
    # nothing pending: this run renders zero, compose must still fire
    assert main(["--config", str(cfg), "daily", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "rendered=0" in out and "composed=ok" in out
    conn = database.connect(db)
    try:
        assert database.get_counts(conn)["composited"] == 2
    finally:
        conn.close()


def test_daily_upload_skips_unconfigured_platforms(tmp_path: Path, monkeypatch, capsys):
    cfg = _base_cfg(tmp_path)
    stub = _stub_renderer(tmp_path)
    import osu_pipeline.cli as cli_mod

    monkeypatch.setattr(cli_mod, "DanserRenderer", lambda **kw: stub)
    assert main(["--config", str(cfg), "daily", "--limit", "5", "--upload"]) == 0
    out = capsys.readouterr().out
    assert "youtube:unconfigured" in out and "instagram:unconfigured" in out


def test_daily_upload_publishes_backlog(tmp_path: Path, monkeypatch, capsys):
    import osu_pipeline.cli as cli_mod

    cfg = _base_cfg(tmp_path)
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    out = tmp_path / "day.mp4"
    out.write_bytes(b"\x00" * 16)
    database.record_daily(conn, "2026-09-06", str(out), 3, 100.0, None, None)
    conn.close()
    stub = _stub_renderer(tmp_path)
    monkeypatch.setattr(cli_mod, "DanserRenderer", lambda **kw: stub)
    monkeypatch.setenv("PIPELINE_YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("PIPELINE_YOUTUBE_CLIENT_SECRET", "sec")

    calls = []

    def _fake_upload(args, cfg):
        calls.append((args.day, args.platform))
        return 0

    monkeypatch.setattr(cli_mod, "_cmd_upload", _fake_upload)
    assert main(["--config", str(cfg), "daily", "--limit", "5", "--upload"]) == 0
    assert calls == [("2026-09-06", "youtube")]  # instagram unconfigured, skipped
    out_text = capsys.readouterr().out
    assert "uploaded=youtube:ok,instagram:unconfigured" in out_text


def test_daily_upload_failure_returns_1(tmp_path: Path, monkeypatch, capsys):
    import osu_pipeline.cli as cli_mod

    cfg = _base_cfg(tmp_path)
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    out = tmp_path / "day.mp4"
    out.write_bytes(b"\x00" * 16)
    database.record_daily(conn, "2026-09-06", str(out), 3, 100.0, None, None)
    conn.close()
    stub = _stub_renderer(tmp_path)
    monkeypatch.setattr(cli_mod, "DanserRenderer", lambda **kw: stub)
    monkeypatch.setattr(cli_mod, "_cmd_upload", lambda args, cfg: 1)
    monkeypatch.setenv("PIPELINE_YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("PIPELINE_YOUTUBE_CLIENT_SECRET", "sec")
    assert main(["--config", str(cfg), "daily", "--limit", "5", "--upload"]) == 1
    assert "youtube:failed" in capsys.readouterr().out


def test_pending_upload_day_prefers_latest(tmp_path: Path):
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    try:
        assert database.pending_upload_day(conn, "youtube") is None
        database.record_daily(conn, "2026-09-01", "/a.mp4", 1, 10.0, None, None)
        database.record_daily(conn, "2026-09-06", "/b.mp4", 2, 20.0, None, None)
        assert database.pending_upload_day(conn, "youtube") == "2026-09-06"
        database.mark_uploaded(conn, "2026-09-06", "youtube", "v", "u")
        assert database.pending_upload_day(conn, "youtube") == "2026-09-01"
        database.mark_uploaded(conn, "2026-09-01", "youtube", "v", "u")
        assert database.pending_upload_day(conn, "youtube") is None
    finally:
        conn.close()


def test_prune_old_dailies(tmp_path: Path):
    from osu_pipeline import cli as cli_mod

    daily = tmp_path / "daily"
    daily.mkdir()
    days = [f"2026-09-{i:02d}" for i in range(1, 11)]
    for d in days:
        (daily / f"day-{d}.mp4").write_bytes(b"v" * 100)
    (daily / "notes.txt").write_text("x")
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    try:
        for d in days:
            database.mark_uploaded(conn, d, "youtube", "v", "u")
    finally:
        conn.close()
    stats = cli_mod.prune_old_dailies(daily, db, keep_days=7)
    assert stats["deleted"] == 3 and stats["kept"] == 7
    assert not (daily / "day-2026-09-01.mp4").exists()
    assert (daily / "day-2026-09-10.mp4").exists()
    assert (daily / "notes.txt").exists()  # non-video: never touched


def test_prune_spares_unuploaded(tmp_path: Path):
    from osu_pipeline import cli as cli_mod

    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "day-2026-09-01.mp4").write_bytes(b"v" * 100)
    db = tmp_path / "p.sqlite"
    database.init_db(db)  # no upload rows at all
    stats = cli_mod.prune_old_dailies(daily, db, keep_days=0)
    assert stats["deleted"] == 0  # only copy in existence: kept
    assert (daily / "day-2026-09-01.mp4").exists()


def test_check_free_space(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    from osu_pipeline import cli as cli_mod

    cfg = SimpleNamespace(working_dir=tmp_path, daily_dir=tmp_path)
    assert cli_mod.check_free_space(cfg, minimum_gb=0.001) is True
    monkeypatch.setattr(cli_mod, "_free_gb", lambda p: 0.0)
    assert cli_mod.check_free_space(cfg, minimum_gb=5.0) is False
