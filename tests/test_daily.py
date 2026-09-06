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
