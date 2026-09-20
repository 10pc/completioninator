"""Grid backend tests: ensure, audio resolution, compose-grid orchestration."""

from pathlib import Path

import pytest

from osu_pipeline import beatmaps, compositor, database
from osu_pipeline.cli import main


def _seed_pending(db: Path, names=("a.osr", "b.osr")):
    database.init_db(db)
    conn = database.connect(db)
    for i, name in enumerate(names):
        database.insert_replay(
            conn, path=name, sha256=f"s{i}", size=11, mtime_ns=1,
            played_at="2026-09-06T00:00:00+00:00", day="2026-09-06",
            beatmap_hash="h" * 32)
    conn.commit()
    conn.close()


def _cfg(tmp_path: Path, **over) -> Path:
    cfg = tmp_path / "config.toml"
    body = (
        "[paths]\n"
        f"database = {str(tmp_path / 'p.sqlite')!r}\n"
        f"replays = {str(tmp_path / 'replays')!r}\n"
        f"working = {str(tmp_path / 'work')!r}\n"
        f"rendered = {str(tmp_path / 'rendered')!r}\n"
        "[video]\n"
        f"daily = {str(tmp_path / 'daily')!r}\n"
        "max_clips = 12\n"
        "[beatmaps]\n"
        f"songs_dir = {str(tmp_path / 'songs')!r}\n"
        f"cache_dir = {str(tmp_path / 'cache')!r}\n"
        "[grid]\n"
        'binary = "/nonexistent/danser-grid"\n'
        "timeout_seconds = 60\n"
    )
    for k, v in over.items():
        body += f"{k} = {v!r}\n"
    cfg.write_text(body)
    return cfg


def test_mark_ready_and_get_ready(tmp_path: Path):
    db = tmp_path / "p.sqlite"
    _seed_pending(db, ("a.osr",))
    conn = database.connect(db)
    try:
        assert database.get_ready(conn) == []
        jid = conn.execute("SELECT id FROM replays").fetchone()["id"]
        database.mark_ready(conn, jid)
        rows = database.get_ready(conn)
        assert len(rows) == 1 and rows[0]["id"] == jid
        assert database.get_counts(conn)["ready"] == 1
    finally:
        conn.close()


def test_ensure_happy_path(tmp_path: Path, monkeypatch, capsys):
    root = tmp_path / "replays"
    root.mkdir()
    (root / "a.osr").write_bytes(b"fake")
    db = tmp_path / "p.sqlite"
    _seed_pending(db, ("a.osr",))
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        beatmaps, "ensure_beatmap", lambda *a, **k: (77, tmp_path / "songs" / "77.osz"))
    assert main(["--config", str(cfg), "ensure", "--limit", "5"]) == 0
    assert "ready (set 77)" in capsys.readouterr().out
    conn = database.connect(db)
    try:
        assert database.get_counts(conn)["ready"] == 1
    finally:
        conn.close()


def test_ensure_transient_requeues(tmp_path: Path, monkeypatch):
    root = tmp_path / "replays"
    root.mkdir()
    (root / "a.osr").write_bytes(b"fake")
    db = tmp_path / "p.sqlite"
    _seed_pending(db, ("a.osr",))
    cfg = _cfg(tmp_path)

    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise beatmaps.BeatmapError("mirror down", transient=True)
        return (9, tmp_path / "songs" / "9.osz")

    monkeypatch.setattr(beatmaps, "ensure_beatmap", _flaky)
    assert main(["--config", str(cfg), "ensure", "--limit", "5"]) == 0
    conn = database.connect(db)
    try:
        counts = database.get_counts(conn)
        assert counts["ready"] == 1 and calls["n"] == 2
    finally:
        conn.close()


