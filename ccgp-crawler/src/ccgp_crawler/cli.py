from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .crawler import CrawlOptions, CrawlStopped, crawl
from .local_store import initialize_database, open_database
from .sync_client import SyncError, sync_pending


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CCGP static category list crawler")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init = subcommands.add_parser("init-db", help="initialize the local SQLite buffer")
    init.add_argument("--db", type=Path, default=Path("runtime/crawler.sqlite3"))

    run = subcommands.add_parser("crawl", help="crawl a bounded page batch")
    run.add_argument("--db", type=Path, default=Path("runtime/crawler.sqlite3"))
    run.add_argument("--category", required=True)
    start = run.add_mutually_exclusive_group()
    start.add_argument("--start-page", type=int)
    start.add_argument("--resume", action="store_true")
    run.add_argument("--max-pages", type=int, default=3)
    run.add_argument("--delay-min", type=float, default=2.0)
    run.add_argument("--delay-max", type=float, default=5.0)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument(
        "--failure-directory", type=Path, default=Path("runtime/html_failures")
    )
    run.add_argument(
        "--user-agent", default="ccgp-list-crawler/0.1 (+contact your team)"
    )

    sync = subcommands.add_parser("sync", help="sync buffered notices to the ingest API")
    sync.add_argument("--db", type=Path, default=Path("runtime/ccgp.sqlite3"))
    sync.add_argument("--batch-size", type=int, default=200)
    sync.add_argument("--max-items", type=int)
    sync.add_argument("--max-batches", type=int)
    sync.add_argument("--endpoint")
    sync.add_argument("--api-key")
    sync.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init-db":
        with open_database(args.db) as connection:
            initialize_database(connection)
        print(f"initialized {args.db}")
        return 0

    if args.command == "sync":
        try:
            summary = sync_pending(
                args.db,
                endpoint=args.endpoint,
                api_key=args.api_key,
                batch_size=args.batch_size,
                max_items=args.max_items,
                max_batches=args.max_batches,
                dry_run=args.dry_run,
                output=sys.stdout,
            )
        except (ValueError, SyncError) as exc:
            print(f"sync stopped: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(summary.as_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 0

    options = CrawlOptions(
        category=args.category,
        start_page=args.start_page,
        max_pages=args.max_pages,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        resume=args.resume,
        dry_run=args.dry_run,
        user_agent=args.user_agent,
        failure_directory=args.failure_directory,
    )
    try:
        options.validate()
        if options.dry_run:
            pages = crawl(None, options)
        else:
            with open_database(args.db) as connection:
                pages = crawl(connection, options)
    except (ValueError, CrawlStopped) as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; completed pages remain committed", file=sys.stderr)
        return 130
    print(f"completed {pages} page(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
