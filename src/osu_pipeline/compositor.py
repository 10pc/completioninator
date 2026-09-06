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


def grid_dims(k: int, aspect: float = 1.92) -> tuple[int, int]:
    """Columns/rows for k tiles on a ~16:9-ish area (usableW/usableH)."""
    import math

    if k <= 0:
        raise ValueError("grid needs at least one clip")
    cols = max(1, round(math.sqrt(k * aspect)))
    rows = math.ceil(k / cols)
    return cols, rows


def tile_size(cols: int, rows: int, width: int, grid_h: int) -> tuple[int, int]:
    """Tile box as exact multiples of 32x18: always precisely 16:9 and even.

    m is the largest integer fitting both dimensions, so rows*th never
    overflows the grid area; leftover space is distributed by centering.
    Linear lerps between such boxes stay exactly 16:9 at every instant.
    """
    m = max(1, min((width // cols) // 32, (grid_h // rows) // 18))
    return 32 * m, 18 * m


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int


def layout_rects(cols: int, rows: int, k: int, tw: int, th: int, width: int,
                 grid_h: int, y0: int) -> list[Rect]:
    """Tile boxes in grid-position order, block centered in the grid area.

    Ragged last rows are centered too, so margins are never lopsided. Every
    box is 16:9 by construction (see tile_size), matching the 16:9 sources,
    so plain scaling stays aspect-exact with no letterbox bars.
    """
    y_base = y0 + max(0, (grid_h - rows * th) // 2)
    rects = []
    for i in range(k):
        row, col = i // cols, i % cols
        row_count = min(cols, k - row * cols)
        x_base = max(0, (width - row_count * tw) // 2)
        rects.append(Rect(x_base + col * tw, y_base + row * th, tw, th))
    return rects


@dataclass
class MorphTile:
    clip_id: int
    ax: int
    ay: int
    aw: int
    ah: int
    bx: int
    by: int
    bw: int
    bh: int
    dying: bool


@dataclass
class MorphSpan:
    start: float
    end: float
    tiles: list[MorphTile] = field(default_factory=list)

    @property
    def length(self) -> float:
        return self.end - self.start


def plan_timeline(clips: list[Clip], morph_s: float = 1.0,
                  min_morph: float = 0.25, width: int = 1920,
                  grid_h: int = 1000, header_h: int = 80) -> list:
    """Static grid spans glued by animated morphs.

    Each morph occupies a finish time's final `morph_s` seconds, so dying
    tiles play out while shrinking into their survivors' layout. Windows too
    small for `min_morph` degrade to hard cuts. Returns Segment/MorphSpan in
    time order; durations always tile exactly (no gaps, no overlaps).
    """
    by_position = sorted(clips, key=lambda c: (c.day or "", c.id))
    ends: list[float] = []
    for c in sorted(clips, key=lambda c: c.duration):
        if not ends or c.duration > ends[-1]:
            ends.append(c.duration)
    spans: list = []
    cursor = 0.0
    for end in ends:
        alive_here = [c for c in by_position if c.duration > cursor]
        survivors = [c for c in by_position if c.duration > end]
        window = min(morph_s, end - cursor)
        if survivors and window >= min_morph:
            m_start = end - window
            if m_start > cursor:
                spans.append(Segment(start=cursor, end=m_start, active=list(alive_here)))
            spans.append(MorphSpan(
                start=m_start, end=end,
                tiles=_morph_tiles(alive_here, survivors, width, grid_h, header_h)))
        else:
            if end > cursor and alive_here:
                spans.append(Segment(start=cursor, end=end, active=list(alive_here)))
        cursor = end
    tail = [c for c in by_position if c.duration > cursor]
    if tail:
        spans.append(Segment(start=cursor, end=max(c.duration for c in tail), active=tail))
    return spans


def _morph_tiles(alive: list[Clip], survivors: list[Clip],
                 width: int, grid_h: int, header_h: int) -> list[MorphTile]:
    """Match clips to rects in both layouts; dying tiles shrink to their center."""
    fc, fr = grid_dims(len(alive))
    tc, tr = grid_dims(len(survivors))
    ftw, fth = tile_size(fc, fr, width, grid_h)
    ttw, tth = tile_size(tc, tr, width, grid_h)
    from_rects = layout_rects(fc, fr, len(alive), ftw, fth, width, grid_h, header_h)
    to_rects = layout_rects(tc, tr, len(survivors), ttw, tth, width, grid_h, header_h)
    to_by_id = {c.id: r for c, r in zip(survivors, to_rects)}
    tiles = []
    for clip, fr_rect in zip(alive, from_rects):
        to = to_by_id.get(clip.id)
        if to is None:
            cx, cy = fr_rect.x + fr_rect.w // 2, fr_rect.y + fr_rect.h // 2
            tiles.append(MorphTile(clip.id, fr_rect.x, fr_rect.y, fr_rect.w, fr_rect.h,
                                   cx - 8, cy - 4, 16, 9, True))
        else:
            tiles.append(MorphTile(clip.id, fr_rect.x, fr_rect.y, fr_rect.w, fr_rect.h,
                                   to.x, to.y, to.w, to.h, False))
    return tiles


def header_text(date: str, total: int, span: str | None = None, extra: str = "") -> str:
    """Exact format: `dd-mm-yyyy | x maps` (span/extra kept out by default)."""
    try:
        y, m, d = date.split("-")
        head = f"{d}-{m}-{y} | {total} maps"
    except ValueError:
        head = f"{date} | {total} maps"
    if extra:
        head += f" | {extra}"
    return head


def drawtext_filter(text: str, fontfile: str, fontsize: int, y: int,
                    fontcolor: str = "white") -> str:
    safe = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")
    return (f"drawtext=fontfile='{fontfile}':text='{safe}':fontsize={fontsize}:"
            f"fontcolor={fontcolor}:x=(w-text_w)/2:y={y}")


def build_segment_graph(seg: Segment, width: int, grid_h: int,
                         header_h: int, fps: int, header: str, fontfile: str,
                         fontsize: int, fade_out: float = 0.0) -> str:
    """Video-only graph for one static span. Returns the filter_complex script.

    Tiles are exactly 16:9 like the sources, so plain scaling is aspect-exact
    with no letterbox bars. Transitions between spans are handled by morph
    spans (below). Audio is built separately as one continuous mix.
    """
    k = len(seg.active)
    cols, rows = grid_dims(k)
    tw, th = tile_size(cols, rows, width, grid_h)
    rects = layout_rects(cols, rows, k, tw, th, width, grid_h, header_h)
    chains = []
    if k == 1:
        # xstack needs >= 2 inputs; position the lone tile via pad offsets.
        r = rects[0]
        chains.append(f"[0:v]scale={tw}:{th},setsar=1,"
                      f"fps={fps},setpts=PTS-STARTPTS,"
                      f"pad={width}:{grid_h}:{r.x}:{r.y - header_h}:black[vgrid]")
    else:
        for i, r in enumerate(rects):
            chains.append(
                f"[{i}:v]scale={tw}:{th},setsar=1,"
                f"fps={fps},setpts=PTS-STARTPTS[v{i}]")
        labels = "".join(f"[v{i}]" for i in range(k))
        layout = "|".join(f"{r.x}_{r.y}" for r in rects)
        chains.append(
            f"{labels}xstack=inputs={k}:layout={layout}:fill=black[vgrid]")
    chains.append(f"[vgrid]{drawtext_filter(header, fontfile, fontsize, (header_h - fontsize) // 2)}[vhead]")
    if fade_out > 0 and seg.length > fade_out:
        chains.append(f"[vhead]pad={width}:{grid_h + header_h}:0:0:black[vpad]")
        chains.append(f"[vpad]fade=t=out:st={seg.length - fade_out:.3f}:d={fade_out:.3f}[vout]")
    else:
        chains.append(f"[vhead]pad={width}:{grid_h + header_h}:0:0:black[vout]")
    return ";\n".join(chains) + "\n"


def build_outro_graph(width: int, height: int, duration: float, line1: str, line2: str,
                      fontfile: str, title_size: int, sub_size: int) -> str:
    """Black canvas with two centered stat lines fading in, holding, fading out.

    Single lavfi color input; text alpha is animated (format=rgba + alpha
    fades) so the lines dissolve in and out instead of popping.
    """
    y1 = height // 2 - title_size
    y2 = height // 2 + 10
    d_in, d_out = 1.0, 1.0
    # Plain luma fades (NOT alpha): on a black canvas they are visually
    # identical, and alpha fade-outs silently do nothing on some builds.
    return (
        f"color=black:size={width}x{height}:rate=30:duration={duration:.3f}[bg];\n"
        f"[bg]{drawtext_filter(line1, fontfile, title_size, y1)}[t1];\n"
        f"[t1]{drawtext_filter(line2, fontfile, sub_size, y2)}[t2];\n"
        f"[t2]fade=t=in:st=0:d={d_in:.3f},"
        f"fade=t=out:st={max(0.0, duration - d_out):.3f}:d={d_out:.3f}[vout]\n"
    )


def encode_outro(ffmpeg: str, graph_file: Path, out_path: Path,
                 preset: str, crf: int, timeout: int) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error", "-filter_complex_script", str(graph_file),
           "-map", "[vout]", "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
           "-pix_fmt", "yuv420p", "-r", "30", "-an", str(out_path)]
    log.info("encoding outro")
    subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)


def _interp(a: int, b: int, d: float) -> str:
    """Filter expression lerping a->b over d seconds of stream time."""
    return f"{a}+({b}-{a})*min(max(t/{d}\\,0)\\,1)"


def build_morph_graph(span: MorphSpan, clips_by_id: dict, width: int, grid_h: int,
                      header_h: int, fps: int, header: str, fontfile: str,
                      fontsize: int) -> tuple[str, list]:
    """Animated glide between two grid layouts over the span's duration.

    Every tile lerps scale + position per frame; dying tiles shrink to their
    own center while fading out. Inputs = the span's clips in tile order
    (index 0 is the black base canvas). Returns (script, ordered clip list).
    """
    d = span.length
    ordered = [clips_by_id[t.clip_id] for t in span.tiles]
    chains = [f"color=black:size={width}x{grid_h + header_h}:rate={fps}:duration={d:.3f}[bg]",
              f"[bg]{drawtext_filter(header, fontfile, fontsize, (header_h - fontsize) // 2)}[m0]"]
    prev = "m0"
    for n, (tile, clip) in enumerate(zip(span.tiles, ordered)):
        tag = f"mt{n}"
        chains.append(
            f"[{n + 1}:v]scale=w='{_interp(tile.aw, tile.bw, d)}':"
            f"h='{_interp(tile.ah, tile.bh, d)}':eval=frame,"
            f"setsar=1,fps={fps},setpts=PTS-STARTPTS"
            + (",format=rgba,fade=t=out:st=0:d={:.3f}:alpha=1".format(d) if tile.dying else "")
            + f"[{tag}]")
        chains.append(
            f"[{prev}][{tag}]overlay=x='{_interp(tile.ax, tile.bx, d)}':"
            f"y='{_interp(tile.ay, tile.by, d)}':eval=frame[m{n + 1}]")
        prev = f"m{n + 1}"
    chains.append(f"[{prev}]pad={width}:{grid_h + header_h}:0:0:black[vout]")
    return ";\n".join(chains) + "\n", ordered


def build_audio_graph(clips: list[Clip], content_len: float, total_len: float) -> tuple[str, bool]:
    """One continuous mix of every clip that has audio (full timeline, no seeks).

    Finished clips fall silent as their inputs end, so the mix naturally
    thins out exactly like the grid. The tail fades out over the final 2s of
    content, then silence pads through the outro. Returns (script, has_audio).
    """
    voiced = [c for c in clips if c.has_audio]
    if not voiced:
        return "", False
    chains = []
    for i in range(len(voiced)):
        chains.append(f"[{i}:a]aresample=48000,asetpts=PTS-STARTPTS[a{i}]")
    if len(voiced) == 1:
        chains.append("[a0]anull[amix]")
    else:
        labels = "".join(f"[a{i}]" for i in range(len(voiced)))
        chains.append(f"{labels}amix=inputs={len(voiced)}:duration=longest:normalize=1[amix]")
    tail = f",afade=t=out:st={max(0.0, content_len - 2):.3f}:d=2" if content_len > 2 else ""
    chains.append(f"[amix]aformat=sample_rates=48000:channel_layouts=stereo{tail},"
                  f"apad=whole_dur={total_len:.3f}[aout]")
    return ";\n".join(chains) + "\n", True


def encode_audio_mix(ffmpeg: str, clips: list[Clip], graph_file: Path,
                     out_path: Path, timeout: int) -> None:
    voiced = [c for c in clips if c.has_audio]
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for c in voiced:
        cmd += ["-i", str(c.path)]
    cmd += ["-filter_complex_script", str(graph_file),
            "-map", "[aout]", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
            str(out_path)]
    log.info("encoding full-timeline audio mix (%d tracks)", len(voiced))
    subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)


def mux_audio_video(ffmpeg: str, video_path: Path, audio_path: Path | None,
                    out_path: Path, timeout: int = 600) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video_path)]
    if audio_path is not None:
        cmd += ["-i", str(audio_path), "-map", "0:v", "-map", "1:a"]
    cmd += ["-c", "copy", "-shortest", "-movflags", "+faststart", str(out_path)]
    subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)