def test_find_audio_for_hash(tmp_path: Path):
    import hashlib

    d = tmp_path / "songs" / "9"
    d.mkdir(parents=True)
    osu = d / "m.osu"
    osu.write_text("[General]\nAudioFilename: song.mp3\n\n[HitObjects]\n"
                   "64,96,9000,1,0,0:0:0:0:\n256,192,60000,1,0,0:0:0:0:\n")
    mp3 = d / "song.mp3"
    mp3.write_bytes(b"ID3" + b"\x00" * 64)
    digest = hashlib.md5(osu.read_bytes()).hexdigest()
    found, offset = beatmaps.find_audio_for_hash(tmp_path / "songs", 9, digest)
    assert found == mp3
    assert offset == pytest.approx((9000 - 1500) / 1000.0)
    # unknown hash / missing mp3
    assert beatmaps.find_audio_for_hash(tmp_path / "songs", 9, "0" * 32) == (None, 0.0)
    mp3.unlink()
    assert beatmaps.find_audio_for_hash(tmp_path / "songs", 9, digest) == (None, 0.0)


def test_run_danser_grid_success_failure_timeout(tmp_path: Path, monkeypatch):
    import subprocess
    from osu_pipeline import cli as cli_mod

    class _Cfg:
        grid_binary = "/opt/danser-grid/danser-grid"
        danser_home = "/opt/danser"
        danser_settings = "pipeline"
        grid_timeout_seconds = 60

    monkeypatch.setattr(Path, "exists", lambda self: True)

    seen = {}

    def _ok(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    def _fail(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="boom")

    def _slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60)

    monkeypatch.setattr(cli_mod.subprocess, "run", _ok)
    cli_mod.run_danser_grid(_Cfg(), tmp_path / "s.json", 60)  # no raise
    assert seen["cmd"][:2] == ["/opt/danser-grid/danser-grid", "-grid"]
    assert "-settings" in seen["cmd"] and "pipeline" in seen["cmd"]
    assert seen["cwd"] == "/opt/danser"

    monkeypatch.setattr(cli_mod.subprocess, "run", _fail)
    with pytest.raises(cli_mod.GridError, match="exit 3"):
        cli_mod.run_danser_grid(_Cfg(), tmp_path / "s.json", 60)

    monkeypatch.setattr(cli_mod.subprocess, "run", _slow)
    with pytest.raises(cli_mod.GridError, match="timed out"):
        cli_mod.run_danser_grid(_Cfg(), tmp_path / "s.json", 60)

    monkeypatch.setattr(Path, "exists", lambda self: False)
    with pytest.raises(cli_mod.GridError, match="missing"):
        cli_mod.run_danser_grid(_Cfg(), tmp_path / "s.json", 60)


