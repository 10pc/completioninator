"""Domain models for Milestone 1."""

from __future__ import annotations

from dataclasses import dataclass

# Replay lifecycle states relevant to M1 (full set reserved for later milestones).
STATUS_PENDING = "pending"
STATUS_RENDERING = "rendering"
STATUS_RENDERED = "rendered"
STATUS_FAILED = "failed"
STATUS_UNRENDERABLE = "unrenderable"
STATUS_COMPOSITED = "composited"
STATUS_UPLOADED = "uploaded"

ALL_STATUSES = (
    STATUS_PENDING,
    STATUS_RENDERING,
    STATUS_RENDERED,
    STATUS_FAILED,
    STATUS_UNRENDERABLE,
    STATUS_COMPOSITED,
    STATUS_UPLOADED,
)


@dataclass
class Replay:
    path: str  # relative POSIX path from replay root
    sha256: str
    size: int
    mtime_ns: int
    played_at: str | None  # ISO8601 UTC
    day: str | None  # YYYY-MM-DD in UTC
    status: str = STATUS_PENDING
    error: str | None = None
