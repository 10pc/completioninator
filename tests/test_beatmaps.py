"""Beatmap mirror tests run against a local HTTP stub — no network needed."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from osu_pipeline import beatmaps

FAKE_OSZ = b"PK\x03\x04" + b"\x00" * 64
MD5 = "abc123def456"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/search"):
            payload = [
                {"SetID": 42,
                 "ChildrenBeatmaps": [{"FileMD5": MD5, "BeatmapID": 7, "ParentSetID": 42}]},
                {"SetID": 99,
                 "ChildrenBeatmaps": [{"FileMD5": "other", "BeatmapID": 8, "ParentSetID": 99}]},
            ]
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
    assert beatmaps.lookup_set_id_by_hash(mirror, "nope" * 8) is None


def test_download_caches_and_validates(mirror, tmp_path: Path):
    songs = tmp_path / "songs"
    p = beatmaps.download_beatmapset(mirror, 42, songs)
    assert p.exists()
    # second call skips download (cache hit)
    assert beatmaps.download_beatmapset(mirror, 42, songs) == p
    with pytest.raises(beatmaps.BeatmapError):
        beatmaps.download_beatmapset(mirror, 13, songs)
    with pytest.raises(beatmaps.BeatmapError):
        beatmaps.download_beatmapset(mirror, 404, songs)


def test_ensure_beatmap_override_and_missing_hash(mirror, tmp_path: Path):
    set_id, path = beatmaps.ensure_beatmap(mirror, None, tmp_path, override_set_id=42)
    assert (set_id, path.exists()) == (42, True)
    with pytest.raises(beatmaps.BeatmapError, match="no_beatmap"):
        beatmaps.ensure_beatmap(mirror, None, tmp_path)
    with pytest.raises(beatmaps.BeatmapError, match="no_beatmap"):
        beatmaps.ensure_beatmap(mirror, "missing" * 5, tmp_path)