def test_compose_grid_live_morphs(tmp_path: Path, monkeypatch, capsys):
    """Morphs render live in danser-grid: spec carries from/to rects, and no
    dissolve pass runs (dissolves freeze time and desync the mix)."""
    root = tmp_path / "replays"
    root.mkdir()
    (root / "a.osr").write_bytes(b"fake")
    (root / "b.osr").write_bytes(b"fake")
    db = tmp_path / "p.sqlite"
    _seed_pending(db, ("a.osr", "b.osr"))
    cfg = _cfg(tmp_path)
    conn = database.connect(db)
    try:
        for row in conn.execute("SELECT id FROM replays"):
            database.mark_ready(conn, row["id"])
        conn.execute("UPDATE replays SET beatmapset_id = 9, day = '2026-09-06'")
        conn.commit()
    finally:
        conn.close()

    from osu_pipeline import cli as cli_mod
    from osu_pipeline import completion as completion_mod

    monkeypatch.setattr(
        beatmaps, "ensure_beatmap", lambda *a, **k: (9, tmp_path / "songs" / "9.osz"))
    monkeypatch.setattr(
        beatmaps, "find_audio_for_hash",
        lambda *a, **k: (tmp_path / "s.mp3", 1.5))
    (tmp_path / "s.mp3").write_bytes(b"ID3" + b"\x00" * 64)

    def _probe(cfg_, spec_path, out_path):
        import json as _j

        rows = _j.loads(Path(spec_path).read_text())["spans"][0]["tiles"]
        durs = {"a.osr": 60.0, "b.osr": 30.0}
        out = [{"replay": t["replay"],
                "duration_s": durs[Path(t["replay"]).name]} for t in rows]
        Path(out_path).write_text(_j.dumps(out))

    monkeypatch.setattr(cli_mod, "run_danser_grid_probe", _probe)

    seen = {}

    def _record(cfg_, spec_path, timeout):
        import json as _j

        spec = _j.loads(Path(spec_path).read_text())
        seen["spans"] = spec["spans"]
        for span in spec["spans"]:
            seg = Path(spec["outDir"]) / f"{span['name']}.mp4"
            seg.parent.mkdir(parents=True, exist_ok=True)
            seg.write_bytes(b"seg")

    monkeypatch.setattr(cli_mod, "run_danser_grid", _record)
    monkeypatch.setattr(
        completion_mod, "fetch_completion",
        lambda url, timeout=30: completion_mod.CompletionStats("1,133", "147,163", "0.73%"))

    from osu_pipeline import compositor as comp_mod

    monkeypatch.setattr(comp_mod, "check_binaries", lambda: ("ffmpeg", "ffprobe"))
    dissolved = {"n": 0}

    def _dissolve(*a, **k):
        dissolved["n"] += 1

    monkeypatch.setattr(comp_mod, "encode_dissolve", _dissolve)
    monkeypatch.setattr(comp_mod, "encode_seg_still",
                        lambda *a, **k: None)
    monkeypatch.setattr(comp_mod, "encode_outro",
                        lambda ffmpeg, graph, out_path, *a: Path(out_path).write_bytes(b"outro"))
    monkeypatch.setattr(comp_mod, "concat_segments",
                        lambda ffmpeg, segs, out_path, workdir, timeout=600:
                        Path(out_path).write_bytes(b"joined"))
    monkeypatch.setattr(comp_mod, "encode_grid_finish",
                        lambda *a, **k: Path(a[3]).write_bytes(b"finished"))
    monkeypatch.setattr(comp_mod, "encode_audio_mix",
                        lambda *a, **k: Path(a[3]).write_bytes(b"mix"))
    monkeypatch.setattr(comp_mod, "mux_audio_video",
                        lambda *a, **k: Path(a[3]).write_bytes(b"final"))
    monkeypatch.setattr(
        comp_mod, "probe_clip",
        lambda ffprobe, src: comp_mod.Clip(id=-1, path=Path(src), day=None,
                                           duration=100.0, has_audio=True))

    assert main(["--config", str(cfg), "compose", "--grid"]) == 0
    spans = seen["spans"]
    kinds = [s.get("kind", "static") for s in spans]
    assert kinds == ["static", "morph", "static"]
    morph = spans[1]
    assert len(morph["tiles"]) == 2
    tile = morph["tiles"][0]
    assert {"ax", "ay", "aw", "ah", "bx", "by", "bw", "bh", "dying"} <= set(tile)
    assert dissolved["n"] == 0
    # every span (static + morph) has a direct danser-grid output
    assert [s["name"] for s in spans] == ["seg-000", "seg-001", "seg-002"]


