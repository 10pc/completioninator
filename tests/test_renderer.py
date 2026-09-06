"""Renderer + render-queue tests use a stub danser (no GPU/binary needed)."""

import sqlite3
import sys
from pathlib import Path

from osu_pipeline import database
from osu_pipeline.renderer import DanserRenderer, verify_output

# Stub danser: understands -out=<stem>, writes <cwd>/videos/<stem>.mp4.
STUB = """
import sys, os
out = [a.split("=", 1)[1] for a in sys.argv if a.startswith("-out=")][0]
fail = os.environ.get("STUB_DANSER_FAIL")
os.makedirs("videos", exist_ok=True)
with open("argv.txt", "w") as f:
    f.write("\\n".join(sys.argv))
if fail:
    sys.stderr.write("boom")
    sys.exit(1)
with open(f"videos/{out}.mp4", "wb") as f:
    f.write(b"\\x00" * 200_000)
print("stub render done")
"""


def _stub_renderer(tmp_path: Path, **kw) -> DanserRenderer:
    home = tmp_path / "danser"
    home.mkdir()
    stub = home / "stub_danser.py"
    stub.write_text(STUB)
    return DanserRenderer(danser_home=home, cmd_prefix=[sys.executable, str(stub)], **kw)


def test_render_success_and_reuse(tmp_path: Path):
    r = _stub_renderer(tmp_path)
    osr = tmp_path / "x.osr"
    osr.write_bytes(b"fake")
    res = r.render(osr, "job-1")
    assert res.ok and res.output.exists()
    # second call reuses the valid output without invoking danser
    res2 = r.render(osr, "job-1")
    assert res2.ok and res2.log_text == "reused existing output"


def test_render_failure(tmp_path: Path, monkeypatch):
    import os

    monkeypatch.setenv("STUB_DANSER_FAIL", "1")
    r = _stub_renderer(tmp_path)
    osr = tmp_path / "x.osr"
    osr.write_bytes(b"fake")
    res = r.render(osr, "job-9")
    assert not res.ok and "exit 1" in (res.error or "")
    # failed stub must not leave an output behind
    assert not r.output_for("job-9").exists()


def test_extra_args_reach_danser(tmp_path: Path):
    home = tmp_path / "danser"
    r = _stub_renderer(tmp_path, extra_args=("-noupdatecheck", "-skip"))
    osr = tmp_path / "x.osr"
    osr.write_bytes(b"fake")
    assert r.render(osr, "job-args").ok
    argv = (home / "argv.txt").read_text()
    assert "-noupdatecheck" in argv and "-skip" in argv


def test_verify_size_gate(tmp_path: Path):
    tiny = tmp_path / "t.mp4"
    tiny.write_bytes(b"\x00" * 10)
    ok, _ = verify_output(tiny)
    assert ok is False
    missing_ok, _ = verify_output(tmp_path / "nope.mp4")
    assert missing_ok is False


def _seed_pending(db: Path, **kw) -> int:
    database.init_db(db)
    conn = database.connect(db)
    row = {"path": "a.osr", "sha256": "s", "size": 4, "mtime_ns": 1,
           "played_at": "2026-09-06T00:00:00+00:00", "day": "2026-09-06"}
    row.update(kw)
    database.insert_replay(conn, **row)
    conn.commit()
    jid = conn.execute("SELECT id FROM replays").fetchone()["id"]
    conn.close()
    return jid


def test_claim_reset_mark_cycle(tmp_path: Path):
    db = tmp_path / "p.sqlite"
    _seed_pending(db)
    conn = database.connect(db)
    try:
        job = database.claim_pending(conn)
        assert job["status"] == "rendering" and job["attempts"] == 1
        assert database.claim_pending(conn) is None  # nothing pending now
        assert database.reset_stale_rendering(conn) == 1  # crash recovery
        job2 = database.claim_pending(conn)
        assert job2["attempts"] == 2
        database.mark_rendered(conn, job2["id"], "/r/1.mp4")
        assert database.get_counts(conn)["rendered"] == 1
    finally:
        conn.close()


def test_migration_adds_m2_columns(tmp_path: Path):
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(db))  # simulate an M1 database (full M1 schema, no M2 cols)
    conn.executescript(
        """
        CREATE TABLE replays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            played_at TEXT,
            day TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            render_path TEXT,
            created_at TEXT NOT NULL,
            rendered_at TEXT,
            error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            UNIQUE(path, sha256)
        );
        """
    )
    conn.commit()
    conn.close()
    database.init_db(db)  # must not crash; adds missing columns
    conn = database.connect(db)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(replays)").fetchall()}
        assert {"beatmap_hash", "beatmapset_id", "render_path"} <= cols
    finally:
        conn.close()
