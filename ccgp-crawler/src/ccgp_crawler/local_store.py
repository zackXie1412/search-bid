from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .models import Notice


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS pending_notices (
    canonical_url TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    region_raw TEXT,
    region_code TEXT,
    category TEXT NOT NULL,
    source_list_url TEXT NOT NULL,
    sync_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (sync_status IN ('pending', 'syncing', 'synced', 'failed')),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    synced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_notices_sync
    ON pending_notices(sync_status, updated_at);
CREATE INDEX IF NOT EXISTS idx_pending_notices_published
    ON pending_notices(published_at);

CREATE TABLE IF NOT EXISTS sync_batches (
    batch_key TEXT PRIMARY KEY,
    payload_sha256 TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK (item_count > 0),
    status TEXT NOT NULL CHECK (status IN ('syncing', 'synced', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    synced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_batches_status_updated
    ON sync_batches(status, updated_at);

CREATE TABLE IF NOT EXISTS sync_batch_items (
    batch_key TEXT NOT NULL REFERENCES sync_batches(batch_key) ON DELETE CASCADE,
    canonical_url TEXT NOT NULL REFERENCES pending_notices(canonical_url) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (batch_key, canonical_url),
    UNIQUE (batch_key, position)
);

CREATE TABLE IF NOT EXISTS crawl_checkpoint (
    category TEXT PRIMARY KEY,
    next_page INTEGER NOT NULL DEFAULT 0 CHECK (next_page >= 0),
    last_success_at TEXT,
    last_notice_url TEXT,
    status TEXT NOT NULL DEFAULT 'idle',
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS domain_cooldown (
    domain TEXT PRIMARY KEY,
    blocked_count INTEGER NOT NULL DEFAULT 0,
    until_at TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_page_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    page_no INTEGER NOT NULL CHECK (page_no >= 0),
    page_url TEXT NOT NULL,
    http_status INTEGER,
    row_count INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    parse_error_count INTEGER NOT NULL DEFAULT 0,
    filtered_count INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0 CHECK (blocked IN (0, 1)),
    elapsed_ms INTEGER,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_crawl_page_runs_category_page
    ON crawl_page_runs(category, page_no, id);
"""


def utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ClosingConnection(sqlite3.Connection):
    """Commit or roll back like sqlite3.Connection, then close the handle."""

    def __enter__(self):
        super().__enter__()
        self._context_depth = getattr(self, "_context_depth", 0) + 1
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self._context_depth -= 1
            if self._context_depth == 0:
                self.close()


def open_database(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(pending_notices)")}
    # Existing buffers created before region extraction must remain usable.
    for column in ("region_raw", "region_code"):
        if column not in columns:
            connection.execute(f"ALTER TABLE pending_notices ADD COLUMN {column} TEXT")
    _migrate_sync_status(connection)
    page_run_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(crawl_page_runs)")
    }
    if "filtered_count" not in page_run_columns:
        connection.execute(
            "ALTER TABLE crawl_page_runs ADD COLUMN filtered_count INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_notices_region_published "
        "ON pending_notices(region_code, published_at)"
    )
    connection.commit()


def _migrate_sync_status(connection: sqlite3.Connection) -> None:
    """Add the syncing state to old buffers without losing rows or FKs."""

    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pending_notices'"
    ).fetchone()
    if row is None or "'syncing'" in (row[0] or ""):
        return

    # SQLite cannot alter a CHECK constraint. Foreign keys must be disabled
    # outside a transaction while the referenced table is replaced in place.
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE pending_notices_sync_migration (
                canonical_url TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                published_at TEXT NOT NULL,
                region_raw TEXT,
                region_code TEXT,
                category TEXT NOT NULL,
                source_list_url TEXT NOT NULL,
                sync_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (sync_status IN ('pending', 'syncing', 'synced', 'failed')),
                retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                synced_at TEXT
            );
            INSERT INTO pending_notices_sync_migration(
                canonical_url, title, published_at, region_raw, region_code,
                category, source_list_url, sync_status, retry_count, last_error,
                created_at, updated_at, synced_at
            )
            SELECT
                canonical_url, title, published_at, region_raw, region_code,
                category, source_list_url, sync_status, retry_count, last_error,
                created_at, updated_at, synced_at
            FROM pending_notices;
            DROP TABLE pending_notices;
            ALTER TABLE pending_notices_sync_migration RENAME TO pending_notices;
            CREATE INDEX idx_pending_notices_sync
                ON pending_notices(sync_status, updated_at);
            CREATE INDEX idx_pending_notices_published
                ON pending_notices(published_at);
            COMMIT;
            """
        )
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(f"foreign key violations after sync migration: {violations}")


def get_checkpoint(connection: sqlite3.Connection, category: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM crawl_checkpoint WHERE category = ?", (category,)
    ).fetchone()


def checkpoint_page(connection: sqlite3.Connection, category: str) -> int:
    row = get_checkpoint(connection, category)
    return int(row["next_page"]) if row else 0


def active_cooldown(connection: sqlite3.Connection, domain: str, now: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM domain_cooldown WHERE domain = ? AND until_at > ?",
        (domain, now),
    ).fetchone()


def record_soft_block(connection: sqlite3.Connection, domain: str, reason: str, now: str) -> str:
    from datetime import datetime, timedelta, timezone

    prior = connection.execute(
        "SELECT blocked_count FROM domain_cooldown WHERE domain = ?", (domain,)
    ).fetchone()
    count = (prior[0] if prior else 0) + 1
    # First block: 30 minutes; second: 2 hours; further blocks: until the
    # next local day. This only prevents requests, never tries to evade a ban.
    instant = datetime.fromisoformat(now)
    if count == 1:
        until = instant + timedelta(minutes=30)
    elif count == 2:
        until = instant + timedelta(hours=2)
    else:
        from .normalize import SHANGHAI_TZ

        local = instant.astimezone(SHANGHAI_TZ)
        until = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        until = until.astimezone(timezone.utc)
    until_at = until.isoformat(timespec="seconds")
    connection.execute(
        """
        INSERT INTO domain_cooldown(domain, blocked_count, until_at, reason)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(domain) DO UPDATE SET
            blocked_count = excluded.blocked_count,
            until_at = excluded.until_at,
            reason = excluded.reason
        """,
        (domain, count, until_at, reason),
    )
    return until_at


def save_notices(
    connection: sqlite3.Connection,
    notices: Iterable[Notice],
    *,
    now: str | None = None,
) -> tuple[int, int]:
    """Upsert notices and return (new_count, duplicate_count).

    The caller controls the surrounding transaction so notice writes, page
    audit data and checkpoint movement can commit atomically.
    """

    notices = list(notices)
    if not notices:
        return 0, 0
    timestamp = now or utc_now_iso()
    urls = [notice.canonical_url for notice in notices]
    placeholders = ",".join("?" for _ in urls)
    existing = {
        row[0]
        for row in connection.execute(
            f"SELECT canonical_url FROM pending_notices WHERE canonical_url IN ({placeholders})",
            urls,
        )
    }

    connection.executemany(
        """
        INSERT INTO pending_notices(
            canonical_url, title, published_at, region_raw, region_code, category, source_list_url,
            sync_status, retry_count, last_error, created_at, updated_at, synced_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?, NULL)
        ON CONFLICT(canonical_url) DO UPDATE SET
            title = excluded.title,
            published_at = excluded.published_at,
            region_raw = excluded.region_raw,
            region_code = excluded.region_code,
            category = excluded.category,
            source_list_url = excluded.source_list_url,
            sync_status = 'pending',
            last_error = NULL,
            updated_at = excluded.updated_at,
            synced_at = NULL
        """,
        [
            (
                notice.canonical_url,
                notice.title,
                notice.published_at,
                notice.region_raw,
                notice.region_code,
                notice.category,
                notice.source_list_url,
                timestamp,
                timestamp,
            )
            for notice in notices
        ],
    )
    return len(notices) - len(existing), len(existing)


def write_page_run(
    connection: sqlite3.Connection,
    *,
    category: str,
    page_no: int,
    page_url: str,
    http_status: int | None,
    row_count: int = 0,
    new_count: int = 0,
    duplicate_count: int = 0,
    parse_error_count: int = 0,
    filtered_count: int = 0,
    blocked: bool = False,
    elapsed_ms: int | None = None,
    error_message: str | None = None,
    started_at: str,
    finished_at: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO crawl_page_runs(
            category, page_no, page_url, http_status, row_count, new_count,
            duplicate_count, parse_error_count, blocked, elapsed_ms,
            filtered_count, error_message, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            category,
            page_no,
            page_url,
            http_status,
            row_count,
            new_count,
            duplicate_count,
            parse_error_count,
            int(blocked),
            elapsed_ms,
            filtered_count,
            error_message,
            started_at,
            finished_at,
        ),
    )


def update_checkpoint(
    connection: sqlite3.Connection,
    *,
    category: str,
    next_page: int,
    status: str,
    last_notice_url: str | None = None,
    last_success_at: str | None = None,
    error_message: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO crawl_checkpoint(
            category, next_page, last_success_at, last_notice_url, status, error_message
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(category) DO UPDATE SET
            next_page = excluded.next_page,
            last_success_at = COALESCE(excluded.last_success_at, crawl_checkpoint.last_success_at),
            last_notice_url = COALESCE(excluded.last_notice_url, crawl_checkpoint.last_notice_url),
            status = excluded.status,
            error_message = excluded.error_message
        """,
        (
            category,
            next_page,
            last_success_at,
            last_notice_url,
            status,
            error_message,
        ),
    )


def sync_statistics(connection: sqlite3.Connection) -> dict[str, object]:
    """Return upload counters for the workbench without exposing table details."""

    counts = {
        row["sync_status"]: int(row["count"])
        for row in connection.execute(
            "SELECT sync_status, count(*) AS count FROM pending_notices GROUP BY sync_status"
        ).fetchall()
    }
    batch_counts = {
        row["status"]: int(row["count"])
        for row in connection.execute(
            "SELECT status, count(*) AS count FROM sync_batches GROUP BY status"
        ).fetchall()
    }
    last_success = connection.execute(
        "SELECT max(synced_at) FROM sync_batches WHERE status='synced'"
    ).fetchone()[0]
    last_failure = connection.execute(
        """
        SELECT last_error
        FROM sync_batches
        WHERE status='failed' AND last_error IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 1
        """
    ).fetchone()
    return {
        "pending": counts.get("pending", 0),
        "syncing": counts.get("syncing", 0),
        "synced": counts.get("synced", 0),
        "failed": counts.get("failed", 0),
        "total": sum(counts.values()),
        "synced_batches": batch_counts.get("synced", 0),
        "failed_batches": batch_counts.get("failed", 0),
        "last_success_at": last_success,
        "last_error": last_failure[0] if last_failure else None,
    }


def retry_failed_syncs(connection: sqlite3.Connection) -> int:
    """Move terminal failed notices back into the explicit retry queue."""

    timestamp = utc_now_iso()
    with connection:
        result = connection.execute(
            """
            UPDATE pending_notices
            SET sync_status='pending', last_error=NULL, updated_at=?
            WHERE sync_status='failed'
            """,
            (timestamp,),
        )
    return result.rowcount