def test_compose_grid_happy_path(tmp_path: Path, monkeypatch, capsys):
    root = tmp_path / "replays"
    root.mkdir()
    (root / "a.osr").write_bytes(b"fake")
    (root / "b.osr").write_bytes(b"fake")
    db = tmp_path / "p.sqlite"
    _seed_pending(db, ("a.osr", "b.osr"))
    cfg = _cfg(tmp_path)
    conn = database.connect(db)
    try:
        for row in conn.execute("SELECT id FROM replays"):
            database.mark_ready(conn, row["id"])
        conn.execute("UPDATE replays SET beatmapset_id = 9, day = '2026-09-06'")
        conn.commit()
    finally:
        conn.close()

    from osu_pipeline import cli as cli_mod
    from osu_pipeline import completion as completion_mod

    monkeypatch.setattr(
        beatmaps, "ensure_beatmap", lambda *a, **k: (9, tmp_path / "songs" / "9.osz"))
    monkeypatch.setattr(
        beatmaps, "find_audio_for_hash",
        lambda *a, **k: (tmp_path / "s.mp3", 1.5))
    (tmp_path / "s.mp3").write_bytes(b"ID3" + b"\x00" * 64)

    def _probe(cfg_, spec_path, out_path):
        import json as _j

        rows = _j.loads(Path(spec_path).read_text())["spans"][0]["tiles"]
        out = [{"replay": t["replay"], "duration_s": 60.0} for t in rows]
        Path(out_path).write_text(_j.dumps(out))

    monkeypatch.setattr(cli_mod, "run_danser_grid_probe", _probe)

    def _record(cfg_, spec_path, timeout):
        import json as _j

        spec = _j.loads(Path(spec_path).read_text())
        for span in spec["spans"]:
            seg = Path(spec["outDir"]) / f"{span['name']}.mp4"
            seg.parent.mkdir(parents=True, exist_ok=True)
            seg.write_bytes(b"seg")

    monkeypatch.setattr(cli_mod, "run_danser_grid", _record)
    monkeypatch.setattr(
        completion_mod, "fetch_completion",
        lambda url, timeout=30: completion_mod.CompletionStats("1,133", "147,163", "0.73%"))

    from osu_pipeline import compositor as comp_mod

    monkeypatch.setattr(comp_mod, "check_binaries", lambda: ("ffmpeg", "ffprobe"))

    def _still(ffmpeg, seg_path, out_path, **kwargs):
        Path(out_path).write_bytes(b"still")

    def _dissolve(ffmpeg, a, b, duration, fps, out_path, preset, crf, *args):
        Path(out_path).write_bytes(b"dissolve")

    def _outro(ffmpeg, graph, out_path, preset, crf, timeout):
        Path(out_path).write_bytes(b"outro")

    concats = []

    def _concat(ffmpeg, segs, out_path, workdir, timeout=600):
        concats.append(([Path(s).name for s in segs], Path(out_path).name))
        Path(out_path).write_bytes(b"joined")

    def _finish(ffmpeg, src, graph, out_path, fps, preset, crf, timeout=600):
        Path(out_path).write_bytes(b"finished")

    def _mix(ffmpeg, clips, graph, out_path, timeout, max_tracks=10):
        Path(out_path).write_bytes(b"mix")

    def _mux(ffmpeg, video, audio, out_path, timeout=600):
        Path(out_path).write_bytes(b"final")

    monkeypatch.setattr(comp_mod, "encode_seg_still", _still)
    monkeypatch.setattr(comp_mod, "encode_dissolve", _dissolve)
    monkeypatch.setattr(comp_mod, "encode_outro", _outro)
    monkeypatch.setattr(comp_mod, "concat_segments", _concat)
    monkeypatch.setattr(comp_mod, "encode_grid_finish", _finish)
    monkeypatch.setattr(comp_mod, "encode_audio_mix", _mix)
    monkeypatch.setattr(comp_mod, "mux_audio_video", _mux)
    # final verification probe
    monkeypatch.setattr(
        comp_mod, "probe_clip",
        lambda ffprobe, src: comp_mod.Clip(id=-1, path=Path(src), day=None,
                                           duration=100.0, has_audio=True))

    assert main(["--config", str(cfg), "compose", "--grid"]) == 0
    out_text = capsys.readouterr().out
    assert "batch: 2 clips" in out_text and "composed" in out_text
    # finish pass runs on content only; outro joins after (else the tail
    # fade would hold the outro black).
    assert len(concats) == 2
    assert all("seg-outro.mp4" not in segs for segs, _ in [concats[0]])
    assert concats[1][0][-1] == "seg-outro.mp4"
    conn = database.connect(db)
    try:
        assert database.get_counts(conn)["composited"] == 2
        latest = database.get_latest_daily(conn)
        assert latest and latest["clips"] == 2
    finally:
        conn.close()


