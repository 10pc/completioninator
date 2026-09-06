"""CLI: osu-pipeline discover / render / compose / status."""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import beatmaps, completion, compositor, database
from .config import load_config
from .discovery import read_beatmap_hash, scan_replays
from .renderer import DanserRenderer

log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="osu-pipeline", description="osu! completionist pipeline")
    p.add_argument("--config", default=None, help="Path to config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="Scan replay dir into SQLite")
    d.add_argument("--scan-root", default=None, help="Override replay directory")
    d.add_argument("--db", default=None, help="Override database path")

    s = sub.add_parser("status", help="Show discovery state")
    s.add_argument("--db", default=None, help="Override database path")

    r = sub.add_parser("render", help="Render pending replays with danser")
    r.add_argument("--limit", type=int, default=None, help="Max jobs this run (default: config)")
    r.add_argument("--db", default=None, help="Override database path")
    r.add_argument("--beatmapset-id", type=int, default=None,
                   help="Skip mirror lookup; use this beatmapset for every job (acceptance testing)")

    q = sub.add_parser("requeue", help="Return failed jobs to pending for retry")
    q.add_argument("--db", default=None, help="Override database path")

    st = sub.add_parser("stop", help="Ask a running render loop to stop after its current job")
    st.add_argument("--db", default=None, help="Override database path")

    pg = sub.add_parser("progress", help="Batch overview for one UTC day")
    pg.add_argument("day", nargs="?", default=None, help="YYYY-MM-DD (default: today UTC)")
    pg.add_argument("--db", default=None, help="Override database path")

    c = sub.add_parser("compose", help="Compose uncomposited renders into a shrinking-grid video")
    c.add_argument("--max-clips", type=int, default=None, help="Keep longest N clips (default: config)")
    c.add_argument("--db", default=None, help="Override database path")

    dl = sub.add_parser("daily", help="Nightly close-out: discover, render, compose")
    dl.add_argument("--limit", type=int, default=None, help="Max renders (default: config)")
    dl.add_argument("--max-clips", type=int, default=None, help="Keep longest N clips (default: config)")
    dl.add_argument("--scan-root", default=None, help="Override replay directory")
    dl.add_argument("--db", default=None, help="Override database path")

    au = sub.add_parser("auth-youtube", help="One-time browser authorization for uploads")
    au.add_argument("--port", type=int, default=8080, help="Local callback port (publish it)")
    au.add_argument("--host", default="0.0.0.0", help="Callback bind address (see uploader docs)")
    au.add_argument("--db", default=None, help="Override database path")

    up = sub.add_parser("upload", help="Upload a daily video")
    up.add_argument("day", help="YYYY-MM-DD of the daily video")
    up.add_argument("--platform", choices=("youtube", "instagram"), default="youtube")
    up.add_argument("--force", action="store_true", help="Re-upload even if recorded")
    up.add_argument("--db", default=None, help="Override database path")
    return p


def _cmd_discover(args, cfg) -> int:
    root = Path(args.scan_root) if args.scan_root else cfg.replays_dir
    db_path = Path(args.db) if args.db else cfg.database_path
    stats = scan_replays(root, db_path, min_age_seconds=cfg.min_age_seconds)
    print(
        f"found={stats['found']} new={stats['new']} "
        f"duplicates={stats['duplicates']} unstable={stats['skipped_unstable']} "
        f"fallbacks={stats['parse_fallbacks']}"
    )
    return 0


def _cmd_status(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.database_path
    if not db_path.exists():
        print(f"No database yet at {db_path}. Run `osu-pipeline discover` first.")
        return 0
    database.init_db(db_path)
    conn = database.connect(db_path)
    try:
        counts = database.get_counts(conn)
        print("Replays:")
        print(f"  discovered:   {counts.get('discovered', 0)}")
        print(f"  pending:      {counts.get('pending', 0)}")
        print(f"  rendering:    {counts.get('rendering', 0)}")
        print(f"  rendered:     {counts.get('rendered', 0)}")
        print(f"  failed:       {counts.get('failed', 0)}")
        print(f"  unrenderable: {counts.get('unrenderable', 0)}")
        print(f"  composited:   {counts.get('composited', 0)}")
        today = datetime.now(timezone.utc).date().isoformat()
        day = database.get_day_summary(conn, today)
        print(f"\nUTC day {today}:")
        print(f"  total:    {day.get('total', 0)}")
        print(f"  pending:  {day.get('pending', 0)}")
        print(f"  rendered: {day.get('rendered', 0)}")
        print(f"  failed:   {day.get('failed', 0)}")
        print("\nRecent days:")
        for row in database.get_distinct_days(conn):
            print(f"  {row['day']}: {row['total']}")
        latest = database.get_latest_daily(conn)
        if latest:
            snap = ""
            if latest.get("passed"):
                snap = f" [{latest['passed']}/{latest.get('left')} ({latest.get('pct')})]"
            print(f"\nLatest daily: {latest['path']} "
                  f"({latest['clips']} clips, day {latest['day']}){snap}")
        uploads = database.recent_uploads(conn)
        if uploads:
            print("\nRecent uploads:")
            for u in uploads:
                print(f"  {u['day']} {u['platform']}: {u['status']}"
                      f"{' ' + u['remote_url'] if u.get('remote_url') else ''}")
    finally:
        conn.close()
    return 0


def _free_gb(path: Path) -> float:
    target = Path(path)
    while not target.exists():
        parent = target.parent
        if parent == target:
            return 0.0
        target = parent
    return shutil.disk_usage(target).free / 1e9


def stop_flag_path(cfg) -> Path:
    """Cooperative-stop sentinel; lives next to the DB so all containers see it."""
    return Path(cfg.database_path).parent / "stop-render"


def _cmd_stop(args, cfg) -> int:
    flag = stop_flag_path(cfg)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    print(f"stop requested ({flag}); the render loop exits after the current job")
    return 0


def run_render(cfg, limit: int, beatmapset_id: int | None = None) -> tuple[int, dict]:
    """Render loop shared by `render` and `daily`. Returns (exit code, stats)."""
    db_path = cfg.database_path
    renderer = DanserRenderer(
        danser_home=cfg.danser_home,
        settings=cfg.danser_settings,
        timeout_seconds=cfg.render_timeout_seconds,
        extra_args=cfg.danser_extra_args,
    )
    if not Path(renderer.cmd_prefix[0]).exists():
        print(f"danser not found: {renderer.cmd_prefix[0]} (PIPELINE_DANSER_HOME={cfg.danser_home})",
              file=sys.stderr)
        return 2, {"rendered": 0, "failed": 0, "skipped_disk": 0, "retried": 0, "unrenderable": 0}
    cfg.working_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    cfg.rendered_dir.mkdir(parents=True, exist_ok=True)

    database.init_db(db_path)
    conn = database.connect(db_path)
    stats = {"rendered": 0, "failed": 0, "skipped_disk": 0, "retried": 0, "unrenderable": 0}
    try:
        stale = database.reset_stale_rendering(conn)
        if stale:
            print(f"requeued {stale} stale rendering job(s)")
        for _ in range(limit):
            if stop_flag_path(cfg).exists():
                stop_flag_path(cfg).unlink(missing_ok=True)
                print("stop requested; exiting after current job (none harmed)")
                break
            free = _free_gb(cfg.rendered_dir)
            if free < cfg.disk_min_free_gb:
                print(f"disk guard: {free:.1f}GB free < {cfg.disk_min_free_gb}GB minimum; stopping run")
                stats["skipped_disk"] += 1
                break
            job = database.claim_pending(conn)
            if job is None:
                break
            try:
                _render_one(conn, cfg, renderer, job, override_set_id=beatmapset_id, stats=stats)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad job must never kill the batch
                log.exception("unexpected error on job-%s", job["id"])
                try:
                    database.mark_failed(conn, job["id"], f"unexpected: {exc}"[-500:])
                    stats["failed"] += 1
                except Exception:
                    log.exception("could not record failure for job-%s", job["id"])
                print(f"[job-{job['id']}] FAILED (unexpected): {exc}")
    except KeyboardInterrupt:
        print("\ninterrupted; current job returns to pending on the next run")
        return 130, stats
    finally:
        conn.close()
    print(f"rendered={stats['rendered']} failed={stats['failed']} "
          f"unrenderable={stats['unrenderable']} "
          f"retried={stats['retried']} skipped_disk={stats['skipped_disk']}")
    return 0, stats


def _with_db(cfg, db_path: Path):
    """Copy of cfg pointing at another database (frozen dataclass)."""
    import dataclasses

    return dataclasses.replace(cfg, database_path=Path(db_path))


def _cmd_render(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.database_path
    limit = args.limit if args.limit is not None else cfg.render_limit_default
    rc, _ = run_render(_with_db(cfg, db_path), limit, args.beatmapset_id)
    return rc


def _diagnose_render_failure(cfg, bhash: str | None, set_id: int | None, result) -> tuple[str, bool]:
    """Turn danser log patterns into (actionable error, unrenderable?).

    unrenderable=True means retrying is pointless (replay pins a map version
    that no longer exists upstream): the job is parked terminally, skipped by
    both render claims and requeue.
    """
    if "Beatmap not found" in result.log_text and set_id:
        if bhash:
            try:
                on_disk = beatmaps.replay_hash_in_songs(cfg.songs_dir, set_id, bhash)
            except Exception:  # noqa: BLE001 - diagnosis must never fail the job
                on_disk = None
            if on_disk is False:
                return ((f"danser: beatmap not found in set {set_id}; replay hash absent "
                         f"from the downloaded .osz bytes (map updated since play; mirrors "
                         f"only host the latest version)"), True)
            if on_disk is True:
                return (f"danser: beatmap not found for set {set_id} though the hash is "
                        f"on disk (dancer DB mismatch; retry may help)"), False
            if cfg.beatmap_backend == "hinamizawa":
                try:
                    current = beatmaps.set_checksums(cfg.beatmap_mirror, set_id)
                except Exception:  # noqa: BLE001
                    current = None
                if current is not None and bhash.lower() not in current:
                    return ((f"danser: beatmap not found in set {set_id}; replay hash absent "
                             f"from current set version (map likely updated since play)"), True)
        return f"danser: beatmap not found for set {set_id} (dancer DB mismatch; retry may help)", False
    return result.error or "render failed", False


def _render_one(conn, cfg, renderer: DanserRenderer, job: dict, override_set_id, stats: dict) -> None:
    jid = job["id"]
    tag = f"job-{jid}"
    if job["attempts"] > cfg.render_max_attempts:
        database.mark_failed(conn, jid, f"gave up after {job['attempts']} attempts: {job.get('error')}")
        stats["failed"] += 1
        print(f"[{tag}] gave up (attempts exceeded)")
        return

    src = cfg.replays_dir / job["path"]
    if not src.exists():
        database.mark_failed(conn, jid, f"source missing: {src}")
        stats["failed"] += 1
        print(f"[{tag}] source missing: {job['path']}")
        return

    scratch = cfg.working_dir / f"{tag}.osr"
    try:
        shutil.copyfile(src, scratch)
    except OSError as exc:
        database.mark_failed(conn, jid, f"scratch copy failed: {exc}")
        stats["failed"] += 1
        print(f"[{tag}] scratch copy failed: {exc}")
        return

    bhash = job.get("beatmap_hash") or read_beatmap_hash(scratch)
    if bhash and not job.get("beatmap_hash"):
        database.set_beatmap(conn, jid, bhash, job.get("beatmapset_id"))

    set_id = job.get("beatmapset_id")
    try:
        set_id, _osz = beatmaps.ensure_beatmap(
            cfg.beatmap_mirror, bhash, cfg.songs_dir, override_set_id=override_set_id,
            osu_client_id=cfg.osu_client_id, osu_client_secret=cfg.osu_client_secret,
            backend=cfg.beatmap_backend,
            fallback_mirror=cfg.fallback_mirror, fallback_backend=cfg.fallback_backend,
        )
        database.set_beatmap(conn, jid, bhash, set_id)
    except beatmaps.BeatmapError as exc:
        if exc.transient:
            # Mirror blip (503/pressure/timeout): back to pending, attempts cap bounds it.
            database.requeue_one(conn, jid, f"transient: {exc}")
            stats["retried"] += 1
            print(f"[{tag}] transient beatmap failure, requeued: {exc}")
            return
        database.mark_failed(conn, jid, str(exc))
        stats["failed"] += 1
        print(f"[{tag}] beatmap unavailable: {exc}")
        return

    result = renderer.render(scratch, tag)
    log_file = cfg.logs_dir / f"{tag}.log"
    try:
        log_file.write_text(result.log_text, encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("could not write job log %s: %s", log_file, exc)
    else:
        print(f"[{tag}] log -> {log_file}")
    # Console keeps only the tail; the full log lives in the file above.
    tail = "\n".join(result.log_text.splitlines()[-8:])
    print(tail)
    scratch.unlink(missing_ok=True)
    if not result.ok:
        cause, unrenderable = _diagnose_render_failure(cfg, bhash, set_id, result)
        if unrenderable:
            database.mark_unrenderable(conn, jid, cause[-500:])
            stats["unrenderable"] += 1
            print(f"[{tag}] UNRENDERABLE: {cause}")
            return
        database.mark_failed(conn, jid, cause[-500:])
        stats["failed"] += 1
        print(f"[{tag}] FAILED: {cause}")
        return

    day = job.get("day") or "unknown-day"
    dest_dir = cfg.rendered_dir / day
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{jid}.mp4"
    try:
        shutil.move(str(result.output), dest)
    except OSError as exc:
        database.mark_failed(conn, jid, f"collect output failed: {exc}")
        stats["failed"] += 1
        print(f"[{tag}] collect failed: {exc}")
        return
    database.mark_rendered(conn, jid, dest.as_posix())
    stats["rendered"] += 1
    print(f"[{tag}] rendered -> {dest} ({result.duration_s}s)" if result.duration_s else f"[{tag}] rendered -> {dest}")


def _cmd_requeue(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.database_path
    database.init_db(db_path)
    conn = database.connect(db_path)
    try:
        n = database.requeue_failed(conn)
    finally:
        conn.close()
    print(f"requeued={n}")
    return 0


def _cmd_progress(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.database_path
    if not db_path.exists():
        print(f"No database yet at {db_path}.")
        return 0
    day = args.day or datetime.now(timezone.utc).date().isoformat()
    database.init_db(db_path)
    conn = database.connect(db_path)
    try:
        summary = database.get_day_summary(conn, day)
        total = summary.get("total", 0)
        if total == 0:
            print(f"Day {day}: no replays.")
            return 0
        rendered = summary.get("rendered", 0)
        pct = 100.0 * rendered / total
        print(f"Day {day}: total={total} pending={summary.get('pending', 0)} "
              f"rendering={summary.get('rendering', 0)} rendered={rendered} "
              f"failed={summary.get('failed', 0)} "
              f"unrenderable={summary.get('unrenderable', 0)} ({pct:.0f}% rendered)")
        rendering = database.get_day_jobs(conn, day, "rendering")
        for j in rendering:
            print(f"  now rendering: job-{j['id']} {j['path']} (attempt {j['attempts']})")
        for j in database.get_day_jobs(conn, day, "failed"):
            print(f"  failed: job-{j['id']} {j['path']}: {(j['error'] or '')[:160]}")
        for j in database.get_day_jobs(conn, day, "unrenderable"):
            print(f"  unrenderable: job-{j['id']} {j['path']}: {(j['error'] or '')[:160]}")
    finally:
        conn.close()
    return 0


def _cmd_compose(args, cfg) -> int:
    """Rolling batch: all rendered-but-uncomposited clips -> shrinking-grid video."""
    try:
        ffmpeg, ffprobe = compositor.check_binaries()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    db_path = Path(args.db) if args.db else cfg.database_path
    max_clips = args.max_clips or cfg.max_clips
    today = datetime.now(timezone.utc).date().isoformat()
    cfg.daily_dir.mkdir(parents=True, exist_ok=True)
    workdir = cfg.working_dir / f"compose-{today}"
    workdir.mkdir(parents=True, exist_ok=True)

    database.init_db(db_path)
    conn = database.connect(db_path)
    try:
        pending = database.get_counts(conn)["pending"]
        if pending:
            print(f"warning: {pending} replay(s) still pending render; composing without them")
        rows = database.get_uncomposited(conn)
    finally:
        conn.close()
    if not rows:
        print("nothing to compose: no rendered-but-uncomposited clips")
        return 0

    # Probe; unreadable outputs are marked failed so triage sees them.
    clips: list[compositor.Clip] = []
    conn = database.connect(db_path)
    try:
        for row in rows:
            src = Path(row["render_path"])
            if not src.exists():
                database.mark_failed(conn, row["id"], f"render output missing: {src}")
                print(f"[job-{row['id']}] render output missing, marked failed")
                continue
            clip = compositor.probe_clip(ffprobe, src)
            if clip is None:
                database.mark_failed(conn, row["id"], f"unreadable render output: {src}")
                print(f"[job-{row['id']}] unreadable output, marked failed")
                continue
            clip.id, clip.day = row["id"], row["day"]
            clips.append(clip)
    finally:
        conn.close()
    if not clips:
        print("nothing composable: all candidates failed probing")
        return 1

    clips.sort(key=lambda c: c.duration, reverse=True)
    kept, rolled = clips[:max_clips], clips[max_clips:]
    days = sorted({c.day for c in kept if c.day})
    span = f"{days[0]}..{days[-1]}" if len(days) > 1 else (days[0] if days else None)
    print(f"batch: {len(kept)} clips ({sum(c.duration for c in kept):.0f}s content), "
          f"{len(rolled)} roll forward to next batch")

    # Snapshot completion stats FIRST, before any encoding: the outro bakes
    # this exact snapshot in, and it is saved with the daily record.
    comp_stats = None
    try:
        comp_stats = completion.fetch_completion(cfg.completion_profile_url)
    except completion.CompletionError as exc:
        log.warning("completion stats fetch failed: %s", exc)
        comp_stats = completion.from_manual(
            cfg.completion_passed, cfg.completion_left, cfg.completion_pct)
    snapshot = None
    if comp_stats:
        snapshot = {"passed": comp_stats.passed, "left": comp_stats.left, "pct": comp_stats.pct}
        print(f"completion snapshot: {comp_stats.line1} ({comp_stats.line2})")
    else:
        print("completion stats unavailable; skipping outro")

    out_path = cfg.daily_dir / f"day-{today}.mp4"
    suffix = 2
    while out_path.exists():
        out_path = cfg.daily_dir / f"day-{today}-{suffix}.mp4"
        suffix += 1

    header = compositor.header_text(today, len(kept), span, cfg.header_extra)
    grid_h = cfg.video_height - cfg.header_height
    timeline = compositor.plan_timeline(kept, morph_s=cfg.morph_seconds,
                                        width=cfg.video_width, grid_h=grid_h,
                                        header_h=cfg.header_height)
    outro_dur = cfg.outro_seconds if comp_stats else 0.0
    n_seg = sum(isinstance(s, compositor.Segment) for s in timeline)
    n_morph = len(timeline) - n_seg
    print(f"encoding {n_seg} static + {n_morph} morph spans"
          f"{' + outro' if outro_dur else ''} -> {out_path}")
    seg_paths = []
    video_tmp = workdir / "video.mp4"
    audio_tmp = workdir / "audio.m4a"
    clips_by_id = {c.id: c for c in kept}
    content_len = max(c.duration for c in kept)
    total_len = content_len + outro_dur
    try:
        for i, span_item in enumerate(timeline):
            seg_path = workdir / f"seg-{i:03d}.mp4"
            graph = workdir / f"seg-{i:03d}.txt"
            seg_timeout = max(600, int(span_item.length * 10) + 120)
            seg_timeout = min(seg_timeout, cfg.compose_timeout)
            last = i == len(timeline) - 1
            if isinstance(span_item, compositor.MorphSpan):
                script, ordered = compositor.build_morph_graph(
                    span_item, clips_by_id, cfg.video_width, grid_h,
                    cfg.header_height, cfg.video_fps, header, cfg.fontfile, 36)
                graph.write_text(script)
                compositor.encode_morph(ffmpeg, span_item, ordered, cfg.video_width, grid_h,
                                        cfg.header_height, cfg.video_fps, graph, seg_path,
                                        cfg.video_preset, cfg.video_crf, seg_timeout)
            else:
                graph.write_text(compositor.build_segment_graph(
                    span_item, cfg.video_width, grid_h,
                    cfg.header_height, cfg.video_fps, header, cfg.fontfile, 36,
                    fade_out=1.0 if last and outro_dur else 0.0))
                compositor.encode_segment(ffmpeg, span_item, graph, seg_path,
                                          cfg.video_fps, cfg.video_preset, cfg.video_crf,
                                          seg_timeout)
            seg_paths.append(seg_path)
        if outro_dur:
            outro_path = workdir / "seg-outro.mp4"
            outro_graph = workdir / "seg-outro.txt"
            outro_graph.write_text(compositor.build_outro_graph(
                cfg.video_width, cfg.video_height, outro_dur,
                comp_stats.line1, comp_stats.line2,
                cfg.fontfile, cfg.outro_fontsize, cfg.outro_fontsize_sub))
            compositor.encode_outro(ffmpeg, outro_graph, outro_path,
                                    cfg.video_preset, cfg.video_crf, 600)
            seg_paths.append(outro_path)
        compositor.concat_segments(ffmpeg, seg_paths, video_tmp, workdir)
        audio_script, has_audio = compositor.build_audio_graph(kept, content_len, total_len)
        if has_audio:
            (workdir / "audio.txt").write_text(audio_script)
            compositor.encode_audio_mix(ffmpeg, kept, workdir / "audio.txt", audio_tmp,
                                        min(max(300, int(total_len)), cfg.compose_timeout))
            compositor.mux_audio_video(ffmpeg, video_tmp, audio_tmp, out_path)
        else:
            compositor.mux_audio_video(ffmpeg, video_tmp, None, out_path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail = "\n".join(str(exc.stderr).splitlines()[-15:])
        print(f"compose FAILED: {exc}\n{detail} (segments kept under {workdir})", file=sys.stderr)
        return 1

    final = compositor.probe_clip(ffprobe, out_path)
    total = sum(c.duration for c in kept)
    print(f"composed {out_path} ({final.duration:.0f}s)" if final else f"composed {out_path}")
    for p in seg_paths:
        p.unlink(missing_ok=True)
    for p in workdir.glob("seg-*.txt"):
        p.unlink(missing_ok=True)
    (workdir / "concat.txt").unlink(missing_ok=True)
    (workdir / "audio.txt").unlink(missing_ok=True)
    (workdir / "seg-outro.txt").unlink(missing_ok=True)
    video_tmp.unlink(missing_ok=True)
    audio_tmp.unlink(missing_ok=True)
    conn = database.connect(db_path)
    try:
        database.mark_composited(conn, [c.id for c in kept])
        database.record_daily(conn, today, out_path.as_posix(), len(kept),
                              final.duration if final else total, span, snapshot)
    finally:
        conn.close()
    return 0


def _cmd_daily(args, cfg) -> int:
    """Nightly close-out: discover -> render -> compose-if-new-renders."""
    cfg = _with_db(cfg, Path(args.db) if args.db else cfg.database_path)
    root = Path(args.scan_root) if args.scan_root else cfg.replays_dir

    try:
        scan = scan_replays(root, cfg.database_path, min_age_seconds=cfg.min_age_seconds)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"discover: found={scan['found']} new={scan['new']}")

    rc, rstats = run_render(cfg, args.limit if args.limit is not None else cfg.render_limit_default)
    summary = (f"daily: discovered new={scan['new']} rendered={rstats['rendered']} "
               f"failed={rstats['failed']}")
    if rc == 130:
        print(summary + " composed=skipped (interrupted)")
        return 130
    if rc != 0:
        return rc

    composed = "skipped"
    if rstats["rendered"] > 0:
        crc = _cmd_compose(argparse.Namespace(db=str(cfg.database_path), max_clips=args.max_clips), cfg)
        composed = "ok" if crc == 0 else "failed"
        print(summary + f" composed={composed}")
        return crc
    print(summary + f" composed={composed}")
    return 0


def _cmd_auth_youtube(args, cfg) -> int:
    from . import uploader

    if not cfg.youtube_client_id or not cfg.youtube_client_secret:
        print("youtube client ID/secret not configured "
              "(PIPELINE_YOUTUBE_CLIENT_ID / PIPELINE_YOUTUBE_CLIENT_SECRET)", file=sys.stderr)
        return 2
    print("Open this URL in the VNC browser if it doesn't open by itself, "
          "approve, and wait for the callback "
          f"(publish port with: docker compose run --rm -p 127.0.0.1:{args.port}:{args.port} "
          f"pipeline auth-youtube --port {args.port})")
    uploader.run_auth_flow(cfg.youtube_client_id, cfg.youtube_client_secret,
                           cfg.youtube_token_path, port=args.port, host=args.host)
    return 0


def _cmd_upload(args, cfg) -> int:
    from . import uploader

    db_path = Path(args.db) if args.db else cfg.database_path
    platform = args.platform
    database.init_db(db_path)
    conn = database.connect(db_path)
    try:
        existing = database.get_upload(conn, args.day, platform)
        if existing and existing["status"] == "uploaded" and not args.force:
            print(f"day {args.day} already on {platform}: {existing.get('remote_url')} (use --force)")
            return 0
        row = conn.execute("SELECT * FROM daily WHERE day = ?", (args.day,)).fetchone()
        if row is None:
            print(f"no daily video for {args.day}; compose one first", file=sys.stderr)
            return 2
        video = Path(row["path"])
        if not video.exists():
            print(f"daily file missing: {video}", file=sys.stderr)
            return 2
        if platform == "youtube":
            return _upload_youtube(conn, cfg, args, dict(row), video)
        return _upload_instagram(conn, cfg, args, dict(row), video)
    finally:
        conn.close()


def _upload_youtube(conn, cfg, args, row: dict, video: Path) -> int:
    from . import uploader

    if not cfg.youtube_client_id or not cfg.youtube_client_secret:
        print("youtube client ID/secret not configured", file=sys.stderr)
        return 2
    creds = uploader.load_credentials(cfg.youtube_token_path)
    if creds is None:
        print(f"not authorized; run auth-youtube first (token: {cfg.youtube_token_path})",
              file=sys.stderr)
        return 2
    title = uploader.render_title(cfg.youtube_title_template, args.day,
                                  row["clips"], row["span"])
    description = (
        f"osu!standard completionist daily grid — {row['clips']} maps "
        f"({row['span'] or args.day}).\n"
        + (f"Completion at compose time: {row['passed']}/{row['left']} ({row['pct']}).\n"
           if row["passed"] else "")
        + "Rendered with danser; composed by the completioninator pipeline."
    )
    database.mark_uploading(conn, args.day, "youtube")
    service = uploader.build_service(creds)
    try:
        video_id = uploader.upload_video(
            service, video, title, description,
            cfg.youtube_category_id, cfg.youtube_privacy)
    except uploader.UploadError as exc:
        database.mark_upload_failed(conn, args.day, "youtube", str(exc)[-500:])
        print(f"upload FAILED: {exc}", file=sys.stderr)
        return 1
    url = uploader.video_url(video_id)
    database.mark_uploaded(conn, args.day, "youtube", video_id, url)
    print(f"uploaded {args.day} -> {url}")
    return 0


def _upload_instagram(conn, cfg, args, row: dict, video: Path) -> int:
    from . import instagram

    if not cfg.instagram_user_id or not cfg.instagram_token:
        print("instagram user ID/token not configured "
              "(PIPELINE_INSTAGRAM_USER_ID / PIPELINE_INSTAGRAM_TOKEN)", file=sys.stderr)
        return 2
    caption = cfg.instagram_caption_template.format(
        day=args.day, clips=row["clips"], span=row["span"] or args.day)
    database.mark_uploading(conn, args.day, "instagram")
    try:
        media_id, link = instagram.publish_video(
            cfg.instagram_api_version, cfg.instagram_user_id,
            cfg.instagram_token, video, caption)
    except instagram.InstagramError as exc:
        if "190" in str(exc) or "expired" in str(exc).lower() or "invalid" in str(exc).lower():
            hint = " (token expired or invalid: re-issue a long-lived token)"
        else:
            hint = ""
        database.mark_upload_failed(conn, args.day, "instagram", str(exc)[-500:])
        print(f"upload FAILED: {exc}{hint}", file=sys.stderr)
        return 1
    database.mark_uploaded(conn, args.day, "instagram", media_id, link)
    print(f"uploaded {args.day} -> {link or media_id}")
    return 0


def main(argv: list | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = load_config(args.config)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.command == "discover":
        return _cmd_discover(args, cfg)
    if args.command == "status":
        return _cmd_status(args, cfg)
    if args.command == "render":
        return _cmd_render(args, cfg)
    if args.command == "requeue":
        return _cmd_requeue(args, cfg)
    if args.command == "stop":
        return _cmd_stop(args, cfg)
    if args.command == "progress":
        return _cmd_progress(args, cfg)
    if args.command == "compose":
        return _cmd_compose(args, cfg)
    if args.command == "daily":
        return _cmd_daily(args, cfg)
    if args.command == "auth-youtube":
        return _cmd_auth_youtube(args, cfg)
    if args.command == "upload":
        return _cmd_upload(args, cfg)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
