"""Instagram publisher tests: stubbed Graph API, no network."""

import io
import json
import urllib.request
from pathlib import Path

import pytest

from osu_pipeline import database, instagram


class _FakeResp:
    def __init__(self, payload: object):
        self._raw = json.dumps(payload).encode()

    def read(self, *a):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def api(monkeypatch):
    calls = {"n": 0, "status_polls": 0}
    bodies = {}

    def _fake(request, timeout=None):
        url = request.full_url
        calls["n"] += 1
        if "rupload.facebook.com" in url:
            bodies["bytes"] = request.data
            return _FakeResp({"success": True})
        if "/media_publish" in url:
            return _FakeResp({"id": "media9"})
        if "fields=status_code" in url or "fields=status_code" in url:
            calls["status_polls"] += 1
            if calls["status_polls"] < 2:
                return _FakeResp({"status_code": "IN_PROGRESS"})
            return _FakeResp({"status_code": "FINISHED"})
        if "fields=permalink" in url:
            return _FakeResp({"permalink": "https://www.instagram.com/p/ABC123/"})
        if "upload_type=resumable" in url:
            return _FakeResp({"id": "container1"})
        raise AssertionError(f"unexpected API call: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    monkeypatch.setattr(instagram.time, "sleep", lambda s: None)
    return calls, bodies


def test_publish_video_full_chain(tmp_path: Path, api):
    calls, bodies = api
    video = tmp_path / "day.mp4"
    video.write_bytes(b"\x00" * 64)
    media_id, link = instagram.publish_video("v25.0", "12345", "tok", video, "cap")
    assert media_id == "media9"
    assert link == "https://www.instagram.com/p/ABC123/"
    assert bodies["bytes"] == b"\x00" * 64  # raw bytes reached rupload
    assert calls["status_polls"] == 2  # polled until FINISHED


def test_container_error_surfaces(tmp_path: Path, monkeypatch):
    def _bad(request, timeout=None):
        if "rupload" in request.full_url:
            return _FakeResp({})
        return _FakeResp({"error": {"code": 123, "message": "nope"}})

    monkeypatch.setattr(urllib.request, "urlopen", _bad)
    video = tmp_path / "day.mp4"
    video.write_bytes(b"\x00" * 8)
    with pytest.raises(instagram.InstagramError):
        instagram.publish_video("v25.0", "12345", "tok", video, "cap")


def test_error_container_status(tmp_path: Path, monkeypatch):
    def _err(request, timeout=None):
        url = request.full_url
        if "rupload" in url:
            return _FakeResp({})
        if "status_code" in url:
            return _FakeResp({"status_code": "ERROR", "status": "bad video"})
        if "upload_type" in url:
            return _FakeResp({"id": "c1"})
        raise AssertionError(url)

    monkeypatch.setattr(urllib.request, "urlopen", _err)
    monkeypatch.setattr(instagram.time, "sleep", lambda s: None)
    video = tmp_path / "day.mp4"
    video.write_bytes(b"\x00" * 8)
    with pytest.raises(instagram.InstagramError, match="ERROR"):
        instagram.publish_video("v25.0", "12345", "tok", video, "cap")


def test_cli_instagram_happy_and_skip(tmp_path: Path, monkeypatch, capsys):
    from osu_pipeline.cli import main

    out = tmp_path / "day.mp4"
    out.write_bytes(b"\x00" * 16)
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    database.record_daily(conn, "2026-09-06", str(out), 3, 100.0, None, None)
    conn.close()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f"database = {str(db)!r}\n"
        "[instagram]\n"
        'api_version = "v25.0"\n'
    )
    monkeypatch.setenv("PIPELINE_INSTAGRAM_USER_ID", "12345")
    monkeypatch.setenv("PIPELINE_INSTAGRAM_TOKEN", "tok")
    monkeypatch.setattr(
        instagram, "publish_video", lambda *a, **k: ("media9", "https://www.instagram.com/p/X/"))

    assert main(["--config", str(cfg), "upload", "2026-09-06", "--platform", "instagram"]) == 0
    assert "instagram.com/p/X" in capsys.readouterr().out
    assert main(["--config", str(cfg), "upload", "2026-09-06", "--platform", "instagram"]) == 0
    assert "already on instagram" in capsys.readouterr().out
    conn = database.connect(db)
    try:
        row = database.get_upload(conn, "2026-09-06", "instagram")
        assert row["status"] == "uploaded" and row["remote_id"] == "media9"
    finally:
        conn.close()


def test_cli_instagram_missing_creds(tmp_path: Path, capsys):
    from osu_pipeline.cli import main

    out = tmp_path / "day.mp4"
    out.write_bytes(b"\x00" * 16)
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    database.record_daily(conn, "2026-09-06", str(out), 1, 10.0, None, None)
    conn.close()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"[paths]\ndatabase = {str(db)!r}\n")
    assert main(["--config", str(cfg), "upload", "2026-09-06", "--platform", "instagram"]) == 2
    assert "user ID/token" in capsys.readouterr().err
