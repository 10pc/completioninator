"""CLI: osu-pipeline discover / status (Milestone 1)."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import database
from .config import load_config
from .discovery import scan_replays


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="osu-pipeline", description="osu! completionist pipeline (M1: discovery)")
    p.add_argument("--config", default=None, help="Path to config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="Scan replay dir into SQLite")
    d.add_argument("--scan-root", default=None, help="Override replay directory")
    d.add_argument("--db", default=None, help="Override database path")

    s = sub.add_parser("status", help="Show discovery state")
    s.add_argument("--db", default=None, help="Override database path")
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
        print(f"  total:   {day.get('total', 0)}")
        print(f"  pending: {day.get('pending', 0)}")
        print("\nRecent days:")
        for row in database.get_distinct_days(conn):
            print(f"  {row['day']}: {row['total']}")
    finally:
        conn.close()
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
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
