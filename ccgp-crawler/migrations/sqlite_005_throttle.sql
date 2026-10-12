PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS crawl_throttle_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    domain TEXT NOT NULL,
    day_key TEXT NOT NULL,
    day_request_count INTEGER NOT NULL DEFAULT 0 CHECK (day_request_count >= 0),
    batch_request_count INTEGER NOT NULL DEFAULT 0 CHECK (batch_request_count >= 0),
    next_request_at TEXT,
    next_job_at TEXT,
    active_job_id TEXT,
    resume_required INTEGER NOT NULL DEFAULT 0 CHECK (resume_required IN (0, 1)),
    confirmed_job_id TEXT,
    pause_resume_job_id TEXT,
    last_job_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_crawl_throttle_next_request
    ON crawl_throttle_state(next_request_at);
