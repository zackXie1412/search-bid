-- Auditable copy of the local SQLite schema used by
-- ccgp_crawler.local_store.initialize_database.
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
CREATE INDEX IF NOT EXISTS idx_pending_notices_region_published
    ON pending_notices(region_code, published_at);

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