def encode_segment(ffmpeg: str, seg: Segment, graph_file: Path,
                   out_path: Path, fps: int, preset: str, crf: int, timeout: int) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for c in seg.active:
        cmd += ["-ss", f"{seg.start:.3f}", "-i", str(c.path)]
    cmd += ["-filter_complex_script", str(graph_file),
            "-map", "[vout]"]
    cmd += ["-t", f"{seg.length:.3f}",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-an", str(out_path)]
    log.info("encoding static %.1fs-%.1fs (%d clips)", seg.start, seg.end, len(seg.active))
    subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)


def encode_morph(ffmpeg: str, span: MorphSpan, ordered_clips: list[Clip],
                 width: int, grid_h: int, header_h: int, fps: int,
                 graph_file: Path, out_path: Path,
                 preset: str, crf: int, timeout: int) -> None:
    d = span.length
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
           f"color=black:size={width}x{grid_h + header_h}:rate={fps}:duration={d:.3f}"]
    for c in ordered_clips:
        cmd += ["-ss", f"{span.start:.3f}", "-i", str(c.path)]
    cmd += ["-filter_complex_script", str(graph_file),
            "-map", "[vout]",
            "-t", f"{d:.3f}",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-an", str(out_path)]
    log.info("encoding morph %.1fs-%.1fs (%d tiles)", span.start, span.end, len(ordered_clips))
    subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)


def concat_segments(ffmpeg: str, seg_paths: list[Path], out_path: Path,
                    workdir: Path, timeout: int = 600) -> None:
    lst = workdir / "concat.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in seg_paths))
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
           "-i", str(lst), "-c", "copy", str(out_path)]
    subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
