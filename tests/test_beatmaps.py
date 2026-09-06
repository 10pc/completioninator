"""Beatmap mirror tests run against a local HTTP stub — no network needed."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from osu_pipeline import beatmaps

FAKE_OSZ = b"PK\x03\x04" + b"\x00" * 64
MD5 = "abc123def456"
MD5_V2 = "v2checksum0001"
KNOWN_OFFICIAL_MD5 = "officialmd50001"
HINAI_MD5 = "hinaimd5000001"

TOKEN_HITS: list = []


def _json(body: object, status: int = 200):
    raw = json.dumps(body).encode()
    return raw, status


class _Handler(BaseHTTPRequestHandler):
    def _send(self, body: object, status: int = 200):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path == "/oauth/token":
            TOKEN_HITS.append(1)
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self._send({"access_token": "test-token", "expires_in": 3600})
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/search") or self.path.startswith("/api/v2/search"):
            payload = [
                {"SetID": 42,
                 "ChildrenBeatmaps": [{"FileMD5": MD5, "BeatmapID": 7, "ParentSetID": 42}]},
                {"id": 55, "beatmaps": [{"id": 9, "checksum": MD5_V2, "beatmapset_id": 55}]},
            ]
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/v2/beatmaps/lookup"):
            if KNOWN_OFFICIAL_MD5 in self.path:
                self._send({"id": 5, "beatmapset_id": 77})
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path.startswith("/v3/osu/beatmaps/md5/"):
            if self.path.endswith(HINAI_MD5):
                self._send({"id": 11, "beatmapset_id": 66, "checksum": HINAI_MD5})
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path == "/api/v1/hinai/d/66":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(FAKE_OSZ)))
            self.end_headers()
            self.wfile.write(FAKE_OSZ)
        elif self.path == "/api/v1/hinai/d/503":
            self.send_response(503)
            self.end_headers()
        elif self.path == "/d/78":
            self.send_response(200)
            self.send_header("Content-Length", str(len(FAKE_OSZ)))
            self.end_headers()
            self.wfile.write(FAKE_OSZ)
        elif self.path == "/d/42":
            self.send_response(200)
            self.send_header("Content-Length", str(len(FAKE_OSZ)))
            self.end_headers()
            self.wfile.write(FAKE_OSZ)
        elif self.path == "/d/13":
            body = b"not a zip"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def mirror():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_lookup_hit_and_miss(mirror):
    assert beatmaps.lookup_set_id_by_hash(mirror, MD5) == 42
    assert beatmaps.lookup_set_id_by_hash(mirror, MD5_V2) == 55  # v2 shape
    assert beatmaps.lookup_set_id_by_hash(mirror, "nope" * 8) is None


def test_download_caches_and_validates(mirror, tmp_path: Path):
    songs = tmp_path / "songs"
    p = beatmaps.download_beatmapset(mirror, 42, songs, backend="mino")
    assert p.exists()
    # second call skips download (cache hit)
    assert beatmaps.download_beatmapset(mirror, 42, songs, backend="mino") == p
    with pytest.raises(beatmaps.BeatmapError):
        beatmaps.download_beatmapset(mirror, 13, songs, backend="mino")
    with pytest.raises(beatmaps.BeatmapError):
        beatmaps.download_beatmapset(mirror, 404, songs, backend="mino")


def test_ensure_beatmap_override_and_missing_hash(mirror, tmp_path: Path):
    set_id, path = beatmaps.ensure_beatmap(mirror, None, tmp_path, override_set_id=42, backend="mino")
    assert (set_id, path.exists()) == (42, True)
    with pytest.raises(beatmaps.BeatmapError, match="no_beatmap"):
        beatmaps.ensure_beatmap(mirror, None, tmp_path)
    with pytest.raises(beatmaps.BeatmapError, match="no_beatmap"):
        beatmaps.ensure_beatmap(mirror, "missing" * 5, tmp_path)


def test_official_lookup_hit_miss_and_token_reuse(mirror, tmp_path: Path):
    TOKEN_HITS.clear()
    beatmaps._token_cache.clear()
    assert beatmaps.lookup_set_id_official(
        KNOWN_OFFICIAL_MD5, "cid", "secret", osu_base=mirror) == 77
    assert beatmaps.lookup_set_id_official(
        "dead" * 8, "cid", "secret", osu_base=mirror) is None  # official 404
    assert len(TOKEN_HITS) == 1  # token cached across calls


def test_ensure_beatmap_falls_back_to_official(mirror, tmp_path: Path):
    TOKEN_HITS.clear()
    beatmaps._token_cache.clear()
    # "missing"*5 hits no mirror tier, resolves via official stub -> set 77,
    # but set 77 has no download route on the stub -> download error surfaces.
    with pytest.raises(beatmaps.BeatmapError):
        beatmaps.ensure_beatmap(
            mirror, KNOWN_OFFICIAL_MD5, tmp_path,
            osu_client_id="cid", osu_client_secret="secret", osu_base=mirror,
            fallback_mirror=mirror, fallback_backend="mino")


def test_requeue_failed(tmp_path: Path):
    from osu_pipeline import database
    db = tmp_path / "p.sqlite"
    database.init_db(db)
    conn = database.connect(db)
    try:
        database.insert_replay(
            conn, path="a.osr", sha256="s", size=1, mtime_ns=1,
            played_at="2026-09-06T00:00:00+00:00", day="2026-09-06", status="failed",
        )
        conn.commit()
        assert database.requeue_failed(conn) == 1
        assert database.get_counts(conn)["pending"] == 1
        assert database.requeue_failed(conn) == 0
    finally:
        conn.close()


def test_hinamizawa_lookup_hit_and_miss(mirror):
    assert beatmaps.lookup_set_id_hinamizawa(mirror, HINAI_MD5) == 66
    assert beatmaps.lookup_set_id_hinamizawa(mirror, "nope" * 8) is None


def test_hinamizawa_end_to_end(mirror, tmp_path: Path):
    songs = tmp_path / "songs"
    set_id, path = beatmaps.ensure_beatmap(mirror, HINAI_MD5, songs, backend="hinamizawa")
    assert set_id == 66 and path.exists()
    # cache hit on repeat
    assert beatmaps.ensure_beatmap(mirror, HINAI_MD5, songs, backend="hinamizawa")[0] == 66
    with pytest.raises(beatmaps.BeatmapError, match="no_beatmap"):
        beatmaps.ensure_beatmap(mirror, "nope" * 8, songs, backend="hinamizawa",
                                fallback_mirror=None)


def test_lookup_falls_back_to_second_mirror(mirror, tmp_path: Path):
    # MD5 is only in the mino-shaped search payload; hinamizawa md5 route 404s.
    # Download of set 42 then also falls back (hinai route missing, mino serves).
    songs = tmp_path / "songs"
    set_id, path = beatmaps.ensure_beatmap(
        mirror, MD5, songs, backend="hinamizawa",
        fallback_mirror=mirror, fallback_backend="mino")
    assert set_id == 42 and path.exists()


def test_download_falls_back_to_second_mirror(mirror, tmp_path: Path):
    songs = tmp_path / "songs"
    set_id, path = beatmaps.ensure_beatmap(
        mirror, None, songs, override_set_id=78, backend="hinamizawa",
        fallback_mirror=mirror, fallback_backend="mino")
    assert set_id == 78 and path.exists()


def test_transient_classification():
    import urllib.error

    def http_error(code):
        return urllib.error.HTTPError("http://x/", code, "msg", {}, None)

    assert beatmaps._is_transient_network(http_error(503)) is True
    assert beatmaps._is_transient_network(http_error(429)) is True
    assert beatmaps._is_transient_network(http_error(404)) is False
    assert beatmaps._is_transient_network(TimeoutError()) is True


def test_503_download_is_transient(mirror, tmp_path: Path):
    # stub has no md5 route for this hash; resolve via override to reach download
    with pytest.raises(beatmaps.BeatmapError) as ei:
        beatmaps.ensure_beatmap(mirror, None, tmp_path, override_set_id=503, backend="hinamizawa",
                                fallback_mirror=mirror, fallback_backend="mino")
    assert ei.value.transient is True
    assert "503" in str(ei.value)


def test_replay_hash_in_songs(tmp_path: Path):
    import hashlib

    songs = tmp_path / "songs"
    set_dir = songs / "77"
    set_dir.mkdir(parents=True)
    payload = b"[Metadata]\nTitle:Test\n"
    (set_dir / "a.osu").write_bytes(payload)
    (set_dir / "b.osu").write_bytes(b"other content")
    digest = hashlib.md5(payload).hexdigest()
    assert beatmaps.replay_hash_in_songs(songs, 77, digest) is True
    assert beatmaps.replay_hash_in_songs(songs, 77, "0" * 32) is False
    assert beatmaps.replay_hash_in_songs(songs, 78, digest) is None  # no dir
