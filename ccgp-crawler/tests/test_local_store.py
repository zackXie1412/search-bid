import sqlite3

import pytest

from ccgp_crawler.local_store import (
    checkpoint_page,
    get_checkpoint,
    initialize_database,
    open_database,
    save_notices,
    update_checkpoint,
)
from ccgp_crawler.models import Notice


def test_database_context_closes_connection(tmp_path):
    with open_database(tmp_path / "closed.sqlite3") as connection:
        connection.execute("SELECT 1").fetchone()
        with connection:
            connection.execute("SELECT 1").fetchone()
        connection.execute("SELECT 1").fetchone()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_upsert_keeps_one_url_and_checkpoint_roundtrip(tmp_path):
    notice = Notice(
        canonical_url="https://www.ccgp.gov.cn/cggg/zygg/a.htm",
        title="初始标题",
        published_at="2026-09-18T10:00:00+08:00",
        region_raw="湖北",
        region_code="420000",
        category="zygg",
        source_list_url="https://www.ccgp.gov.cn/cggg/zygg/index.htm",
    )
    with open_database(tmp_path / "crawler.sqlite3") as db:
        initialize_database(db)
        with db:
            assert save_notices(db, [notice]) == (1, 0)
            update_checkpoint(db, category="zygg", next_page=1, status="running")
        with db:
            assert save_notices(db, [notice]) == (0, 1)
        assert db.execute("SELECT count(*) FROM pending_notices").fetchone()[0] == 1
        assert db.execute("SELECT region_code FROM pending_notices").fetchone()[0] == "420000"
        assert checkpoint_page(db, "zygg") == 1
        assert get_checkpoint(db, "zygg")["status"] == "running"

    with open_database(tmp_path / "crawler.sqlite3") as db:
        assert checkpoint_page(db, "zygg") == 1


def test_existing_database_gets_region_columns_without_losing_rows(tmp_path):
    db_path = tmp_path / "old.sqlite3"
    with open_database(db_path) as db:
        db.execute("""CREATE TABLE pending_notices (
            canonical_url TEXT PRIMARY KEY, title TEXT NOT NULL,
            published_at TEXT NOT NULL, category TEXT NOT NULL,
            source_list_url TEXT NOT NULL, sync_status TEXT DEFAULT 'pending',
            retry_count INTEGER DEFAULT 0, last_error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, synced_at TEXT
        )""")
        db.execute("""INSERT INTO pending_notices
            (canonical_url,title,published_at,category,source_list_url,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?)""", (
                "https://example.com/a.htm", "old", "2026-09-18T10:00:00+08:00", "zygg",
                "https://www.ccgp.gov.cn/cggg/zygg/index.htm", "now", "now"
            ))
        db.commit()
        initialize_database(db)
        assert db.execute("SELECT title,region_raw,region_code FROM pending_notices").fetchone()[:] == (
            "old", None, None
        )
        initialize_database(db)
