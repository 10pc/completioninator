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

    if cfg_file is not None:
        with open(cfg_file, "rb") as f:
            data = tomllib.load(f)
        paths = data.get("paths", {})
        pipeline = data.get("pipeline", {})
        replays = replays or paths.get("replays", "/replays")
        database = database or paths.get("database", "/data/db/pipeline.sqlite")
        timezone = pipeline.get("timezone", timezone) if "PIPELINE_TIMEZONE" not in os.environ else timezone
        min_age = pipeline.get("min_age_seconds", min_age) if "PIPELINE_MIN_AGE_SECONDS" not in os.environ else min_age

    return PipelineConfig(
        replays_dir=Path(replays or "/replays"),
        database_path=Path(database or "/data/db/pipeline.sqlite"),
        timezone=timezone,
        min_age_seconds=min_age,
    )
