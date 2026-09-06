"""Centralized configuration loading. All tunable values live here, never scattered."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATHS = (
    Path("config/config.toml"),
    Path(__file__).resolve().parents[2] / "config" / "config.toml",
)


@dataclass(frozen=True)
class PipelineConfig:
    replays_dir: Path
    database_path: Path
    timezone: str = "UTC"
    min_age_seconds: int = 5
    # render (Milestone 2+3)
    danser_home: Path = Path("/opt/danser")
    danser_settings: str = "pipeline"
    danser_extra_args: tuple = ()
    render_timeout_seconds: int = 7200
    render_max_attempts: int = 3
    render_limit_default: int = 10
    disk_min_free_gb: float = 5.0
    working_dir: Path = Path("/data/working")
    rendered_dir: Path = Path("/data/rendered")
    logs_dir: Path = Path("/data/logs")
    # beatmaps (Milestone 2)
    beatmap_mirror: str = "https://mirror.hinamizawa.ai"
    beatmap_backend: str = "hinamizawa"  # or "mino"
    fallback_mirror: str | None = "https://catboy.best"
    fallback_backend: str = "mino"
    songs_dir: Path = Path("/data/beatmaps/songs")
    # official osu! API fallback for hash resolution (optional; free OAuth app)
    osu_client_id: str | None = None
    osu_client_secret: str | None = None
    # video composition (Milestone 4)
    video_width: int = 1920
    video_height: int = 1080
    video_fps: int = 30
    header_height: int = 80
    header_extra: str = ""
    max_clips: int = 12
    video_preset: str = "veryfast"
    video_crf: int = 23
    compose_timeout: int = 1800
    morph_seconds: float = 1.0
    outro_seconds: float = 6.0
    outro_fontsize: int = 72
    outro_fontsize_sub: int = 54
    completion_profile_url: str = "https://osucomplete.org/u/19333530/osu-ranked"
    completion_passed: str = ""
    completion_left: str = ""
    completion_pct: str = ""
    fontfile: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    daily_dir: Path = Path("/data/daily")

    @property
    def resolved(self) -> "PipelineConfig":
        return self


def _pick_config_file(explicit: str | Path | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        raise FileNotFoundError(f"Config file not found: {p}")
    if os.environ.get("PIPELINE_CONFIG"):
        p = Path(os.environ["PIPELINE_CONFIG"])
        if p.exists():
            return p
        raise FileNotFoundError(f"Config file not found: {p} (PIPELINE_CONFIG)")
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.exists():
            return candidate
    return None


def load_config(explicit: str | Path | None = None) -> PipelineConfig:
    """Load config.toml with env overrides. Missing file -> env/defaults only."""
    cfg_file = _pick_config_file(explicit)

    replays = os.environ.get("PIPELINE_REPLAYS_DIR")
    database = os.environ.get("PIPELINE_DATABASE")
    timezone = os.environ.get("PIPELINE_TIMEZONE", "UTC")
    min_age = int(os.environ.get("PIPELINE_MIN_AGE_SECONDS", "5"))
    danser_home = os.environ.get("PIPELINE_DANSER_HOME")
    danser_settings = os.environ.get("PIPELINE_DANSER_SETTINGS")
    danser_extra = os.environ.get("PIPELINE_DANSER_EXTRA_ARGS")
    working = os.environ.get("PIPELINE_WORKING_DIR")
    rendered = os.environ.get("PIPELINE_RENDERED_DIR")
    logs = os.environ.get("PIPELINE_LOGS_DIR")
    min_free_gb = os.environ.get("PIPELINE_MIN_FREE_DISK_GB")
    mirror = os.environ.get("PIPELINE_BEATMAP_MIRROR")
    beatmap_backend = os.environ.get("PIPELINE_BEATMAP_BACKEND")
    fallback_mirror = os.environ.get("PIPELINE_FALLBACK_MIRROR")
    fallback_backend = os.environ.get("PIPELINE_FALLBACK_BACKEND")
    songs = os.environ.get("PIPELINE_SONGS_DIR")
    osu_client_id = os.environ.get("PIPELINE_OSU_CLIENT_ID")
    osu_client_secret = os.environ.get("PIPELINE_OSU_CLIENT_SECRET")
    max_clips = os.environ.get("PIPELINE_MAX_CLIPS")
    header_extra = os.environ.get("PIPELINE_HEADER_EXTRA")
    daily_dir = os.environ.get("PIPELINE_DAILY_DIR")
    render_timeout = os.environ.get("PIPELINE_RENDER_TIMEOUT")
    render_max_attempts = os.environ.get("PIPELINE_RENDER_MAX_ATTEMPTS")

    if cfg_file is not None:
        with open(cfg_file, "rb") as f:
            data = tomllib.load(f)
        paths = data.get("paths", {})
        pipeline = data.get("pipeline", {})
        render = data.get("render", {})
        beatmaps = data.get("beatmaps", {})
        replays = replays or paths.get("replays", "/replays")
        database = database or paths.get("database", "/data/db/pipeline.sqlite")
        working = working or paths.get("working", "/data/working")
        rendered = rendered or paths.get("rendered", "/data/rendered")
        logs = logs or paths.get("logs", "/data/logs")
        timezone = pipeline.get("timezone", timezone) if "PIPELINE_TIMEZONE" not in os.environ else timezone
        min_age = pipeline.get("min_age_seconds", min_age) if "PIPELINE_MIN_AGE_SECONDS" not in os.environ else min_age
        danser_home = danser_home or render.get("danser_home", "/opt/danser")
        danser_settings = danser_settings or render.get("settings", "pipeline")
        if danser_extra is None:
            extra_cfg = render.get("extra_args", [])
            danser_extra = tuple(extra_cfg) if isinstance(extra_cfg, list) else ()
        else:
            danser_extra = tuple(a for a in danser_extra.split(",") if a.strip())
        min_free_gb = float(min_free_gb if min_free_gb is not None else render.get("min_free_disk_gb", 5.0))
        render_timeout = int(render_timeout or render.get("timeout_seconds", 7200))
        render_max_attempts = int(render_max_attempts or render.get("max_attempts", 3))
        limit_default = int(os.environ.get("PIPELINE_RENDER_LIMIT", str(render.get("limit_default", 10))))
        mirror = mirror or beatmaps.get("mirror", "https://mirror.hinamizawa.ai")
        beatmap_backend = beatmap_backend or beatmaps.get("backend", "hinamizawa")
        if fallback_mirror is None:
            fallback_mirror = beatmaps.get("fallback_mirror", "https://catboy.best")
        elif fallback_mirror == "":
            fallback_mirror = None  # explicitly disabled
        fallback_backend = fallback_backend or beatmaps.get("fallback_backend", "mino")
        songs = songs or beatmaps.get("songs_dir", "/data/beatmaps/songs")
        osu = data.get("osu", {})
        osu_client_id = osu_client_id or osu.get("client_id")
        osu_client_secret = osu_client_secret or osu.get("client_secret")
        video = data.get("video", {})
        video_width = int(video.get("width", 1920))
        video_height = int(video.get("height", 1080))
        video_fps = int(video.get("fps", 30))
        header_height = int(video.get("header_height", 80))
        header_extra = header_extra if header_extra is not None else video.get("header_extra", "")
        max_clips = int(max_clips or video.get("max_clips", 12))
        video_preset = video.get("preset", "veryfast")
        video_crf = int(video.get("crf", 23))
        compose_timeout = int(video.get("compose_timeout", 1800))
        morph_seconds = float(os.environ.get("PIPELINE_MORPH_SECONDS",
                                             video.get("morph_seconds", 1.0)))
        outro_seconds = float(video.get("outro_seconds", 6.0))
        outro_fontsize = int(video.get("outro_fontsize", 72))
        outro_fontsize_sub = int(video.get("outro_fontsize_sub", 54))
        completion_profile_url = video.get(
            "completion_profile_url", "https://osucomplete.org/u/19333530/osu-ranked")
        completion_passed = os.environ.get("PIPELINE_COMPLETION_PASSED",
                                           video.get("completion_passed", ""))
        completion_left = os.environ.get("PIPELINE_COMPLETION_LEFT",
                                         video.get("completion_left", ""))
        completion_pct = os.environ.get("PIPELINE_COMPLETION_PCT",
                                        video.get("completion_pct", ""))
        fontfile = video.get("fontfile", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        daily_dir = daily_dir or video.get("daily", paths.get("daily", "/data/daily"))
    else:
        render_timeout = int(render_timeout or 7200)
        danser_extra = tuple(a for a in (danser_extra or "").split(",") if a.strip())
        min_free_gb = float(min_free_gb or 5.0)
        if fallback_mirror is None:
            fallback_mirror = "https://catboy.best"
        elif fallback_mirror == "":
            fallback_mirror = None
        render_max_attempts = int(render_max_attempts or 3)
        limit_default = int(os.environ.get("PIPELINE_RENDER_LIMIT", "10"))
        video_width, video_height, video_fps = 1920, 1080, 30
        header_height = 80
        header_extra = header_extra if header_extra is not None else ""
        max_clips = int(max_clips or 12)
        video_preset, video_crf, compose_timeout = "veryfast", 23, 1800
        morph_seconds = float(os.environ.get("PIPELINE_MORPH_SECONDS", 1.0))
        outro_seconds, outro_fontsize, outro_fontsize_sub = 6.0, 72, 54
        completion_profile_url = "https://osucomplete.org/u/19333530/osu-ranked"
        completion_passed = os.environ.get("PIPELINE_COMPLETION_PASSED", "")
        completion_left = os.environ.get("PIPELINE_COMPLETION_LEFT", "")
        completion_pct = os.environ.get("PIPELINE_COMPLETION_PCT", "")
        fontfile = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        daily_dir = daily_dir or "/data/daily"

    return PipelineConfig(
        replays_dir=Path(replays or "/replays"),
        database_path=Path(database or "/data/db/pipeline.sqlite"),
        timezone=timezone,
        min_age_seconds=min_age,
        danser_home=Path(danser_home or "/opt/danser"),
        danser_settings=danser_settings or "pipeline",
        danser_extra_args=danser_extra,
        render_timeout_seconds=render_timeout,
        render_max_attempts=render_max_attempts,
        render_limit_default=limit_default,
        disk_min_free_gb=min_free_gb,
        working_dir=Path(working or "/data/working"),
        rendered_dir=Path(rendered or "/data/rendered"),
        logs_dir=Path(logs or "/data/logs"),
        beatmap_mirror=mirror or "https://mirror.hinamizawa.ai",
        beatmap_backend=beatmap_backend or "hinamizawa",
        fallback_mirror=fallback_mirror,
        fallback_backend=fallback_backend or "mino",
        songs_dir=Path(songs or "/data/beatmaps/songs"),
        osu_client_id=osu_client_id,
        osu_client_secret=osu_client_secret,
        video_width=video_width,
        video_height=video_height,
        video_fps=video_fps,
        header_height=header_height,
        header_extra=header_extra,
        max_clips=max_clips,
        video_preset=video_preset,
        video_crf=video_crf,
        compose_timeout=compose_timeout,
        morph_seconds=morph_seconds,
        outro_seconds=outro_seconds,
        outro_fontsize=outro_fontsize,
        outro_fontsize_sub=outro_fontsize_sub,
        completion_profile_url=completion_profile_url,
        completion_passed=completion_passed,
        completion_left=completion_left,
        completion_pct=completion_pct,
        fontfile=fontfile,
        daily_dir=Path(daily_dir),
    )
