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
    # render (Milestone 2)
    danser_home: Path = Path("/opt/danser")
    danser_settings: str = "pipeline"
    render_timeout_seconds: int = 600
    render_max_attempts: int = 3
    render_limit_default: int = 10
    working_dir: Path = Path("/data/working")
    rendered_dir: Path = Path("/data/rendered")
    # beatmaps (Milestone 2)
    beatmap_mirror: str = "https://catboy.best"
    songs_dir: Path = Path("/data/beatmaps/songs")

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
    working = os.environ.get("PIPELINE_WORKING_DIR")
    rendered = os.environ.get("PIPELINE_RENDERED_DIR")
    mirror = os.environ.get("PIPELINE_BEATMAP_MIRROR")
    songs = os.environ.get("PIPELINE_SONGS_DIR")
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
        timezone = pipeline.get("timezone", timezone) if "PIPELINE_TIMEZONE" not in os.environ else timezone
        min_age = pipeline.get("min_age_seconds", min_age) if "PIPELINE_MIN_AGE_SECONDS" not in os.environ else min_age
        danser_home = danser_home or render.get("danser_home", "/opt/danser")
        danser_settings = danser_settings or render.get("settings", "pipeline")
        render_timeout = int(render_timeout or render.get("timeout_seconds", 600))
        render_max_attempts = int(render_max_attempts or render.get("max_attempts", 3))
        limit_default = int(os.environ.get("PIPELINE_RENDER_LIMIT", str(render.get("limit_default", 10))))
        mirror = mirror or beatmaps.get("mirror", "https://catboy.best")
        songs = songs or beatmaps.get("songs_dir", "/data/beatmaps/songs")
    else:
        render_timeout = int(render_timeout or 600)
        render_max_attempts = int(render_max_attempts or 3)
        limit_default = int(os.environ.get("PIPELINE_RENDER_LIMIT", "10"))

    return PipelineConfig(
        replays_dir=Path(replays or "/replays"),
        database_path=Path(database or "/data/db/pipeline.sqlite"),
        timezone=timezone,
        min_age_seconds=min_age,
        danser_home=Path(danser_home or "/opt/danser"),
        danser_settings=danser_settings or "pipeline",
        render_timeout_seconds=render_timeout,
        render_max_attempts=render_max_attempts,
        render_limit_default=limit_default,
        working_dir=Path(working or "/data/working"),
        rendered_dir=Path(rendered or "/data/rendered"),
        beatmap_mirror=mirror or "https://catboy.best",
        songs_dir=Path(songs or "/data/beatmaps/songs"),
    )
