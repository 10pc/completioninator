"""osucomplete stats fetch tests against a local HTTP stub."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from osu_pipeline import completion

HTML = """
<html><body>
<h2>% completed</h2><div>0.73%</div>
<h2>completion progress</h2>
<span>maps passed</span><span>1,133</span>
<span>maps left</span><span>147,163</span>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/profile":
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def profile_url():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/profile"
    server.shutdown()


def test_fetch_completion(profile_url):
    stats = completion.fetch_completion(profile_url)
    assert stats.line1 == "1,133/147,163"
    assert stats.line2 == "0.73%"


def test_fetch_missing_numbers():
    with pytest.raises(completion.CompletionError):
        completion.fetch_completion("http://127.0.0.1:1/profile", timeout=2)


def test_manual_override():
    stats = completion.from_manual("10", "20", "33%")
    assert stats and stats.line1 == "10/20" and stats.line2 == "33%"
    assert completion.from_manual("", "20", "33%") is None
    assert completion.from_manual(None, None, None) is None
