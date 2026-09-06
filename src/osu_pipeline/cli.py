"""CLI: osu-pipeline discover / render / status."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import beatmaps, database
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
        print(f"  discovered: {counts.get('discovered', 0)}")
        print(f"  pending:    {counts.get('pending', 0)}")
        print(f"  rendering:  {counts.get('rendering', 0)}")
        print(f"  rendered:   {counts.get('rendered', 0)}")
        print(f"  failed:     {counts.get('failed', 0)}")
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


def _cmd_render(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.database_path
    limit = args.limit if args.limit is not None else cfg.render_limit_default
    renderer = DanserRenderer(
        danser_home=cfg.danser_home,
        settings=cfg.danser_settings,
        timeout_seconds=cfg.render_timeout_seconds,
        extra_args=cfg.danser_extra_args,
    )
    if not Path(renderer.cmd_prefix[0]).exists():
        print(f"danser not found: {renderer.cmd_prefix[0]} (PIPELINE_DANSER_HOME={cfg.danser_home})",
              file=sys.stderr)
        return 2
    cfg.working_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    cfg.rendered_dir.mkdir(parents=True, exist_ok=True)

    database.init_db(db_path)
    conn = database.connect(db_path)
    stats = {"rendered": 0, "failed": 0, "skipped_disk": 0}
    try:
        stale = database.reset_stale_rendering(conn)
        if stale:
            print(f"requeued {stale} stale rendering job(s)")
        for _ in range(limit):
            free = _free_gb(cfg.rendered_dir)
            if free < cfg.disk_min_free_gb:
                print(f"disk guard: {free:.1f}GB free < {cfg.disk_min_free_gb}GB minimum; stopping run")
                stats["skipped_disk"] += 1
                break
            job = database.claim_pending(conn)
            if job is None:
                break
            _render_one(conn, cfg, renderer, job, override_set_id=args.beatmapset_id, stats=stats)
    finally:
        conn.close()
    print(f"rendered={stats['rendered']} failed={stats['failed']} skipped_disk={stats['skipped_disk']}")
    return 0


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

    try:
        set_id, _osz = beatmaps.ensure_beatmap(
            cfg.beatmap_mirror, bhash, cfg.songs_dir, override_set_id=override_set_id,
            osu_client_id=cfg.osu_client_id, osu_client_secret=cfg.osu_client_secret,
            backend=cfg.beatmap_backend,
        )
        database.set_beatmap(conn, jid, bhash, set_id)
    except beatmaps.BeatmapError as exc:
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
        database.mark_failed(conn, jid, (result.error or "render failed")[-500:])
        stats["failed"] += 1
        print(f"[{tag}] FAILED: {result.error}")
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
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
