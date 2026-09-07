"""CLI: osu-pipeline discover / render / compose / status."""

from __future__ import annotations

import argparse
import contextlib
import logging
import shutil
import subprocess
import sys
import threading
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
    r.add_argument("--workers", type=int, default=None, help="Parallel workers (default: config)")
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

    dl = sub.add_parser("daily", help="Nightly close-out: discover, render, compose, upload")
    dl.add_argument("--limit", type=int, default=None, help="Max renders (default: config)")
    dl.add_argument("--workers", type=int, default=None, help="Parallel workers (default: config)")
    dl.add_argument("--max-clips", type=int, default=None, help="Keep longest N clips (default: config)")
    dl.add_argument("--upload", action="store_true", help="Publish backlog to configured platforms")
    dl.add_argument("--scan-root", default=None, help="Override replay directory")
    dl.add_argument("--db", default=None, help="Override database path")

    au = sub.add_parser("auth-youtube", help="One-time browser authorization for uploads")
    au.add_argument("--port", type=int, default=8080, help="Local callback port (publish it)")
    au.add_argument("--host", default="localhost", help="Redirect hostname (server binds all interfaces)")
    au.add_argument("--manual", action="store_true",
                    help="Print URL and read pasted callback from stdin (no callback server)")
    au.add_argument("--db", default=None, help="Override database path")

    up = sub.add_parser("upload", help="Upload a daily video")
    up.add_argument("day", help="YYYY-MM-DD of the daily video")
    up.add_argument("--platform", choices=("youtube", "instagram"), default="youtube")
    up.add_argument("--force", action="store_true", help="Re-upload even if recorded")
    up.add_argument("--db", default=None, help="Override database path")

    pr = sub.add_parser("prune", help="Delete render files covered by YouTube uploads")
    pr.add_argument("--day", default=None, help="Prune one uploaded day")
    pr.add_argument("--backfill", action="store_true", help="Prune pre-membership renders")
    pr.add_argument("--dry-run", action="store_true", help="Report without deleting")
    pr.add_argument("--force", action="store_true", help="Prune a day with no upload record")
    pr.add_argument("--db", default=None, help="Override database path")

    ex = sub.add_parser("exclude", help="Park old testing-era replays out of future grids")
    ex.add_argument("--before", required=True, help="Exclude rendered/composited rows with day < YYYY-MM-DD")
    ex.add_argument("--dry-run", action="store_true", help="Report without changing")
    ex.add_argument("--db", default=None, help="Override database path")
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
        print(f"  excluded:     {counts.get('excluded', 0)}")
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


def _fresh_stats() -> dict:
    return {"rendered": 0, "failed": 0, "skipped_disk": 0, "retried": 0, "unrenderable": 0}


def _cmd_stop(args, cfg) -> int:
    flag = stop_flag_path(cfg)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    print(f"stop requested ({flag}); the render loop exits after the current job")
    return 0


def _worker_loop(wid: int, cfg, renderer, beatmapset_id, state,
                 stop_event: threading.Event, ensure_lock: threading.Lock) -> dict:
    """One worker: own DB connection, claims until budget out / queue empty / told to stop."""
    conn = database.connect(cfg.database_path)
    local = _fresh_stats()
    try:
        while not stop_event.is_set():
            with state["lock"]:
                if state["remaining"] <= 0:
                    break
                state["remaining"] -= 1
            if stop_flag_path(cfg).exists():
                stop_flag_path(cfg).unlink(missing_ok=True)
                print("stop requested; exiting after current job (none harmed)")
                stop_event.set()
                break
            if _free_gb(cfg.rendered_dir) < cfg.disk_min_free_gb:
                print(f"disk guard: low space; worker {wid} stopping run")
                local["skipped_disk"] += 1
                stop_event.set()
                break
            job = database.claim_pending(conn)
            if job is None:
                break
            try:
                _render_one(conn, cfg, renderer, job, override_set_id=beatmapset_id,
                            stats=local, ensure_lock=ensure_lock)
            except Exception as exc:  # noqa: BLE001 - one bad job must never kill the batch
                log.exception("unexpected error on job-%s", job["id"])
                try:
                    database.mark_failed(conn, job["id"], f"unexpected: {exc}"[-500:])
                    local["failed"] += 1
                except Exception:
                    log.exception("could not record failure for job-%s", job["id"])
                print(f"[job-{job['id']}] FAILED (unexpected): {exc}")
    finally:
        conn.close()
    return local


