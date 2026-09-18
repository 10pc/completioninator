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
        compositor, "encode_morph",
        lambda ffmpeg, span, ordered, *a, **k: Path(a[4]).write_bytes(b"morph"))
    monkeypatch.setattr(
        compositor, "concat_segments",
        lambda ffmpeg, segs, out_path, workdir, timeout=600: Path(out_path).write_bytes(b"joined"))
    monkeypatch.setattr(
        compositor, "encode_audio_mix",
        lambda ffmpeg, clips, graph, out_path, timeout, max_tracks=10: Path(out_path).write_bytes(b"mix"))
    monkeypatch.setattr(
        compositor, "mux_audio_video",
        lambda ffmpeg, video, audio, out_path, timeout=600: Path(out_path).write_bytes(b"final"))
    monkeypatch.setattr(
        compositor, "encode_outro",
        lambda ffmpeg, graph, out_path, preset, crf, timeout: Path(out_path).write_bytes(b"outro"))
    from osu_pipeline import completion as completion_mod
    monkeypatch.setattr(
        completion_mod, "fetch_completion",
        lambda url, timeout=30: completion_mod.CompletionStats("1,133", "147,163", "0.73%"))
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
    assert "batch: 3 clips" in out_text and "morph spans" in out_text
    assert "outro" in out_text
    assert "completion snapshot: 1,133/147,163 (0.73%)" in out_text

    conn = database.connect(db)
    try:
        assert database.get_counts(conn)["composited"] == 3
        latest = database.get_latest_daily(conn)
        assert latest and latest["clips"] == 3 and latest["path"].endswith(".mp4")
        assert Path(latest["path"]).exists()
        assert (latest["passed"], latest["left"], latest["pct"]) == ("1,133", "147,163", "0.73%")
    finally:
        conn.close()


