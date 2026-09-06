"""Renderer abstraction around danser-cli. Nothing else knows danser's CLI details.

Production command shape:
    danser-cli -replay="<scratch.osr>" -record -out="<job_stem>" -settings=pipeline [-skip]

`cmd_prefix` exists so tests can substitute a stub (and so Linux can
optionally prepend e.g. `xvfb-run -a`). Verification is two-tier:
  1. output exists and is non-trivially sized (always);
  2. ffprobe duration > 0 when ffprobe is available (gracefully skipped otherwise).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MIN_MP4_BYTES = 100_000


@dataclass
class RenderResult:
    ok: bool
    output: Path | None
    log_text: str
    duration_s: float | None = None
    error: str | None = None


class DanserRenderer:
    def __init__(
        self,
        danser_home: Path,
        cmd_prefix: list[str] | None = None,
        settings: str = "pipeline",
        timeout_seconds: int = 7200,
        skip_intro: bool = True,
        videos_subdir: str = "videos",
        extra_args: tuple | list = (),
        skin: str | None = None,
    ) -> None:
        self.danser_home = Path(danser_home)
        self.binary = self.danser_home / "danser-cli"
        self.cmd_prefix = cmd_prefix or [str(self.binary)]
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        self.skip_intro = skip_intro
        self.extra_args = list(extra_args)
        self.videos_dir = self.danser_home / videos_subdir
        if skin:
            ensure_skin(self.danser_home, settings, skin)

    def output_for(self, job_stem: str) -> Path:
        return self.videos_dir / f"{job_stem}.mp4"

    def render(self, replay_osr: Path, job_stem: str) -> RenderResult:
        """Render one replay. Idempotent: existing valid output is reused."""
        expected = self.output_for(job_stem)
        if expected.exists():
            ok, duration = verify_output(expected)
            if ok:
                log.info("reusing existing render %s", expected)
                return RenderResult(True, expected, "reused existing output", duration)
            expected.unlink()  # stale/invalid leftover; re-render

        cmd = [
            *self.cmd_prefix,
            f"-replay={replay_osr}",
            "-record",
            f"-out={job_stem}",
            f"-settings={self.settings}",
        ]
        if self.skip_intro:
            cmd.append("-skip")
        cmd.extend(self.extra_args)
        log.info("rendering %s: %s", job_stem, " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.danser_home,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            log_text = f"$ {' '.join(cmd)}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
            if proc.returncode != 0:
                return RenderResult(False, None, log_text, error=f"danser exit {proc.returncode}")
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stdout or "") + (exc.stderr or "")
            return RenderResult(
                False, None, partial, error=f"danser timed out after {self.timeout_seconds}s"
            )
        except FileNotFoundError as exc:
            return RenderResult(False, None, "", error=f"danser binary not found: {exc}")

        if not expected.exists():
            return RenderResult(False, None, log_text, error=f"expected output missing: {expected}")
        ok, duration = verify_output(expected)
        if not ok:
            return RenderResult(False, None, log_text, error=f"output failed verification: {expected}")
        return RenderResult(True, expected, log_text, duration)


def ensure_skin(danser_home: Path, settings: str, skin: str) -> None:
    """Point danser's settings profile at the configured skin (merge, keep rest).

    danser rewrites this file on every run, so the repo copy stays the source
    of truth and this merge re-applies the selection each time.
    """
    settings_dir = Path(danser_home) / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    profile = settings_dir / f"{settings}.json"
    try:
        data = json.loads(profile.read_text(encoding="utf-8")) if profile.exists() else {}
    except (OSError, ValueError) as exc:
        log.warning("could not read %s (%s); starting fresh", profile, exc)
        data = {}
    if not isinstance(data, dict):
        data = {}
    skin_section = data.get("Skin")
    if not isinstance(skin_section, dict):
        skin_section = {}
        data["Skin"] = skin_section
    skin_section["CurrentSkin"] = skin
    profile.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("danser skin -> %s (%s)", skin, profile)


def verify_output(mp4: Path) -> tuple[bool, float | None]:
    """Return (valid, duration_s). Size gate always; ffprobe duration when available."""
    try:
        if not mp4.exists() or mp4.stat().st_size < MIN_MP4_BYTES:
            return False, None
    except OSError:
        return False, None
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return True, None
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(mp4)],
            capture_output=True, text=True, timeout=60,
        )
        duration = float(json.loads(proc.stdout)["format"]["duration"])
        return duration > 0, duration
    except Exception:  # noqa: BLE001 - ffprobe failure just voids the duration tier
        return True, None
