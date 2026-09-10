"""YouTube uploader tests: scripted fake service, no network."""

from pathlib import Path

import pytest

from osu_pipeline import database, uploader


class _Resp:
    def __init__(self, status):
        self.status = status
        self.reason = "x"


def _http_error(status):
    from googleapiclient.errors import HttpError

    return HttpError(_Resp(status), b"boom")


class _FakeInsert:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def next_chunk(self):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeService:
    def __init__(self, script):
        self._script = script
        self.insert_kwargs = None
        self.request = None

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.insert_kwargs = {"part": part, "body": body}
        self.request = _FakeInsert(self._script)
        return self.request


def _video(tmp_path: Path) -> Path:
    p = tmp_path / "day.mp4"
    p.write_bytes(b"\x00" * 1024)
    return p


def test_title_rendering():
    assert uploader.render_title("OSU! Completionist — {day} ({clips} maps)",
                                 "2026-09-06", 12, None) == \
        "OSU! Completionist — 2026-09-06 (12 maps)"
    assert uploader.video_url("abc") == "https://youtu.be/abc"


def test_description_rendering():
    from osu_pipeline.config import DEFAULT_YOUTUBE_DESCRIPTION

    desc = uploader.render_description(
        DEFAULT_YOUTUBE_DESCRIPTION, "2026-09-07", 108,
        "2026-03-29..2026-09-05",
        passed="1,166", left="147,148", pct="0.79%")
    assert desc.splitlines()[0] == \
        "osu!standard ranked only - 108 passes (2026-03-29..2026-09-05)"
    assert "Completion at compose time: 1,166/147,148 (0.79%)" in desc
    assert "Remaining: 145,982 maps" in desc
    assert "Rendered with danser" in desc
    assert desc.rstrip().endswith("#gaming")
    # no snapshot: stats lines drop instead of rendering blank
    bare = uploader.render_description(
        DEFAULT_YOUTUBE_DESCRIPTION, "2026-09-06", 3, None)
    assert "Completion at compose time" not in bare
    assert "Remaining:" not in bare
    assert "3 passes (2026-09-06)" in bare