def test_compose_degrades_oversized_morph_to_dissolve(tmp_path: Path, monkeypatch, capsys):
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
        "morph_glide_max_tiles = 2\n"  # 3-tile morph must dissolve
    )
    monkeypatch.setattr(compositor, "check_binaries", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(
        compositor, "probe_clip",
        lambda ffprobe, src: compositor.Clip(
            id=-1, path=Path(src), day=None,
            duration=DURATIONS.get(src.name, 100.0), has_audio=True))
    calls = {"morph": 0, "dissolve": 0, "still": 0}

    def _encode_seg(ffmpeg, seg, graph, out_path, *a):
        Path(graph).write_text("graph")
        Path(out_path).write_bytes(b"seg")

    def _encode_morph(ffmpeg, span, ordered, *a, **k):
        calls["morph"] += 1
        Path(a[4]).write_bytes(b"morph")

    def _encode_seg_still(ffmpeg, seg_mp4, out_path, last, *a, **k):
        calls["still"] += 1
        Path(out_path).write_bytes(b"still")

    def _encode_dissolve(ffmpeg, a, b, duration, fps, out_path, preset, crf, *args, **k):
        calls["dissolve"] += 1
        Path(out_path).write_bytes(b"dissolve")

    monkeypatch.setattr(compositor, "encode_segment", _encode_seg)
    monkeypatch.setattr(compositor, "encode_morph", _encode_morph)
    monkeypatch.setattr(compositor, "encode_seg_still", _encode_seg_still)
    monkeypatch.setattr(compositor, "encode_dissolve", _encode_dissolve)
    monkeypatch.setattr(
        compositor, "concat_segments",
        lambda ffmpeg, segs, out_path, workdir, timeout=600: Path(out_path).write_bytes(b"joined"))
    monkeypatch.setattr(
        compositor, "encode_audio_mix",
        lambda ffmpeg, clips, graph, out_path, timeout, max_tracks=10: Path(out_path).write_bytes(b"mix"))
    monkeypatch.setattr(
        compositor, "mux_audio_video",
        lambda ffmpeg, video, audio, out_path, timeout=600: Path(out_path).write_bytes(b"final"))
    monkeypatch.setattr(
        compositor, "encode_outro",
        lambda ffmpeg, graph, out_path, preset, crf, timeout: Path(out_path).write_bytes(b"outro"))
    from osu_pipeline import completion as completion_mod
    monkeypatch.setattr(
        completion_mod, "fetch_completion",
        lambda url, timeout=30: completion_mod.CompletionStats("1,133", "147,163", "0.73%"))

    assert main(["--config", str(cfg), "compose"]) == 0
    # 3-tile morph dissolves, 2-tile morph still glides (threshold is strict >)
    assert calls["dissolve"] == 1 and calls["still"] == 2 and calls["morph"] == 1
    assert "degraded to dissolve" in capsys.readouterr().out


def test_finale_upgrade_rerenders_longest_at_1080p(tmp_path: Path, monkeypatch, capsys):
    import types

    from osu_pipeline import cli as cli_mod
    from osu_pipeline import database
    from osu_pipeline.renderer import RenderResult

    out = tmp_path / "rendered"
    out.mkdir()
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    (tmp_path / "replays").mkdir()
    (tmp_path / "replays" / "long.osr").write_bytes(b"fake")
    database.insert_replay(
        conn, path="long.osr", sha256="s9", size=11, mtime_ns=1,
        played_at="2026-09-06T00:00:00+00:00", day="2026-09-06",
        status="rendered", beatmap_hash="h" * 32)
    jid = conn.execute("SELECT id FROM replays").fetchone()["id"]
    dest = out / "9.mp4"
    dest.write_bytes(b"\x00" * 16)
    conn.execute("UPDATE replays SET render_path = ?, beatmapset_id = 9 WHERE id = ?",
                 (str(dest), jid))
    conn.commit()
    conn.close()

    cfg = types.SimpleNamespace(
        replays_dir=tmp_path / "replays", working_dir=tmp_path / "work",
        danser_home=tmp_path / "danser", danser_settings="pipeline",
        render_timeout_seconds=60, danser_extra_args=(), danser_skin="",
        beatmap_mirror="m", songs_dir=tmp_path / "songs",
        osu_client_id=None, osu_client_secret=None, beatmap_backend="x",
        fallback_mirror=None, fallback_backend="x",
        fallback2_mirror=None, fallback2_backend="x",
        beatmaps_cache=tmp_path / "cache")
    (tmp_path / "work").mkdir()

    clips = [compositor.Clip(id=jid, path=dest, day="2026-09-06", duration=200.0,
                             has_audio=True, width=1280, height=720)]
    seen = {}

    class _Hi:
        def __init__(self, **kw):
            seen["settings"] = kw.get("settings")

        def render(self, osr, stem, force=False, height=None):
            seen["force"] = force
            outp = tmp_path / "danser" / "videos" / f"{stem}.mp4"
            outp.parent.mkdir(parents=True, exist_ok=True)
            outp.write_bytes(b"\x00" * 16)
            return RenderResult(True, outp, "hi render", 200.0)

    # _upgrade_finale imports DanserRenderer from the renderer module at call
    # time, so patching the source attribute redirects construction.
    import osu_pipeline.renderer as rend_mod
    monkeypatch.setattr(rend_mod, "DanserRenderer", _Hi)
    monkeypatch.setattr(cli_mod.beatmaps, "ensure_beatmap", lambda *a, **k: (9, tmp_path / "9.osz"))
    monkeypatch.setattr(
        compositor, "probe_clip",
        lambda ffprobe, src: compositor.Clip(
            id=-1, path=Path(src), day=None, duration=200.0,
            has_audio=True, width=1920, height=1080))

    cli_mod._upgrade_finale(cfg, db, clips, {jid: "long.osr"}, "ffprobe")
    assert seen == {"settings": "pipeline-hi", "force": True}
    assert clips[0].height == 1080
    assert "upgraded to 1080p" in capsys.readouterr().out

    # already-1080p finale: no render call
    seen.clear()
    clips[0].height = 1080
    cli_mod._upgrade_finale(cfg, db, clips, {jid: "long.osr"}, "ffprobe")
    assert seen == {}

    # missing source: warn-only, clip untouched
    clips[0].height = 720
    cli_mod._upgrade_finale(cfg, db, clips, {jid: "gone.osr"}, "ffprobe")
    assert seen == {} and clips[0].height == 720


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
        compositor, "encode_morph",
        lambda ffmpeg, span, ordered, *a, **k: Path(a[4]).write_bytes(b"morph"))
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
    from osu_pipeline import completion as completion_mod
    monkeypatch.setattr(
        completion_mod, "fetch_completion",
        lambda url, timeout=30: (_ for _ in ()).throw(
            completion_mod.CompletionError("offline")))
    assert main(["--config", str(cfg), "compose"]) == 0
    rolled_out = capsys.readouterr().out
    assert "batch: 2 clips" in rolled_out
    assert "roll forward" in rolled_out
    conn = database.connect(db)
    try:
        counts = database.get_counts(conn)
        assert counts["composited"] == 2 and counts["rendered"] == 3
        latest = database.get_latest_daily(conn)
        assert latest and latest["passed"] is None  # no stats: skipped outro
    finally:
        conn.close()


def test_daily_migration_adds_snapshot_columns(tmp_path: Path):
    import sqlite3

    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE daily (day TEXT PRIMARY KEY, path TEXT, clips INTEGER, "
        "seconds REAL, span TEXT, created_at TEXT);"
    )
    conn.commit()
    conn.close()
    database.init_db(db)
    conn = database.connect(db)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(daily)").fetchall()}
        assert {"passed", "left", "pct"} <= cols
        database.record_daily(conn, "2026-09-06", "/x.mp4", 3, 100.0, None,
                              {"passed": "1,133", "left": "147,163", "pct": "0.73%"})
        row = database.get_latest_daily(conn)
        assert (row["passed"], row["left"], row["pct"]) == ("1,133", "147,163", "0.73%")
    finally:
        conn.close()
