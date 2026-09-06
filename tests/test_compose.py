"""`compose` CLI test with a fully mocked ffmpeg layer."""

from pathlib import Path

from osu_pipeline import compositor, database
from osu_pipeline.cli import main

DURATIONS = {"a.mp4": 100.0, "b.mp4": 60.0, "c.mp4": 30.0}


def _seed_rendered(db: Path, out: Path, names=("a.mp4", "b.mp4", "c.mp4")):
    database.init_db(db)
    conn = database.connect(db)
    for i, name in enumerate(names):
        (out / name).write_bytes(b"\x00" * 16)  # exists(); content faked by probe mock
        database.insert_replay(
            conn, path=f"{name}.osr", sha256=f"s{i}", size=11, mtime_ns=1,
            played_at="2026-09-06T00:00:00+00:00", day="2026-09-06",
            status="rendered",
        )
        jid = conn.execute("SELECT id FROM replays WHERE path = ?", (f"{name}.osr",)).fetchone()["id"]
        database.mark_rendered(conn, jid, str(out / name))
    conn.commit()
    conn.close()


def test_compose_happy_path(tmp_path: Path, monkeypatch, capsys):
    out = tmp_path / "rendered"
    out.mkdir()
    daily = tmp_path / "daily"
    db = tmp_path / "p.sqlite"
    _seed_rendered(db, out)

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f"database = {str(db)!r}\n"
        f"working = {str(tmp_path / 'work')!r}\n"
        f"rendered = {str(out)!r}\n"
        "[video]\n"
        f"daily = {str(daily)!r}\n"
        "max_clips = 12\n"
    )

    monkeypatch.setattr(compositor, "check_binaries", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(
        compositor, "probe_clip",
        lambda ffprobe, src: compositor.Clip(
            id=-1, path=Path(src), day=None, duration=DURATIONS[src.name], has_audio=True))

    def _encode(ffmpeg, seg, graph, out_path, *a):
        Path(graph).write_text("graph")
        Path(out_path).write_bytes(b"seg")

    monkeypatch.setattr(compositor, "encode_segment", _encode)
    monkeypatch.setattr(
        compositor, "concat_segments",
        lambda ffmpeg, segs, out_path, workdir, timeout=600: Path(out_path).write_bytes(b"joined"))
    monkeypatch.setattr(
        compositor, "encode_audio_mix",
        lambda ffmpeg, clips, graph, out_path, timeout: Path(out_path).write_bytes(b"mix"))
    monkeypatch.setattr(
        compositor, "mux_audio_video",
        lambda ffmpeg, video, audio, out_path, timeout=600: Path(out_path).write_bytes(b"final"))
    # final verification probe
    calls = {"n": 0}

    def _final(ffprobe, src):
        calls["n"] += 1
        if calls["n"] <= 3:
            name = Path(src).name
            return compositor.Clip(id=-1, path=Path(src), day=None,
                                   duration=DURATIONS.get(name, 100.0), has_audio=True)
        return compositor.Clip(id=-1, path=Path(src), day=None, duration=100.0, has_audio=True)

    monkeypatch.setattr(compositor, "probe_clip", _final)

    assert main(["--config", str(cfg), "compose"]) == 0
    out_text = capsys.readouterr().out
    assert "batch: 3 clips" in out_text and "3 segments" in out_text

    conn = database.connect(db)
    try:
        assert database.get_counts(conn)["composited"] == 3
        latest = database.get_latest_daily(conn)
        assert latest and latest["clips"] == 3 and latest["path"].endswith(".mp4")
        assert Path(latest["path"]).exists()
    finally:
        conn.close()


def test_compose_nothing_to_do(tmp_path: Path, monkeypatch, capsys):
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f"database = {str(db)!r}\n"
        f"working = {str(tmp_path / 'work')!r}\n"
        f"rendered = {str(tmp_path / 'rendered')!r}\n"
        "[video]\n"
        f"daily = {str(tmp_path / 'daily')!r}\n"
    )
    monkeypatch.setattr(compositor, "check_binaries", lambda: ("ffmpeg", "ffprobe"))
    assert main(["--config", str(cfg), "compose"]) == 0
    assert "nothing to compose" in capsys.readouterr().out


def test_compose_keeps_longest_max_clips(tmp_path: Path, monkeypatch, capsys):
    out = tmp_path / "rendered"
    out.mkdir()
    db = tmp_path / "p.sqlite"
    names = [f"v{i}.mp4" for i in range(5)]
    _seed_rendered(db, out, names)
    durs = {n: float(10 * (i + 1)) for i, n in enumerate(names)}

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f"database = {str(db)!r}\n"
        f"working = {str(tmp_path / 'work')!r}\n"
        f"rendered = {str(out)!r}\n"
        "[video]\n"
        f"daily = {str(tmp_path / 'daily')!r}\n"
        "max_clips = 2\n"
    )
    monkeypatch.setattr(compositor, "check_binaries", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(
        compositor, "probe_clip",
        lambda ffprobe, src: compositor.Clip(
            id=-1, path=Path(src), day=None,
            duration=durs.get(Path(src).name, 90.0), has_audio=False))
    monkeypatch.setattr(
        compositor, "encode_segment",
        lambda ffmpeg, seg, graph, out_path, *a: Path(out_path).write_bytes(b"seg"))
    monkeypatch.setattr(
        compositor, "concat_segments",
        lambda ffmpeg, segs, out_path, workdir, timeout=600: Path(out_path).write_bytes(b"joined"))
    monkeypatch.setattr(
        compositor, "encode_audio_mix",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no audio expected")))
    def _mux(ffmpeg, video, audio, out_path, timeout=600):
        assert audio is None  # this test's clips have no audio
        Path(out_path).write_bytes(b"final")

    monkeypatch.setattr(compositor, "mux_audio_video", _mux)
    assert main(["--config", str(cfg), "compose"]) == 0
    rolled_out = capsys.readouterr().out
    assert "batch: 2 clips" in rolled_out
    assert "roll forward" in rolled_out
    conn = database.connect(db)
    try:
        counts = database.get_counts(conn)
        assert counts["composited"] == 2 and counts["rendered"] == 3
    finally:
        conn.close()
