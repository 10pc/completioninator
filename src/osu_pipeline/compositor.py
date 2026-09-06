"""Giant shrinking-grid composer (Milestone 4).

Rolling batch model: every `compose` run consumes ALL rendered-but-uncomposited
clips (any day — old days roll forward), keeps the longest `max_clips`
(default 12), and encodes a fixed-canvas video whose grid shrinks as clips
finish, ending on the longest clip alone.

Timeline = segments split at each clip's finish time (<= N segments for N
clips). Every segment is the same canvas/codec/fps, so the final join is a
lossless concat-demuxer copy. Audio throughout = the longest clip's track.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Clip:
    id: int
    path: Path
    day: str | None
    duration: float
    has_audio: bool


@dataclass
class Segment:
    start: float
    end: float
    active: list[Clip] = field(default_factory=list)

    @property
    def length(self) -> float:
        return self.end - self.start


def check_binaries() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("compose needs ffmpeg + ffprobe on PATH (pip image has neither)")
    return ffmpeg, ffprobe


def probe_clip(ffprobe: str, path: Path, timeout: int = 60) -> Clip | None:
    """Probe one rendered MP4. None when unreadable (caller marks it failed)."""
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error",
             "-show_entries", "format=duration",
             "-show_entries", "stream=codec_type",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
        data = json.loads(proc.stdout)
        duration = float(data["format"]["duration"])
        has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
        if duration <= 0:
            return None
        return Clip(id=-1, path=path, day=None, duration=duration, has_audio=has_audio)
    except Exception as exc:  # noqa: BLE001 - probe failure just skips the clip
        log.warning("probe failed for %s: %s", path, exc)
        return None


def plan_segments(clips: list[Clip]) -> list[Segment]:
    """Split the timeline at each clip's finish; active = clips outliving t.

    Grid positions follow (day, id) order; segment count never exceeds len(clips).
    """
    ordered = sorted(clips, key=lambda c: c.duration)
    ends: list[float] = []
    for c in ordered:
        if not ends or c.duration > ends[-1]:
            ends.append(c.duration)
    by_position = sorted(clips, key=lambda c: (c.day or "", c.id))
    segments = []
    prev = 0.0
    for end in ends:
        if end <= prev:
            continue
        active = [c for c in by_position if c.duration > prev]
        if active:
            segments.append(Segment(start=prev, end=end, active=active))
        prev = end
    return segments


def grid_dims(k: int, aspect: float = 1.92) -> tuple[int, int]:
    """Columns/rows for k tiles on a ~16:9-ish area (usableW/usableH)."""
    import math

    if k <= 0:
        raise ValueError("grid needs at least one clip")
    cols = max(1, round(math.sqrt(k * aspect)))
    rows = math.ceil(k / cols)
    return cols, rows


def tile_size(cols: int, rows: int, width: int, grid_h: int) -> tuple[int, int]:
    """Integer tile size, rounded down to even (x264 yuv420p needs even dims)."""
    tw = max(2, (width // cols) // 2 * 2)
    th = max(2, (grid_h // rows) // 2 * 2)
    return tw, th


def layout_string(cols: int, rows: int, k: int, tw: int, th: int, y0: int) -> str:
    """Absolute-pixel xstack layout, row-major; ragged last row needs no padding."""
    parts = []
    for i in range(k):
        parts.append(f"{(i % cols) * tw}_{y0 + (i // cols) * th}")
    return "|".join(parts)


def header_text(date: str, total: int, span: str | None, extra: str = "") -> str:
    text = f"OSU! COMPLETIONIST -- {date} -- {total} MAPS"
    if span:
        text += f" -- {span}"
    if extra:
        text += f" -- {extra}"
    return text


def drawtext_filter(text: str, fontfile: str, fontsize: int, y: int,
                    fontcolor: str = "white") -> str:
    safe = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")
    return (f"drawtext=fontfile='{fontfile}':text='{safe}':fontsize={fontsize}:"
            f"fontcolor={fontcolor}:x=(w-text_w)/2:y={y}")


def build_segment_graph(seg: Segment, longest_idx: int, width: int, grid_h: int,
                         header_h: int, fps: int, header: str, fontfile: str,
                         fontsize: int) -> tuple[str, str]:
    """Return (filter_complex_script, audio_label_or_empty) for one segment."""
    k = len(seg.active)
    cols, rows = grid_dims(k)
    tw, th = tile_size(cols, rows, width, grid_h)
    chains = []
    if k == 1:
        # xstack needs >= 2 inputs; a lone survivor just fills the grid area.
        chains.append(f"[0:v]scale={width}:{grid_h},setsar=1,fps={fps},setpts=PTS-STARTPTS[vgrid]")
    else:
        for i in range(k):
            chains.append(
                f"[{i}:v]scale={tw}:{th},setsar=1,fps={fps},setpts=PTS-STARTPTS[v{i}]")
        labels = "".join(f"[v{i}]" for i in range(k))
        layout = layout_string(cols, rows, k, tw, th, header_h)
        chains.append(
            f"{labels}xstack=inputs={k}:layout={layout}:fill=black[vgrid]")
    chains.append(f"[vgrid]{drawtext_filter(header, fontfile, fontsize, (header_h - fontsize) // 2)}[vhead]")
    chains.append(f"[vhead]pad={width}:{grid_h + header_h}:0:0:black[vout]")
    audio = ""
    order = [longest_idx] + [i for i in range(k) if i != longest_idx]
    for i in order:
        if seg.active[i].has_audio:
            chains.append(f"[{i}:a]aresample=48000,asetpts=PTS-STARTPTS[aout]")
            audio = "[aout]"
            break
    return ";\n".join(chains) + "\n", audio


def longest_index(active: list[Clip]) -> int:
    return max(range(len(active)), key=lambda i: active[i].duration)


def encode_segment(ffmpeg: str, seg: Segment, graph_file: Path, audio_label: str,
                   out_path: Path, fps: int, preset: str, crf: int, timeout: int) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for c in seg.active:
        cmd += ["-ss", f"{seg.start:.3f}", "-i", str(c.path)]
    cmd += ["-filter_complex_script", str(graph_file),
            "-map", "[vout]"]
    if audio_label:
        cmd += ["-map", audio_label]
    cmd += ["-t", f"{seg.length:.3f}",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-r", str(fps)]
    if audio_label:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    else:
        cmd += ["-an"]
    cmd += [str(out_path)]
    log.info("encoding segment %.1fs-%.1fs (%d clips)", seg.start, seg.end, len(seg.active))
    subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)


def concat_segments(ffmpeg: str, seg_paths: list[Path], out_path: Path,
                    workdir: Path, timeout: int = 600) -> None:
    lst = workdir / "concat.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in seg_paths))
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
           "-i", str(lst), "-c", "copy", str(out_path)]
    subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
