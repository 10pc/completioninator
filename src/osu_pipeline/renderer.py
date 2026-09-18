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

# Render tiers: output height -> danser settings profile. Shorts render at
# 480p in the same wall time as 720p (measured 30.3s vs 28.4s) with much
# smaller files, which is what keeps giant compose graphs out of OOM.
# Profiles live baked into the image next to `pipeline`.
RENDER_HEIGHTS = (480, 720, 1080)


def profile_for_height(base_settings: str, height: int) -> str:
    """Settings profile name for a tier: base itself at 720p."""
    if height <= 480:
        return f"{base_settings}-lo"
    if height >= 1080:
        return f"{base_settings}-hi"
    return base_settings


def select_render_height(estimate_s: float | None, lo_max_s: float) -> int:
    """Render tier from estimated replay length. Unknown -> default 720p.

    A small padding biases slider-heavy maps (tails underestimated) toward
    the higher tier: a wrongly-high tier only costs bytes, a wrongly-low one
    costs visible quality on small days.
    """
    if estimate_s is not None and estimate_s + 5.0 < lo_max_s:
        return 480
    return 720


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

    def render(self, replay_osr: Path, job_stem: str, force: bool = False,
               height: int | None = None) -> RenderResult:
        """Render one replay. Idempotent: existing valid output is reused.

        force re-renders even when a valid output exists (resolution upgrades).
        height re-renders when the reusable output was recorded at another
        tier (retries after a tier change).
        """
        expected = self.output_for(job_stem)
        if expected.exists() and not force:
            ok, duration = verify_output(expected, expect_height=height)
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


def verify_output(mp4: Path, expect_height: int | None = None) -> tuple[bool, float | None]:
    """Return (valid, duration_s). Size gate always; ffprobe duration when available.

    expect_height re-validates the tier: a reusable file recorded at another
    height fails so the job re-renders at the right one. Unknown height
    (no ffprobe) passes through rather than looping re-renders.
    """
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
             "-show_entries", "stream=height", "-of", "json", str(mp4)],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(proc.stdout)
        duration = float(data["format"]["duration"])
        if duration <= 0:
            return False, None
        if expect_height is not None:
            heights = [s.get("height") for s in data.get("streams", []) if s.get("height")]
            if heights and all(h != expect_height for h in heights):
                return False, None
        return True, duration
    except Exception:  # noqa: BLE001 - ffprobe failure just voids the duration tier
        return True, None
