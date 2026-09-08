"""Repair command tests: heal rows indexed inside the sync staging dir."""

from pathlib import Path

from osu_pipeline import database
from osu_pipeline.cli import main


def _cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f"database = {str(tmp_path / 'p.sqlite')!r}\n"
    )
    return cfg


def _seed(db: Path, root: Path, rel: str, sha: str, status: str = "failed",
          attempts: int = 4, make_file: bool = True) -> int:
    database.init_db(db)
    conn = database.connect(db)
    database.insert_replay(
        conn, path=rel, sha256=sha, size=11, mtime_ns=1,
        played_at="2026-09-06T00:00:00+00:00", day="2026-09-06",
        status=status, error="gave up")
    rid = conn.execute("SELECT id FROM replays WHERE path = ?",
                       (rel,)).fetchone()["id"]
    conn.execute("UPDATE replays SET attempts = ? WHERE id = ?", (attempts, rid))
    conn.commit()
    conn.close()
    if make_file:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"fake-replay")
    return rid


def _run(tmp_path: Path, *extra: str) -> int:
    return main(["--config", str(_cfg(tmp_path)), "repair",
                 "--scan-root", str(tmp_path / "replays"), *extra])


def _row(db: Path, rid: int) -> dict | None:
    conn = database.connect(db)
    try:
        row = conn.execute("SELECT * FROM replays WHERE id = ?",
                           (rid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def test_repair_dedupes_when_twin_exists(tmp_path: Path, capsys):
    db = tmp_path / "p.sqlite"
    root = tmp_path / "replays"
    twin = _seed(db, root, "sub/a.osr", "sha-twin", status="pending",
                 attempts=0)
    doomed = _seed(db, root, "sub/staging/a.osr", "sha-twin", make_file=False)
    assert _run(tmp_path) == 0
    assert "dedupe=1" in capsys.readouterr().out
    assert _row(db, doomed) is None  # redundant row gone
    assert _row(db, twin)["status"] == "pending"  # twin untouched


def test_repair_repoints_when_final_exists(tmp_path: Path, capsys):
    db = tmp_path / "p.sqlite"
    root = tmp_path / "replays"
    (root / "sub" / "b.osr").parent.mkdir(parents=True, exist_ok=True)
    (root / "sub" / "b.osr").write_bytes(b"fake-replay")
    rid = _seed(db, root, "sub/staging/b.osr", "sha-repoint", make_file=False)
    assert _run(tmp_path) == 0
    assert "repoint=1" in capsys.readouterr().out
    row = _row(db, rid)
    assert row["path"] == "sub/b.osr"
    assert row["status"] == "pending" and row["attempts"] == 0


def test_repair_moves_orphan_staged_file(tmp_path: Path, capsys):
    db = tmp_path / "p.sqlite"
    root = tmp_path / "replays"
    rid = _seed(db, root, "sub/staging/c.osr", "sha-move")
    assert _run(tmp_path) == 0
    assert "moved=1" in capsys.readouterr().out
    assert (root / "sub" / "c.osr").exists()
    assert not (root / "sub" / "staging" / "c.osr").exists()
    row = _row(db, rid)
    assert row["path"] == "sub/c.osr" and row["status"] == "pending"


def test_repair_leaves_true_orphans(tmp_path: Path, capsys):
    db = tmp_path / "p.sqlite"
    root = tmp_path / "replays"
    rid = _seed(db, root, "sub/staging/d.osr", "sha-lost", make_file=False)
    assert _run(tmp_path) == 0
    assert "orphan=1" in capsys.readouterr().out
    assert _row(db, rid)["status"] == "failed"  # untouched


def test_repair_dry_run_changes_nothing(tmp_path: Path, capsys):
    db = tmp_path / "p.sqlite"
    root = tmp_path / "replays"
    rid = _seed(db, root, "sub/staging/e.osr", "sha-dry")
    assert _run(tmp_path, "--dry-run") == 0
    assert "would repair" in capsys.readouterr().out
    assert _row(db, rid)["status"] == "failed"
    assert (root / "sub" / "staging" / "e.osr").exists()
    assert not (root / "sub" / "e.osr").exists()
