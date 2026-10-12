PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS crawl_jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL CHECK (job_type IN ('manual', 'scheduled')),
    schedule_key TEXT UNIQUE,
    scheduled_for TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'stopping', 'stopped', 'blocked', 'failed', 'completed')
    ),
    current_date TEXT,
    current_page INTEGER,
    current_total_pages INTEGER,
    current_title TEXT,
    dates_total INTEGER NOT NULL DEFAULT 0,
    dates_completed INTEGER NOT NULL DEFAULT 0,
    pages_total INTEGER NOT NULL DEFAULT 0,
    pages_completed INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    parse_error_count INTEGER NOT NULL DEFAULT 0,
    filtered_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_crawl_jobs_status_created
    ON crawl_jobs(status, created_at);

CREATE TABLE IF NOT EXISTS crawl_slices (
    job_id TEXT NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    slice_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'running', 'stopped', 'blocked', 'failed', 'completed')
    ),
    total_pages INTEGER,
    next_page INTEGER NOT NULL DEFAULT 1 CHECK (next_page >= 1),
    pages_completed INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, slice_date)
);

CREATE INDEX IF NOT EXISTS idx_crawl_slices_job_status_date
    ON crawl_slices(job_id, status, slice_date);

CREATE TABLE IF NOT EXISTS crawl_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_crawl_events_job_id
    ON crawl_events(job_id, id);

CREATE TABLE IF NOT EXISTS notice_job_links (
    job_id TEXT NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    canonical_url TEXT NOT NULL REFERENCES pending_notices(canonical_url) ON DELETE CASCADE,
    is_new INTEGER NOT NULL CHECK (is_new IN (0, 1)),
    seen_at TEXT NOT NULL,
    PRIMARY KEY (job_id, canonical_url)
);

CREATE INDEX IF NOT EXISTS idx_notice_job_links_job_seen
    ON notice_job_links(job_id, seen_at DESC);

CREATE TABLE IF NOT EXISTS schedule_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    run_time TEXT NOT NULL DEFAULT '02:00',
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    last_success_date TEXT,
    updated_at TEXT NOT NULL
);

