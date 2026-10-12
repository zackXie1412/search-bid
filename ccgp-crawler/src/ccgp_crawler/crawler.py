from __future__ import annotations

import json
import random
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .fetcher import FetchError, SoftBlockError, fetch_page
from .local_store import (
    active_cooldown,
    checkpoint_page,
    initialize_database,
    record_soft_block,
    update_checkpoint,
    utc_now_iso,
    write_page_run,
    save_notices,
)
from .normalize import list_url, validate_category
from .parser import parse_list_page_result


class CrawlStopped(RuntimeError):
    """The requested crawl stopped safely; the checkpoint stays resumable."""


@dataclass(frozen=True, slots=True)
class CrawlOptions:
    category: str
    start_page: int | None = None
    max_pages: int = 3
    delay_min: float = 2.0
    delay_max: float = 5.0
    resume: bool = False
    dry_run: bool = False
    user_agent: str = "ccgp-list-crawler/0.1 (+contact your team)"
    failure_directory: Path | None = None

    def validate(self) -> None:
        validate_category(self.category)
        if self.start_page is not None and self.start_page < 0:
            raise ValueError("start_page must be >= 0")
        if self.max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if self.delay_min < 0 or self.delay_max < self.delay_min:
            raise ValueError("delay range must satisfy 0 <= min <= max")
        if self.resume and self.start_page is not None:
            raise ValueError("--resume and --start-page cannot be used together")


def _emit(payload: dict, output) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=output, flush=True)


def _save_failure_html(directory: Path | None, category: str, page_no: int, html: str) -> None:
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    # Category has already been validated and page_no is numeric, so this
    # cannot escape the intended diagnostics directory.
    (directory / f"{category}_page_{page_no}.html").write_text(html, encoding="utf-8")


def crawl(
    connection: sqlite3.Connection | None,
    options: CrawlOptions,
    *,
    client: httpx.Client | None = None,
    output=None,
    sleep=time.sleep,
) -> int:
    """Fetch at most max_pages list pages; return successful page count.

    A fresh batch defaults to page zero; resume uses the persisted checkpoint.
    Dry-run never writes SQLite and prints one JSON line per extracted notice.
    """

    options.validate()
    output = output or sys.stdout
    if not options.dry_run:
        if connection is None:
            raise ValueError("SQLite connection required unless dry-run")
        initialize_database(connection)
    category = validate_category(options.category)
    page_no = (
        checkpoint_page(connection, category)
        if options.resume and connection is not None
        else options.start_page if options.start_page is not None else 0
    )
    previous_urls: frozenset[str] | None = None
    completed = 0
    domain = "www.ccgp.gov.cn"
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            headers={"User-Agent": options.user_agent},
            follow_redirects=True,
            timeout=httpx.Timeout(connect=20, read=30, write=20, pool=20),
        )

    try:
        while completed < options.max_pages:
            if connection is not None:
                cooldown = active_cooldown(connection, domain, utc_now_iso())
                if cooldown is not None:
                    raise CrawlStopped(f"{domain} cooling down until {cooldown['until_at']}")

            page_url = list_url(category, page_no)
            started_at = utc_now_iso()
            started_clock = time.monotonic()
            http_status: int | None = None
            body: str | None = None
            blocked = False
            try:
                fetched = fetch_page(client, page_url)
                body = fetched.body
                http_status = fetched.status_code
                parsed = parse_list_page_result(page_url, body, category)
                notices = parsed.notices
                urls = frozenset(notice.canonical_url for notice in notices)
                if parsed.skipped_count and body is not None:
                    # Keep the source page for diagnosing rows whose required
                    # URL/title/time could not be normalized. The HTML is a
                    # failure artifact, not an additional extracted field.
                    _save_failure_html(options.failure_directory, category, page_no, body)
                if not notices and parsed.listing_count == 0:
                    raise CrawlStopped(f"no valid listing rows on {page_url}")
                if not notices and parsed.listing_count > parsed.filtered_count:
                    raise CrawlStopped(f"no parseable tender notices on {page_url}")
                if previous_urls is not None and urls and urls == previous_urls:
                    raise CrawlStopped(f"same notice URLs as previous page: {page_url}")

                elapsed_ms = int((time.monotonic() - started_clock) * 1000)
                if options.dry_run:
                    for notice in notices:
                        _emit(notice.as_dict(), output)
                    new_count, duplicate_count = len(notices), 0
                else:
                    assert connection is not None
                    with connection:
                        new_count, duplicate_count = save_notices(connection, notices)
                        write_page_run(
                            connection,
                            category=category,
                            page_no=page_no,
                            page_url=page_url,
                            http_status=http_status,
                            row_count=len(notices),
                            new_count=new_count,
                            duplicate_count=duplicate_count,
                            parse_error_count=parsed.skipped_count,
                            filtered_count=parsed.filtered_count,
                            elapsed_ms=elapsed_ms,
                            started_at=started_at,
                            finished_at=utc_now_iso(),
                        )
                        update_checkpoint(
                            connection,
                            category=category,
                            next_page=page_no + 1,
                            status="running",
                            last_notice_url=notices[-1].canonical_url if notices else None,
                            last_success_at=utc_now_iso(),
                        )
                _emit(
                    {
                        "category": category,
                        "page_no": page_no,
                        "page_url": page_url,
                        "http_status": http_status,
                        "elapsed_ms": elapsed_ms,
                        "row_count": len(notices),
                        "new_count": new_count,
                        "duplicate_count": duplicate_count,
                        "parse_error_count": parsed.skipped_count,
                        "filtered_count": parsed.filtered_count,
                        "blocked": False,
                    },
                    sys.stderr,
                )
                previous_urls = urls
                completed += 1
                page_no += 1
                if completed < options.max_pages:
                    sleep(random.uniform(options.delay_min, options.delay_max))
            except (SoftBlockError, FetchError, CrawlStopped) as exc:
                blocked = isinstance(exc, SoftBlockError)
                if body is not None:
                    _save_failure_html(options.failure_directory, category, page_no, body)
                elapsed_ms = int((time.monotonic() - started_clock) * 1000)
                if connection is not None and not options.dry_run:
                    with connection:
                        cooldown_until = (
                            record_soft_block(connection, domain, str(exc), utc_now_iso())
                            if blocked
                            else None
                        )
                        write_page_run(
                            connection,
                            category=category,
                            page_no=page_no,
                            page_url=page_url,
                            http_status=http_status,
                            blocked=blocked,
                            elapsed_ms=elapsed_ms,
                            error_message=str(exc),
                            started_at=started_at,
                            finished_at=utc_now_iso(),
                        )
                        update_checkpoint(
                            connection,
                            category=category,
                            next_page=page_no,
                            status="blocked" if blocked else "error",
                            error_message=str(exc),
                        )
                _emit(
                    {
                        "category": category,
                        "page_no": page_no,
                        "page_url": page_url,
                        "http_status": http_status,
                        "elapsed_ms": elapsed_ms,
                        "row_count": 0,
                        "new_count": 0,
                        "duplicate_count": 0,
                        "blocked": blocked,
                        "error": str(exc),
                        **({"cooldown_until": cooldown_until} if blocked and not options.dry_run else {}),
                    },
                    sys.stderr,
                )
                raise CrawlStopped(str(exc)) from exc
    except KeyboardInterrupt:
        if connection is not None and not options.dry_run:
            with connection:
                update_checkpoint(
                    connection,
                    category=category,
                    next_page=page_no,
                    status="interrupted",
                    error_message="stopped by user",
                )
        raise
    finally:
        if owns_client:
            client.close()
    return completed