def test_compose_grid_falls_back(tmp_path: Path, monkeypatch, capsys):
    root = tmp_path / "replays"
    root.mkdir()
    (root / "a.osr").write_bytes(b"fake")
    db = tmp_path / "p.sqlite"
    _seed_pending(db, ("a.osr",))
    conn = database.connect(db)
    try:
        for row in conn.execute("SELECT id FROM replays"):
            database.mark_ready(conn, row["id"])
        conn.execute("UPDATE replays SET beatmapset_id = 9, day = '2026-09-06'")
        conn.commit()
    finally:
        conn.close()
    cfg = _cfg(tmp_path)

    from osu_pipeline import cli as cli_mod
    from osu_pipeline import compositor as comp_mod

    monkeypatch.setattr(comp_mod, "check_binaries", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(
        beatmaps, "ensure_beatmap", lambda *a, **k: (9, tmp_path / "songs" / "9.osz"))
    monkeypatch.setattr(
        beatmaps, "find_audio_for_hash", lambda *a, **k: (None, 0.0))

    def _probe_ok(cfg_, spec_path, out_path):
        import json as _j

        rows = _j.loads(Path(spec_path).read_text())["spans"][0]["tiles"]
        Path(out_path).write_text(_j.dumps(
            [{"replay": t["replay"], "duration_s": 60.0} for t in rows]))

    monkeypatch.setattr(cli_mod, "run_danser_grid_probe", _probe_ok)

    def _boom(cfg_, spec_path, timeout):
        raise cli_mod.GridError("grid exploded")

    monkeypatch.setattr(cli_mod, "run_danser_grid", _boom)

    calls = {"n": 0}

    def _legacy(args, cfg_):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(cli_mod, "_cmd_compose", _legacy)
    monkeypatch.setattr(cli_mod, "run_render",
                        lambda cfg_, limit, workers=None: (0, {"rendered": 1}))
    assert main(["--config", str(cfg), "compose", "--grid"]) == 0
    assert calls["n"] == 1
    assert "falling back" in capsys.readouterr().err


def test_daily_grid_routing(tmp_path: Path, monkeypatch):
    (tmp_path / "replays").mkdir()
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    database.insert_replay(
        conn, path="a.osr", sha256="s", size=11, mtime_ns=1,
        played_at="2026-09-06T00:00:00+00:00", day="2026-09-06")
    conn.commit()
    jid = conn.execute("SELECT id FROM replays").fetchone()["id"]
    database.mark_ready(conn, jid)
    conn.close()
    cfg = _cfg(tmp_path)

    from osu_pipeline import cli as cli_mod

    seen = {}

    def _ensure(args, cfg_):
        seen["ensure"] = True
        return 0

    def _compose(args, cfg_):
        seen["compose"] = True
        return 0

    monkeypatch.setattr(cli_mod, "_cmd_ensure", _ensure)
    monkeypatch.setattr(cli_mod, "_cmd_compose_grid", _compose)
    assert main(["--config", str(cfg), "daily", "--grid"]) == 0
    assert seen == {"ensure": True, "compose": True}


def test_grid_finish_graph_header_and_fade():
    g = compositor.build_grid_finish_graph("19-09-2026 | 2 maps", "font.ttf", 36, 80)
    assert "drawtext" in g and "19-09-2026" in g and "fade=t=out" not in g
    assert g.strip().endswith("[vout]")
    g2 = compositor.build_grid_finish_graph("h", "font.ttf", 36, 80,
                                            fade_out=1.0, fade_start=59.0)
    assert "fade=t=out:st=59.000:d=1.000" in g2
    # no fade without a valid start (mirrors legacy guard)
    g3 = compositor.build_grid_finish_graph("h", "font.ttf", 36, 80,
                                            fade_out=1.0, fade_start=0.0)
    assert "fade=t=out" not in g3
