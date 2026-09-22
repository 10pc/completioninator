"""osu! API v2 player lookup for the grid player card.

Single-player batches only: resolve username -> (id, rank, country,
avatar download). Uses the existing PIPELINE_OSU_CLIENT_ID/SECRET via
OAuth client-credentials; stdlib only. All failures raise PlayerError so
callers fall back to the .osr-embedded username with a text-only card.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_API = "https://osu.ppy.sh/api/v2"


class PlayerError(Exception):
    """Player profile unresolvable (network, auth, or unknown user)."""


@dataclass
class PlayerProfile:
    username: str
    user_id: int
    rank: str  # global, formatted "#12,345"
    country: str  # "ID"-style code
    avatar_url: str
    cover_url: str = ""  # profile banner (custom preferred, default fallback)

    @property
    def avatar_fallback_url(self) -> str:
        return f"https://a.ppy.sh/{self.user_id}"


def _post_json(url: str, data: dict, timeout: int) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception as exc:
        raise PlayerError(f"POST {url} failed: {exc}") from exc


def _get_json(url: str, token: str, timeout: int) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception as exc:
        raise PlayerError(f"GET {url} failed: {exc}") from exc


def parse_profile(username: str, payload: dict) -> PlayerProfile:
    """Build a profile from an API v2 user object (pure, unit-testable)."""
    try:
        uid = int(payload["id"])
        stats = payload.get("statistics") or {}
        rank_num = int(stats.get("global_rank") or 0)
        country = str(payload.get("country_code") or "")
        avatar = str(payload.get("avatar_url") or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise PlayerError(f"bad profile payload for {username}: {exc}") from exc
    if uid <= 0:
        raise PlayerError(f"unknown user {username}")
    cover = payload.get("cover") or {}
    cover_url = str(cover.get("custom_url") or cover.get("url") or "")
    return PlayerProfile(
        username=str(payload.get("username") or username),
        user_id=uid,
        rank=f"#{rank_num:,}" if rank_num > 0 else "#—",
        country=country,
        avatar_url=avatar,
        cover_url=cover_url,
    )


def fetch_player(username: str, client_id: str, client_secret: str,
                 timeout: int = 30) -> PlayerProfile:
    """OAuth client-credentials -> user lookup. Raises PlayerError."""
    if not client_id or not client_secret:
        raise PlayerError("osu API client not configured")
    token = _post_json(f"{_API}/oauth/token", {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": "public",
    }, timeout).get("access_token")
    if not token:
        raise PlayerError("osu OAuth returned no token")
    payload = _get_json(
        f"{_API}/users/{urllib.parse.quote(username)}/osu?key=username",
        token, timeout)
    return parse_profile(username, payload)


def download_avatar(url: str, dest: Path, timeout: int = 60) -> Path:
    """Fetch avatar bytes to dest. Raises PlayerError on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "osu-completionist-pipeline/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except Exception as exc:
        raise PlayerError(f"avatar download failed: {exc}") from exc
    if len(data) < 256 or not data.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8")):
        raise PlayerError(f"avatar download returned {len(data)} junk bytes")
    dest.write_bytes(data)
    return dest


def prepare_plate(cover_url: str, dest: Path, width: int, height: int,
                  ffmpeg: str = "ffmpeg", timeout: int = 120) -> Path:
    """Card background plate from the profile banner: center-crop to the
    card aspect, blur, darken. Raises PlayerError on any failure (caller
    falls back to the plain black rect)."""
    import subprocess
    import tempfile

    if not cover_url:
        raise PlayerError("no cover url")
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "cover"
        download_avatar(cover_url, raw)
        cmd = [ffmpeg, "-y", "-v", "error", "-i", str(raw),
               "-vf", (f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
                       f"crop={width * 2}:{height * 2},"
                       f"boxblur=luma_radius=20:luma_power=2,"
                       f"colorchannelmixer=rr=0.55:gg=0.55:bb=0.55,"
                       f"scale={width}:{height}"),
               "-frames:v", "1", str(dest)]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
        except Exception as exc:
            raise PlayerError(f"plate render failed: {exc}") from exc
    if not dest.exists():
        raise PlayerError("plate render produced no output")
    return dest
