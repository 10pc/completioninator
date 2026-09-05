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
        "[render]\n"
        'danser_home = "/opt/danser"\n'
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

    # queue empty now: second run renders nothing
    assert main(["--config", str(cfg), "render", "--limit", "5"]) == 0
