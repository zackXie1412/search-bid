from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ccgp_crawler.local_store import initialize_database, open_database, save_notices
from ccgp_crawler.models import Notice
from ccgp_crawler.sync_client import (
    SyncError,
    make_batch_key,
    payload_sha256,
    prepare_batch,
    recover_stale_syncs,
    sync_pending,
)


def notices(count: int) -> list[Notice]:
    return [
        Notice(
            canonical_url=f"https://www.ccgp.gov.cn/cggg/dfgg/gkzb/202609/item-{index}.htm",
            title=f"测试公告 {index}",
            published_at="2026-09-20T10:00:00+08:00",
            region_raw="湖北",
            region_code="420000",
            category="dfgg",
            source_list_url="https://search.ccgp.gov.cn/bxsearch?searchtype=1",
        )
        for index in range(count)
    ]


def make_database(tmp_path, count: int = 3):
    path = tmp_path / "sync.sqlite3"
    with open_database(path) as connection:
        initialize_database(connection)
        with connection:
            save_notices(connection, notices(count), now="2026-09-20T00:00:00+00:00")
    return path


def test_batch_key_and_payload_hash_are_stable(tmp_path):
    path = make_database(tmp_path)
    with open_database(path) as connection:
        first = prepare_batch(connection, 3, dry_run=True)
        second = prepare_batch(connection, 3, dry_run=True)
    assert first is not None and second is not None
    assert first.batch_key == second.batch_key
    assert first.payload_sha256 == payload_sha256(first.items)
    assert first.batch_key == make_batch_key(first.items, first.payload_sha256)


def test_successful_sync_marks_rows_and_tracks_replay_flag(tmp_path):
    path = make_database(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret"
        body = request.read().decode()
        assert "payload_sha256" in body
        return httpx.Response(
            200,
            json={
                "batch_key": __import__("json").loads(body)["batch_key"],
                "received": 3,
                "inserted": 3,
                "updated": 0,
                "rejected": 0,
                "replayed": False,
            },
        )

    output = io.StringIO()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = sync_pending(
            path,
            endpoint="https://api.example.test",
            api_key="secret",
            batch_size=3,
            max_items=3,
            client=client,
            output=output,
        )
    assert result.as_dict() == {
        "batches": 1,
        "synced": 3,
        "inserted": 3,
        "updated": 0,
        "replayed_batches": 0,
    }
    with open_database(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pending_notices WHERE sync_status='synced'"
        ).fetchone()[0] == 3
        assert connection.execute("SELECT status FROM sync_batches").fetchone()[0] == "synced"
        assert prepare_batch(connection, 3) is None


def test_network_failure_restores_pending_for_safe_retry(tmp_path):
    path = make_database(tmp_path, 1)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SyncError, match="network error"):
            sync_pending(
                path,
                endpoint="https://api.example.test",
                api_key="secret",
                batch_size=1,
                client=client,
                sleep=lambda _: None,
            )
    with open_database(path) as connection:
        row = connection.execute(
            "SELECT sync_status,retry_count,last_error FROM pending_notices"
        ).fetchone()
        assert row["sync_status"] == "pending"
        assert row["retry_count"] == 1
        assert "offline" in row["last_error"]


def test_authentication_failure_stops_without_retrying(tmp_path):
    path = make_database(tmp_path, 1)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(403, json={"detail": "invalid bearer token"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SyncError, match="HTTP 403"):
            sync_pending(
                path,
                endpoint="https://api.example.test",
                api_key="wrong",
                batch_size=1,
                client=client,
                sleep=lambda _: None,
            )
    assert attempts == 1
    with open_database(path) as connection:
        assert connection.execute(
            "SELECT sync_status FROM pending_notices"
        ).fetchone()[0] == "pending"


def test_validation_failure_is_terminal_and_stale_sync_recovers(tmp_path):
    path = make_database(tmp_path, 2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "bad row"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SyncError, match="HTTP 422"):
            sync_pending(
                path,
                endpoint="https://api.example.test",
                api_key="secret",
                batch_size=1,
                client=client,
            )
    with open_database(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pending_notices WHERE sync_status='failed'"
        ).fetchone()[0] == 1
        batch = prepare_batch(connection, 1)
        assert batch is not None
        stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        connection.execute(
            "UPDATE pending_notices SET updated_at=? WHERE sync_status='syncing'", (stale,)
        )
        connection.execute("UPDATE sync_batches SET updated_at=? WHERE status='syncing'", (stale,))
        connection.commit()
        assert recover_stale_syncs(connection) == 1
        assert connection.execute(
            "SELECT count(*) FROM pending_notices WHERE sync_status='pending'"
        ).fetchone()[0] == 1


def test_old_sync_constraint_is_migrated_without_losing_rows(tmp_path):
    path = tmp_path / "old.sqlite3"
    with open_database(path) as connection:
        connection.executescript(
            """
            CREATE TABLE pending_notices (
                canonical_url TEXT PRIMARY KEY, title TEXT NOT NULL,
                published_at TEXT NOT NULL, region_raw TEXT, region_code TEXT,
                category TEXT NOT NULL, source_list_url TEXT NOT NULL,
                sync_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (sync_status IN ('pending','synced','failed')),
                retry_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, synced_at TEXT
            );
            INSERT INTO pending_notices(
                canonical_url,title,published_at,category,source_list_url,created_at,updated_at
            ) VALUES (
                'https://www.ccgp.gov.cn/cggg/dfgg/gkzb/a.htm','old',
                '2026-09-20T10:00:00+08:00','dfgg',
                'https://search.ccgp.gov.cn/bxsearch','now','now'
            );
            """
        )
        initialize_database(connection)
        connection.execute("UPDATE pending_notices SET sync_status='syncing'")
        connection.commit()
        assert connection.execute("SELECT title FROM pending_notices").fetchone()[0] == "old"
