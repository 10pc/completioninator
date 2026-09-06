"""Beatmap sourcing: resolve MD5 (.osr) -> beatmapset id -> .osz from mirror.

Resolution chain per replay (.osr files carry a beatmap MD5 but NO beatmap id,
so hash is the only replay-native key):
    Tier 1 (no credentials): mirror hash lookup —
        hinamizawa: GET {base}/v3/osu/beatmaps/md5/{hash} (direct, incl. graveyard)
        mino:       search API scanned for the hash (text index; md5 queries miss)
    Tier 2 (free osu! OAuth app): official API v2 beatmaps/lookup?checksum=
        -> beatmapset id (definitive; 404 means the map is deleted from osu!).
    Download: mirror .osz endpoint -> file dropped into danser's Songs dir
        hinamizawa: GET {base}/api/v1/hinai/d/{set_id} (proxied bytes)
        mino:       GET {base}/d/{set_id}
.

danser unpacks .osz files itself (UnpackOszFiles) and matches maps by hash,
so the pipeline only needs to get the right .osz into the Songs directory.
Downloads are resume-safe: existing files are skipped, partial downloads
use a `.part` suffix and are never left behind under the final name.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


class BeatmapError(Exception):
    """Beatmap problem for one job (recorded, queue continues).

    transient=True means "worth retrying soon" (mirror 503/pressure, timeouts,
    error pages): the job goes back to pending instead of failed. The attempts
    cap still bounds it, so a persistently-broken job eventually gives up.
    """

    def __init__(self, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


_TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}


def _is_transient_http(code: int) -> bool:
    return code in _TRANSIENT_HTTP


def _is_transient_network(exc: BaseException) -> bool:
    import http.client
    import socket
    import urllib.error

    if isinstance(exc, urllib.error.HTTPError):
        return _is_transient_http(exc.code)
    return isinstance(exc, (
        urllib.error.URLError,  # includes timeouts/connection resets
        TimeoutError,
        socket.timeout,
        http.client.HTTPException,
    ))


def _silent_unlink(path: Path) -> None:
    """Best-effort delete with retries (Windows AV/indexer can briefly lock new files)."""
    for _ in range(5):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.05)
    path.unlink(missing_ok=True)


def _get_json(url: str, timeout: int) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "osu-completionist-pipeline/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        if _is_transient_network(exc):
            raise BeatmapError(f"mirror request failed (transient): {exc}", transient=True) from exc
        raise BeatmapError(f"mirror search HTTP error: {exc}") from exc


def lookup_set_id_hinamizawa(base: str, beatmap_hash: str, timeout: int = 30) -> int | None:
    """Direct MD5 -> beatmap lookup (no auth, covers ranked + graveyard). None on 404."""
    import urllib.error

    url = f"{base.rstrip('/')}/v3/osu/beatmaps/md5/{urllib.parse.quote(beatmap_hash)}"
    req = urllib.request.Request(url, headers={"User-Agent": "osu-completionist-pipeline/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise BeatmapError(f"hinamizawa md5 lookup HTTP {exc.code}",
                           transient=_is_transient_http(exc.code)) from exc
    except Exception as exc:
        raise BeatmapError(f"hinamizawa md5 lookup failed: {exc}",
                           transient=_is_transient_network(exc)) from exc
    set_id = payload.get("beatmapset_id")
    return int(set_id) if set_id else None


def lookup_set_id_by_hash(mirror: str, beatmap_hash: str, timeout: int = 30) -> int | None:
    """Resolve a beatmap MD5 to a beatmapset id via Mino search (v1+v2 shapes)."""
    query = urllib.parse.quote(beatmap_hash)
    want = beatmap_hash.lower()
    transient_seen = False
    for path in ("api/search", "api/v2/search"):
        try:
            data = _get_json(f"{mirror.rstrip('/')}/{path}?query={query}", timeout)
        except BeatmapError as exc:
            log.warning("mirror %s failed: %s", path, exc)
            transient_seen = transient_seen or exc.transient
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            if not isinstance(entry, dict):
                continue
            # v1 (CheeseGull): ChildrenBeatmaps[].FileMD5 / ParentSetID / SetID
            for child in entry.get("ChildrenBeatmaps", []):
                if str(child.get("FileMD5", "")).lower() == want:
                    set_id = child.get("ParentSetID") or entry.get("SetID")
                    if set_id:
                        return int(set_id)
            # v2: beatmaps[].checksum / id
            for child in entry.get("beatmaps", []):
                if str(child.get("checksum", "")).lower() == want:
                    set_id = child.get("beatmapset_id") or entry.get("id")
                    if set_id:
                        return int(set_id)
    if transient_seen:
        raise BeatmapError("mirror search unavailable (transient)", transient=True)
    return None


def set_checksums(base: str, beatmapset_id: int, timeout: int = 30) -> set[str] | None:
    """Best-effort: current difficulty checksums of a set (hinamizawa only).

    Returns None when the backend has no such endpoint or the call fails —
    callers must treat None as "unknown", never as "hash absent".
    """
    try:
        data = _get_json(f"{base.rstrip('/')}/v3/osu/beatmaps/s/{beatmapset_id}", timeout)
    except BeatmapError:
        return None
    if not isinstance(data, dict):
        return None
    out = set()
    for child in data.get("beatmaps", []):
        if isinstance(child, dict) and child.get("checksum"):
            out.add(str(child["checksum"]).lower())
    return out or None


# --- Tier 2: official osu! API (needs a free OAuth app; definitive) ---

_token_cache: dict = {}


def _osu_token(osu_base: str, client_id: str, client_secret: str, timeout: int) -> str:
    cached = _token_cache.get("token")
    if cached and _token_cache.get("expires_at", 0) > time.time() + 60:
        return cached
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "public",
    }).encode()
    req = urllib.request.Request(
        f"{osu_base.rstrip('/')}/oauth/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "osu-completionist-pipeline/0.2"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise BeatmapError(f"osu! OAuth failed: {exc}",
                           transient=_is_transient_network(exc)) from exc
    token = payload.get("access_token")
    if not token:
        raise BeatmapError("osu! OAuth returned no access_token")
    _token_cache["token"] = token
    _token_cache["expires_at"] = time.time() + int(payload.get("expires_in", 3600))
    return token


def lookup_set_id_official(
    beatmap_hash: str,
    client_id: str,
    client_secret: str,
    timeout: int = 30,
    osu_base: str = "https://osu.ppy.sh",
) -> int | None:
    """Resolve MD5 via official API v2 beatmaps/lookup?checksum=. None = deleted/missing."""
    import urllib.error

    token = _osu_token(osu_base, client_id, client_secret, timeout)
    url = f"{osu_base.rstrip('/')}/api/v2/beatmaps/lookup?checksum={urllib.parse.quote(beatmap_hash)}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "User-Agent": "osu-completionist-pipeline/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code == 401:  # token stale; drop cache so next call re-auths
            _token_cache.clear()
        raise BeatmapError(f"osu! lookup HTTP {exc.code}",
                           transient=_is_transient_http(exc.code)) from exc
    except Exception as exc:
        raise BeatmapError(f"osu! lookup failed: {exc}",
                           transient=_is_transient_network(exc)) from exc
    set_id = payload.get("beatmapset_id")
    return int(set_id) if set_id else None


def _download_url(mirror: str, beatmapset_id: int, backend: str) -> str:
    base = mirror.rstrip("/")
    if backend == "hinamizawa":
        return f"{base}/api/v1/hinai/d/{beatmapset_id}"  # proxied .osz bytes
    return f"{base}/d/{beatmapset_id}"


def download_beatmapset(
    mirror: str,
    beatmapset_id: int,
    songs_dir: Path,
    timeout: int = 120,
    backend: str = "hinamizawa",
) -> Path:
    """Download `<set_id>.osz` into the Songs dir. Skips if already present and valid."""
    songs_dir = Path(songs_dir)
    songs_dir.mkdir(parents=True, exist_ok=True)
    dest = songs_dir / f"{beatmapset_id}.osz"
    if dest.exists() and dest.stat().st_size > 0:
        with open(dest, "rb") as f:
            if f.read(2) == b"PK":
                return dest
        log.warning("existing %s is not a zip; re-downloading", dest)
        dest.unlink()

    url = _download_url(mirror, beatmapset_id, backend)
    tmp = dest.with_suffix(".osz.part")
    req = urllib.request.Request(url, headers={"User-Agent": "osu-completionist-pipeline/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as f:
            if resp.status != 200:
                raise BeatmapError(f"mirror download HTTP {resp.status} for set {beatmapset_id}")
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
    except BeatmapError:
        _silent_unlink(tmp)
        raise
    except Exception as exc:
        _silent_unlink(tmp)
        raise BeatmapError(f"download failed for set {beatmapset_id}: {exc}",
                           transient=_is_transient_network(exc)) from exc

    if tmp.stat().st_size == 0:
        _silent_unlink(tmp)
        raise BeatmapError(f"mirror returned empty file for set {beatmapset_id}",
                           transient=True)  # usually an error page, not a real empty set
    with open(tmp, "rb") as f:
        magic = f.read(2)
    if magic != b"PK":
        _silent_unlink(tmp)
        raise BeatmapError(f"mirror returned non-zip payload for set {beatmapset_id}",
                           transient=True)
    tmp.rename(dest)
    log.info("downloaded beatmapset %s -> %s", beatmapset_id, dest)
    return dest


def ensure_beatmap(
    mirror: str,
    beatmap_hash: str | None,
    songs_dir: Path,
    timeout: int = 30,
    override_set_id: int | None = None,
    osu_client_id: str | None = None,
    osu_client_secret: str | None = None,
    osu_base: str = "https://osu.ppy.sh",
    backend: str = "hinamizawa",
) -> tuple[int, Path]:
    """Ensure the .osz for a replay is in the Songs dir. Returns (set_id, path)."""
    if override_set_id is not None:
        return override_set_id, download_beatmapset(mirror, override_set_id, songs_dir, timeout, backend)
    if not beatmap_hash:
        raise BeatmapError("no_beatmap: replay has no beatmap hash (unparseable .osr?)")
    transient_seen = False
    set_id = None
    try:
        if backend == "hinamizawa":
            set_id = lookup_set_id_hinamizawa(mirror, beatmap_hash, timeout)
        else:
            set_id = lookup_set_id_by_hash(mirror, beatmap_hash, timeout)
    except BeatmapError as exc:
        if not exc.transient:
            raise
        transient_seen = True
    if set_id is None and osu_client_id and osu_client_secret:
        log.info("mirror missed hash %s; trying official osu! lookup", beatmap_hash)
        try:
            set_id = lookup_set_id_official(
                beatmap_hash, osu_client_id, osu_client_secret, timeout, osu_base)
        except BeatmapError as exc:
            if not exc.transient:
                raise
            transient_seen = True
    if set_id is None:
        if transient_seen:
            raise BeatmapError(
                f"beatmap services unavailable (transient) for hash {beatmap_hash}",
                transient=True)
        raise BeatmapError(f"no_beatmap: hash {beatmap_hash} not found on mirror")
    return set_id, download_beatmapset(mirror, set_id, songs_dir, timeout, backend)
