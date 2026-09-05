"""Beatmap sourcing via the catboy.best (Mino) mirror. No API key required.

Resolution chain per replay:
    replay.beatmap_hash (MD5 from .osr)
        -> Mino search API -> beatmapset id
        -> GET {mirror}/d/{set_id} -> .osz dropped into danser's Songs dir

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
    """Fatal-for-this-job beatmap problem (recorded, queue continues)."""


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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise BeatmapError(f"mirror search HTTP {resp.status}: {url}")
        return json.loads(resp.read().decode("utf-8"))


def lookup_set_id_by_hash(mirror: str, beatmap_hash: str, timeout: int = 30) -> int | None:
    """Resolve a beatmap MD5 to a beatmapset id via Mino search. None if not found."""
    query = urllib.parse.quote(beatmap_hash)
    data = _get_json(f"{mirror.rstrip('/')}/api/search?query={query}", timeout)
    if not isinstance(data, list):
        raise BeatmapError(f"unexpected search response shape: {type(data)}")
    want = beatmap_hash.lower()
    for entry in data:
        for child in entry.get("ChildrenBeatmaps", []):
            if str(child.get("FileMD5", "")).lower() == want:
                set_id = child.get("ParentSetID") or entry.get("SetID")
                if set_id:
                    return int(set_id)
    return None


def download_beatmapset(mirror: str, beatmapset_id: int, songs_dir: Path, timeout: int = 120) -> Path:
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

    url = f"{mirror.rstrip('/')}/d/{beatmapset_id}"
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
        raise BeatmapError(f"download failed for set {beatmapset_id}: {exc}") from exc

    if tmp.stat().st_size == 0:
        _silent_unlink(tmp)
        raise BeatmapError(f"mirror returned empty file for set {beatmapset_id}")
    with open(tmp, "rb") as f:
        magic = f.read(2)
    if magic != b"PK":
        _silent_unlink(tmp)
        raise BeatmapError(f"mirror returned non-zip payload for set {beatmapset_id}")
    tmp.rename(dest)
    log.info("downloaded beatmapset %s -> %s", beatmapset_id, dest)
    return dest


def ensure_beatmap(
    mirror: str,
    beatmap_hash: str | None,
    songs_dir: Path,
    timeout: int = 30,
    override_set_id: int | None = None,
) -> tuple[int, Path]:
    """Ensure the .osz for a replay is in the Songs dir. Returns (set_id, path)."""
    if override_set_id is not None:
        return override_set_id, download_beatmapset(mirror, override_set_id, songs_dir, timeout)
    if not beatmap_hash:
        raise BeatmapError("no_beatmap: replay has no beatmap hash (unparseable .osr?)")
    set_id = lookup_set_id_by_hash(mirror, beatmap_hash, timeout)
    if set_id is None:
        raise BeatmapError(f"no_beatmap: hash {beatmap_hash} not found on mirror")
    return set_id, download_beatmapset(mirror, set_id, songs_dir, timeout)