def test_upload_success_and_metadata(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(uploader.time, "sleep", lambda s: None)
    svc = _FakeService([(None, None), (None, {"id": "vid1"})])
    vid = uploader.upload_video(svc, _video(tmp_path), "T", "D", "20", "unlisted")
    assert vid == "vid1"
    body = svc.insert_kwargs["body"]
    assert body["snippet"]["title"] == "T"
    assert body["status"]["privacyStatus"] == "unlisted"
    assert svc.insert_kwargs["part"] == "snippet,status"


def test_upload_retries_then_succeeds(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(uploader.time, "sleep", lambda s: None)
    svc = _FakeService([_http_error(500), _http_error(503), (None, {"id": "vid2"})])
    assert uploader.upload_video(svc, _video(tmp_path), "T", "D", "20", "unlisted") == "vid2"
    assert svc.request.calls == 3


def test_upload_fatal_error(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(uploader.time, "sleep", lambda s: None)
    svc = _FakeService([_http_error(403)])
    with pytest.raises(uploader.UploadError, match="403"):
        uploader.upload_video(svc, _video(tmp_path), "T", "D", "20", "unlisted")


def test_upload_state_transitions(tmp_path: Path):
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    try:
        assert database.get_upload(conn, "2026-09-06", "youtube") is None
        database.mark_uploading(conn, "2026-09-06", "youtube")
        assert database.get_upload(conn, "2026-09-06", "youtube")["status"] == "uploading"
        database.mark_uploaded(conn, "2026-09-06", "youtube", "vid9", "https://youtu.be/vid9")
        row = database.get_upload(conn, "2026-09-06", "youtube")
        assert row["status"] == "uploaded" and row["remote_id"] == "vid9"
        database.mark_upload_failed(conn, "2026-09-07", "youtube", "nope")
        assert database.get_upload(conn, "2026-09-07", "youtube")["status"] == "failed"
        assert len(database.recent_uploads(conn)) == 2
    finally:
        conn.close()


def _seed_daily(db: Path, out: Path) -> None:
    database.init_db(db)
    conn = database.connect(db)
    database.record_daily(conn, "2026-09-06", str(out), 3, 100.0, None,
                          {"passed": "1,133", "left": "147,163", "pct": "0.73%"})
    conn.close()


def _cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[paths]\n"
        f"database = {str(tmp_path / 'p.sqlite')!r}\n"
        "[youtube]\n"
        'privacy = "unlisted"\n'
    )
    return cfg


def test_cli_upload_happy_path_and_duplicate_skip(tmp_path: Path, monkeypatch, capsys):
    from osu_pipeline.cli import main

    out = tmp_path / "day.mp4"
    out.write_bytes(b"\x00" * 16)
    db = tmp_path / "p.sqlite"
    _seed_daily(db, out)
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("PIPELINE_YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("PIPELINE_YOUTUBE_CLIENT_SECRET", "sec")

    calls = {"n": 0}

    class _Creds:
        pass

    monkeypatch.setattr(uploader, "load_credentials", lambda p: _Creds())
    monkeypatch.setattr(uploader, "build_service", lambda c: _FakeService([(None, {"id": "v1"})]))
    monkeypatch.setattr(
        uploader, "upload_video",
        lambda svc, path, title, desc, cat, priv, **k: (calls.__setitem__("n", calls["n"] + 1), "v1")[1])

    assert main(["--config", str(cfg), "upload", "2026-09-06"]) == 0
    assert "youtu.be/v1" in capsys.readouterr().out
    # second run skips without calling upload again
    assert main(["--config", str(cfg), "upload", "2026-09-06"]) == 0
    assert "already on youtube" in capsys.readouterr().out
    assert calls["n"] == 1
    # --force re-uploads
    assert main(["--config", str(cfg), "upload", "2026-09-06", "--force"]) == 0
    assert calls["n"] == 2


def test_cli_upload_failure_recorded(tmp_path: Path, monkeypatch, capsys):
    from osu_pipeline.cli import main

    out = tmp_path / "day.mp4"
    out.write_bytes(b"\x00" * 16)
    db = tmp_path / "p.sqlite"
    _seed_daily(db, out)
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("PIPELINE_YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("PIPELINE_YOUTUBE_CLIENT_SECRET", "sec")

    class _Creds:
        pass

    monkeypatch.setattr(uploader, "load_credentials", lambda p: _Creds())
    monkeypatch.setattr(uploader, "build_service", lambda c: object())

    def _boom(*a, **k):
        raise uploader.UploadError("quotaExceeded")

    monkeypatch.setattr(uploader, "upload_video", _boom)
    assert main(["--config", str(cfg), "upload", "2026-09-06"]) == 1
    assert "quotaExceeded" in capsys.readouterr().err
    conn = database.connect(db)
    try:
        assert database.get_upload(conn, "2026-09-06", "youtube")["status"] == "failed"
    finally:
        conn.close()


def test_cli_upload_missing_pieces(tmp_path: Path, monkeypatch, capsys):
    from osu_pipeline.cli import main

    db = tmp_path / "p.sqlite"
    database.init_db(db)
    cfg = _cfg(tmp_path)
    # no daily row
    assert main(["--config", str(cfg), "upload", "2026-09-06"]) == 2
    # daily row but no credentials configured
    conn = database.connect(db)
    out = tmp_path / "day.mp4"
    out.write_bytes(b"\x00" * 16)
    database.record_daily(conn, "2026-09-06", str(out), 1, 10.0, None, None)
    conn.close()
    assert main(["--config", str(cfg), "upload", "2026-09-06"]) == 2
    assert "client ID" in capsys.readouterr().err


def test_extract_thumbnail_runs_ffmpeg_once(tmp_path: Path, monkeypatch):
    import subprocess

    seen = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        Path(cmd[-1]).write_bytes(b"\xff" * 64)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    video = tmp_path / "day.mp4"
    video.write_bytes(b"\x00" * 16)
    out = tmp_path / "thumb.jpg"
    assert uploader.extract_thumbnail(video, out, 2.0) == out
    cmd = seen["cmd"]
    assert cmd[:6] == ["ffmpeg", "-y", "-v", "error", "-ss", "2.0"]
    assert "1" in cmd[cmd.index("-frames:v") + 1:cmd.index("-frames:v") + 2]


def test_extract_thumbnail_shrinks_then_gives_up(tmp_path: Path, monkeypatch):
    import subprocess

    calls = {"n": 0}

    def _fake_run(cmd, **kwargs):
        calls["n"] += 1
        Path(cmd[-1]).write_bytes(b"\xff" * (3 * 1024 * 1024))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    video = tmp_path / "day.mp4"
    video.write_bytes(b"\x00" * 16)
    with pytest.raises(uploader.UploadError, match="2MB"):
        uploader.extract_thumbnail(video, tmp_path / "thumb.jpg", 1.5)
    assert calls["n"] == 2  # full frame, then shrink attempt


def test_extract_thumbnail_ffmpeg_missing(tmp_path: Path, monkeypatch):
    import subprocess

    def _boom(cmd, **kwargs):
        raise FileNotFoundError("no ffmpeg")

    monkeypatch.setattr(subprocess, "run", _boom)
    video = tmp_path / "day.mp4"
    video.write_bytes(b"\x00" * 16)
    with pytest.raises(uploader.UploadError, match="extraction failed"):
        uploader.extract_thumbnail(video, tmp_path / "thumb.jpg")


class _FakeThumbs:
    def __init__(self, exc=None):
        self.exc = exc
        self.video_id = None

    def thumbnails(self):
        return self

    def set(self, videoId, media_body):
        self.video_id = videoId

        class _Exec:
            def __init__(self, exc):
                self.exc = exc

            def execute(self):
                if self.exc is not None:
                    raise self.exc
                return {}

        return _Exec(self.exc)


def test_set_thumbnail_calls_api(tmp_path: Path):
    img = tmp_path / "thumb.jpg"
    img.write_bytes(b"\xff" * 64)
    svc = _FakeThumbs()
    uploader.set_thumbnail(svc, "vid1", img)
    assert svc.video_id == "vid1"


def test_set_thumbnail_403_calls_out_unverified_channel(tmp_path: Path):
    img = tmp_path / "thumb.jpg"
    img.write_bytes(b"\xff" * 64)
    with pytest.raises(uploader.UploadError, match="verified"):
        uploader.set_thumbnail(_FakeThumbs(exc=_http_error(403)), "vid1", img)
    with pytest.raises(uploader.UploadError, match="thumbnail upload failed"):
        uploader.set_thumbnail(_FakeThumbs(exc=_http_error(500)), "vid1", img)


def test_cli_upload_sets_thumbnail_and_survives_thumb_failure(
        tmp_path: Path, monkeypatch, capsys):
    from osu_pipeline.cli import main

    out = tmp_path / "day.mp4"
    out.write_bytes(b"\x00" * 16)
    db = tmp_path / "p.sqlite"
    _seed_daily(db, out)
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("PIPELINE_YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("PIPELINE_YOUTUBE_CLIENT_SECRET", "sec")

    class _Creds:
        pass

    seen = {}

    class _Svc(_FakeService):
        def thumbnails(self):
            return _FakeThumbs()

    def _fake_extract(video, dest, second):
        seen["second"] = second
        Path(dest).write_bytes(b"\xff" * 64)
        return Path(dest)

    monkeypatch.setattr(uploader, "load_credentials", lambda p: _Creds())
    monkeypatch.setattr(uploader, "build_service",
                        lambda c: _Svc([(None, {"id": "v9"})]))
    monkeypatch.setattr(uploader, "upload_video", lambda *a, **k: "v9")
    monkeypatch.setattr(uploader, "extract_thumbnail", _fake_extract)
    orig_set = uploader.set_thumbnail
    monkeypatch.setattr(uploader, "set_thumbnail",
                        lambda svc, vid, img: seen.update(vid=vid) or orig_set(svc, vid, img))

    assert main(["--config", str(cfg), "upload", "2026-09-06", "--force"]) == 0
    assert seen == {"second": 2.0, "vid": "v9"}
    assert "thumbnail set from t=2.0s" in capsys.readouterr().out

    # thumbnail failure is cosmetic: upload still reports success
    def _boom(video, dest, second):
        raise uploader.UploadError("ffmpeg gone")

    monkeypatch.setattr(uploader, "extract_thumbnail", _boom)
    assert main(["--config", str(cfg), "upload", "2026-09-06", "--force"]) == 0
    assert "thumbnail skipped" in capsys.readouterr().out


def test_auth_flow_binds_configured_host(tmp_path: Path, monkeypatch):
    import google_auth_oauthlib.flow as flow_mod

    seen = {}

    class _FakeFlow:
        @classmethod
        def from_client_config(cls, config, scopes):
            assert config["installed"]["client_id"] == "cid"
            return cls()

        def run_local_server(self, **kwargs):
            seen.update(kwargs)

            class _Creds:
                def to_json(self):
                    return "{}"

            return _Creds()

    monkeypatch.setattr(flow_mod, "InstalledAppFlow", _FakeFlow)
    uploader.run_auth_flow("cid", "sec", tmp_path / "tok.json", port=8091, host="localhost")
    assert seen["host"] == "localhost" and seen["port"] == 8091
    assert seen["bind_addr"] == "0.0.0.0"
    assert (tmp_path / "tok.json").exists()


def test_upload_scheduled_sets_private_and_publish_at(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(uploader.time, "sleep", lambda s: None)
    svc = _FakeService([(None, {"id": "v3"})])
    vid = uploader.upload_video(svc, _video(tmp_path), "T", "D", "20", "unlisted",
                                publish_at="2030-01-01T06:00:00+08:00")
    assert vid == "v3"
    status = svc.insert_kwargs["body"]["status"]
    assert status["privacyStatus"] == "private"  # scheduling forces private
    assert status["publishAt"] == "2030-01-01T06:00:00+08:00"


def test_upload_immediate_keeps_privacy(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(uploader.time, "sleep", lambda s: None)
    svc = _FakeService([(None, {"id": "v4"})])
    uploader.upload_video(svc, _video(tmp_path), "T", "D", "20", "unlisted")
    status = svc.insert_kwargs["body"]["status"]
    assert status["privacyStatus"] == "unlisted"
    assert "publishAt" not in status


def test_scheduled_publish_at_helper():
    future = uploader.scheduled_publish_at("2099-01-02", "06:00")
    assert future.startswith("2099-01-02T06:00:00")  # server-local wall time
    assert uploader.scheduled_publish_at("2000-01-01", "06:00") is None  # past -> immediate
    with pytest.raises(uploader.UploadError, match="HH:MM"):
        uploader.scheduled_publish_at("2099-01-02", "dawn")


def test_extract_code_from_url_and_bare():
    url = ("http://localhost:8091/?state=abc&code=4/0XYZ-123_abc&scope=x")
    assert uploader._extract_code(url) == "4/0XYZ-123_abc"
    assert uploader._extract_code("  4/0XYZ-123_abc  ") == "4/0XYZ-123_abc"
    with pytest.raises(uploader.UploadError):
        uploader._extract_code("http://localhost:8091/?state=abc")
    with pytest.raises(uploader.UploadError):
        uploader._extract_code("")


def test_auth_manual_full_roundtrip(tmp_path: Path, monkeypatch, capsys):
    import google_auth_oauthlib.flow as flow_mod

    seen = {}

    class _FakeFlow:
        def __init__(self, *a, **k):
            seen["redirect"] = k.get("redirect_uri")

        @classmethod
        def from_client_config(cls, config, scopes, **kwargs):
            assert config["installed"]["client_id"] == "cid"
            return cls(**kwargs)

        def authorization_url(self, **kwargs):
            return "https://accounts.google.com/auth?x=1", None

        def fetch_token(self, code=None):
            seen["code"] = code
            self.credentials = type("C", (), {"to_json": lambda self: "{}"})()

    monkeypatch.setattr(flow_mod, "Flow", _FakeFlow)
    monkeypatch.setattr("builtins.input", lambda prompt="": (
        "http://localhost:8091/?state=s&code=4/0TESTCODE&scope=y"))
    uploader.run_auth_manual("cid", "sec", tmp_path / "tok.json", port=8091)
    assert seen["code"] == "4/0TESTCODE"
    assert "localhost:8091" in seen["redirect"]
    assert (tmp_path / "tok.json").exists()
    assert "approve" in capsys.readouterr().out