def run_render(cfg, limit: int, beatmapset_id: int | None = None,
               workers: int | None = None) -> tuple[int, dict]:
    """Render loop shared by `render` and `daily`. Returns (exit code, stats)."""
    db_path = cfg.database_path
    n_workers = max(1, workers or cfg.render_workers)
    renderer = DanserRenderer(
        danser_home=cfg.danser_home,
        settings=cfg.danser_settings,
        timeout_seconds=cfg.render_timeout_seconds,
        extra_args=cfg.danser_extra_args,
        skin=cfg.danser_skin,
    )
    if not Path(renderer.cmd_prefix[0]).exists():
        print(f"danser not found: {renderer.cmd_prefix[0]} (PIPELINE_DANSER_HOME={cfg.danser_home})",
              file=sys.stderr)
        return 2, _fresh_stats()
    cfg.working_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    cfg.rendered_dir.mkdir(parents=True, exist_ok=True)

    database.init_db(db_path)
    conn = database.connect(db_path)
    try:
        stale = database.reset_stale_rendering(conn, cfg.stale_after_minutes)
        if stale:
            print(f"requeued {stale} stale rendering job(s)")
    finally:
        conn.close()

    state = {"remaining": max(0, limit), "lock": threading.Lock()}
    stop_event = threading.Event()
    ensure_lock = threading.Lock()
    if n_workers == 1:
        try:
            stats = _worker_loop(0, cfg, renderer, beatmapset_id, state, stop_event, ensure_lock)
        except KeyboardInterrupt:
            print("\ninterrupted; current job returns to pending on the next run")
            return 130, _fresh_stats()
        print(f"rendered={stats['rendered']} failed={stats['failed']} "
              f"unrenderable={stats['unrenderable']} "
              f"retried={stats['retried']} skipped_disk={stats['skipped_disk']}")
        return 0, stats
    print(f"rendering with {n_workers} workers (limit {limit})")
    results: dict = {}
    threads = [threading.Thread(target=lambda i=i: results.setdefault(
        i, _worker_loop(i, cfg, renderer, beatmapset_id, state, stop_event, ensure_lock)),
        name=f"render-{i}", daemon=True) for i in range(n_workers)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\ninterrupted; current jobs return to pending on the next run")
        stop_event.set()
        for t in threads:
            t.join()
        return 130, _merge_stats(results)
    stats = _merge_stats(results)
    print(f"rendered={stats['rendered']} failed={stats['failed']} "
          f"unrenderable={stats['unrenderable']} "
          f"retried={stats['retried']} skipped_disk={stats['skipped_disk']}")
    return 0, stats


def _merge_stats(results: dict) -> dict:
    merged = _fresh_stats()
    for local in results.values():
        for k in merged:
            merged[k] += local.get(k, 0)
    return merged


def _with_db(cfg, db_path: Path):
    """Copy of cfg pointing at another database (frozen dataclass)."""
    import dataclasses

    return dataclasses.replace(cfg, database_path=Path(db_path))


def _cmd_render(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.database_path
    limit = args.limit if args.limit is not None else cfg.render_limit_default
    rc, _ = run_render(_with_db(cfg, db_path), limit, args.beatmapset_id,
                       args.workers if args.workers is not None else cfg.render_workers)
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


def _render_one(conn, cfg, renderer: DanserRenderer, job: dict, override_set_id,
                stats: dict, ensure_lock: threading.Lock | None = None) -> None:
    jid = job["id"]
    tag = f"job-{jid}"
    if job["attempts"] > cfg.render_max_attempts:
        database.mark_failed(conn, jid, f"gave up after {job['attempts']} attempts: {job.get('error')}")
        stats["failed"] += 1
        print(f"[{tag}] gave up (attempts exceeded)")
        return

    src = cfg.replays_dir / job["path"]
    if not src.exists():
        # Transient class (NAS unmount, SMB blip): back to pending, attempts
        # cap still bounds a truly lost file. A terminal mark here would
        # permanently kill every claimed row on one storage hiccup.
        database.requeue_one(conn, jid, f"transient: source missing: {src}")
        stats["retried"] += 1
        print(f"[{tag}] source missing, requeued: {job['path']}")
        return

    scratch = cfg.working_dir / f"{tag}.osr"
    try:
        shutil.copyfile(src, scratch)
    except OSError as exc:
        database.requeue_one(conn, jid, f"transient: scratch copy failed: {exc}")
        stats["retried"] += 1
        print(f"[{tag}] scratch copy failed, requeued: {exc}")
        return

    bhash = job.get("beatmap_hash") or read_beatmap_hash(scratch)
    if bhash and not job.get("beatmap_hash"):
        database.set_beatmap(conn, jid, bhash, job.get("beatmapset_id"))

    set_id = job.get("beatmapset_id")
    # Serialized across workers: concurrent downloads of one set corrupt the
    # .part file, and concurrent danser runs contend on its internal DB.
    # Renders (the minutes-long part) stay parallel; only this ensure phase
    # (seconds) is serialized.
    with ensure_lock if ensure_lock is not None else contextlib.nullcontext():
        try:
            set_id, _osz = beatmaps.ensure_beatmap(
                cfg.beatmap_mirror, bhash, cfg.songs_dir, override_set_id=override_set_id,
                osu_client_id=cfg.osu_client_id, osu_client_secret=cfg.osu_client_secret,
                backend=cfg.beatmap_backend,
                fallback_mirror=cfg.fallback_mirror, fallback_backend=cfg.fallback_backend,
                cache_dir=cfg.beatmaps_cache,
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
        if "beatmap not found" in cause and job.get("attempts", 99) <= 1:
            # First miss is expected whenever a parallel worker's import raced
            # (now that danser.db is shared and persistent): retry once, park
            # only on repeat. Attempts cap still bounds true orphans.
            database.requeue_one(conn, jid, f"transient: {cause}")
            stats["retried"] += 1
            print(f"[{tag}] beatmap not found on first try, requeued: {cause}")
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
        database.requeue_one(conn, jid, f"transient: collect output failed: {exc}")
        stats["retried"] += 1
        print(f"[{tag}] collect failed, requeued: {exc}")
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
              f"unrenderable={summary.get('unrenderable', 0)} "
              f"excluded={summary.get('excluded', 0)} ({pct:.0f}% rendered)")
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
                                        header_h=cfg.header_height,
                                        quant=cfg.segment_quant)
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
        database.mark_composited(conn, [c.id for c in kept], today)
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

    rc, rstats = run_render(cfg, args.limit if args.limit is not None else cfg.render_limit_default,
                            workers=args.workers if args.workers is not None else cfg.render_workers)
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
        if crc != 0:
            return crc
    else:
        print(summary + f" composed={composed}")

    if not args.upload:
        return 0
    # Publish backlog: latest daily per platform missing a success row.
    results = {}
    conn = database.connect(cfg.database_path)
    try:
        for platform in ("youtube", "instagram"):
            if platform == "youtube" and not (
                    cfg.youtube_client_id and cfg.youtube_client_secret):
                results[platform] = "unconfigured"
                continue
            if platform == "instagram" and not (
                    cfg.instagram_user_id and cfg.instagram_token):
                results[platform] = "unconfigured"
                continue
            day = database.pending_upload_day(conn, platform)
            if day is None:
                results[platform] = "nothing"
                continue
            ns = argparse.Namespace(db=str(cfg.database_path), day=day,
                                    platform=platform, force=False)
            results[platform] = "ok" if _cmd_upload(ns, cfg) == 0 else "failed"
    finally:
        conn.close()
    print(summary + f" composed={composed} uploaded=" +
          ",".join(f"{p}:{s}" for p, s in results.items()))
    return 0 if all(s != "failed" for s in results.values()) else 1


def _cmd_exclude(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.database_path
    database.init_db(db_path)
    conn = database.connect(db_path)
    try:
        rows = database.exclude_before(conn, args.before, dry_run=args.dry_run)
    finally:
        conn.close()
    verb = "would exclude" if args.dry_run else "excluded"
    print(f"{verb} {len(rows)} replays with day < {args.before}")
    for r in rows[:10]:
        print(f"  {r['day']} {r['path']}")
    if len(rows) > 10:
        print(f"  ... and {len(rows) - 10} more")
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
    if args.manual:
        uploader.run_auth_manual(cfg.youtube_client_id, cfg.youtube_client_secret,
                                 cfg.youtube_token_path, port=args.port)
        return 0
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
    if cfg.prune_after_upload:
        try:
            n, freed = prune_day_renders(conn, args.day)
            print(f"pruned {n} render files (~{freed / 1e6:.0f}MB freed)")
        except Exception as exc:  # noqa: BLE001 - leftovers are harmless, next prune gets them
            log.warning("post-upload prune failed (retry with `prune`): %s", exc)
            print(f"prune skipped ({exc}); run `prune` later")
    return 0


def _delete_render_files(clips: list[dict], dry_run: bool) -> tuple[int, int]:
    """Delete render_path files (missing_ok, warn-and-continue). Returns (files, bytes)."""
    n, freed = 0, 0
    for clip in clips:
        p = Path(clip["render_path"]) if clip.get("render_path") else None
        if p is None:
            continue
        try:
            if p.exists():
                freed += p.stat().st_size
                if not dry_run:
                    p.unlink()
            n += 1
        except OSError as exc:
            log.warning("could not delete %s: %s", p, exc)
    return n, freed


def prune_day_renders(conn, day: str) -> tuple[int, int]:
    """Delete render files mapped to an uploaded day. Returns (files, bytes)."""
    return _delete_render_files(database.clips_of_day(conn, day), dry_run=False)


def _cmd_prune(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.database_path
    database.init_db(db_path)
    conn = database.connect(db_path)
    try:
        if args.backfill:
            top = database.max_uploaded_day(conn)
            if top is None:
                print("backfill refused: no YouTube-uploaded day yet")
                return 2
            targets = [c for c in database.composited_clips(conn) if (c["day"] or "") <= top]
            n, freed = _delete_render_files(targets, args.dry_run)
            print(f"backfill {'would prune' if args.dry_run else 'pruned'} {n} files "
                  f"(~{freed / 1e6:.0f}MB) at/below uploaded day {top}")
            return 0
        day = args.day
        if day is None:
            print("specify --day or --backfill", file=sys.stderr)
            return 2
        up = database.get_upload(conn, day, "youtube")
        if (not up or up["status"] != "uploaded") and not args.force:
            print(f"prune refused: day {day} has no YouTube upload (use --force)", file=sys.stderr)
            return 2
        if args.dry_run:
            n, freed = _delete_render_files(database.clips_of_day(conn, day), dry_run=True)
            print(f"would prune {n} files (~{freed / 1e6:.0f}MB) for day {day}")
            return 0
        n, freed = prune_day_renders(conn, day)
        print(f"pruned {n} files (~{freed / 1e6:.0f}MB) for day {day}")
    finally:
        conn.close()
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
    if args.command == "prune":
        return _cmd_prune(args, cfg)
    if args.command == "exclude":
        return _cmd_exclude(args, cfg)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
