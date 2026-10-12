from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from ccgp_crawler.api import create_app
from ccgp_crawler.local_store import initialize_database, open_database, save_notices
from ccgp_crawler.models import Notice
from ccgp_crawler.sync_client import SyncSummary, prepare_batch
from ccgp_crawler.sync_service import SyncManager


def notice(index: int) -> Notice:
    return Notice(
        canonical_url=f"https://www.ccgp.gov.cn/cggg/dfgg/gkzb/202609/item-{index}.htm",
        title=f"测试公告 {index}",
        published_at="2026-09-20T10:00:00+08:00",
        region_raw="湖北",
        region_code="420000",
        category="dfgg",
        source_list_url="https://search.ccgp.gov.cn/bxsearch?searchtype=1",
    )


def app_with_notices(tmp_path, monkeypatch, count: int = 2):
    database = tmp_path / "sync-api.sqlite3"
    with open_database(database) as connection:
        initialize_database(connection)
        with connection:
            save_notices(connection, [notice(index) for index in range(count)])
    monkeypatch.setattr(
        SyncManager,
        "_cloud_status",
        lambda self: {"state": "connected", "message": "connected"},
    )
    return create_app(
        database_path=database,
        runtime_dir=tmp_path / "runtime",
        start_background_services=False,
    )


def test_sync_status_returns_local_counts(tmp_path, monkeypatch):
    app = app_with_notices(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.get("/api/sync/status")
    assert response.status_code == 200
    assert response.json()["counts"] == {
        "pending": 2,
        "syncing": 0,
        "synced": 0,
        "failed": 0,
        "total": 2,
        "synced_batches": 0,
        "failed_batches": 0,
        "last_success_at": None,
        "last_error": None,
    }


def test_unavailable_cloud_does_not_change_local_queue(tmp_path, monkeypatch):
    app = app_with_notices(tmp_path, monkeypatch)
    monkeypatch.setattr(
        SyncManager,
        "_cloud_status",
        lambda self: {"state": "unavailable", "message": "tunnel offline"},
    )
    with TestClient(app) as client:
        assert client.get("/api/sync/status").json()["connection"]["state"] == "unavailable"
        response = client.post("/api/sync/start")
        assert response.status_code == 503
        assert client.get("/api/sync/status").json()["counts"]["pending"] == 2


def test_only_one_background_sync_can_start(tmp_path, monkeypatch):
    app = app_with_notices(tmp_path, monkeypatch)
    started = threading.Event()
    release = threading.Event()

    def blocking_sync(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return SyncSummary()

    monkeypatch.setattr("ccgp_crawler.sync_service.sync_pending", blocking_sync)
    with TestClient(app) as client:
        first = client.post("/api/sync/start")
        assert first.status_code == 200
        assert started.wait(1)
        assert client.post("/api/sync/start").status_code == 409
        release.set()
        for _ in range(50):
            if client.get("/api/sync/status").json()["run"]["status"] == "completed":
                break
            time.sleep(0.01)
        assert client.get("/api/sync/status").json()["run"]["status"] == "completed"


def test_retry_failed_only_resets_failed_not_synced(tmp_path, monkeypatch):
    app = app_with_notices(tmp_path, monkeypatch)
    with open_database(app.state.sync_manager.database) as connection:
        with connection:
            connection.execute(
                "UPDATE pending_notices SET sync_status='failed' WHERE canonical_url LIKE '%item-0.htm'"
            )
            connection.execute(
                "UPDATE pending_notices SET sync_status='synced' WHERE canonical_url LIKE '%item-1.htm'"
            )
    with TestClient(app) as client:
        response = client.post("/api/sync/retry-failed")
    assert response.status_code == 200
    assert response.json()["reset"] == 1
    assert response.json()["status"]["counts"]["pending"] == 1
    assert response.json()["status"]["counts"]["synced"] == 1


def test_app_startup_recovers_stale_syncing_rows(tmp_path, monkeypatch):
    database = tmp_path / "stale.sqlite3"
    with open_database(database) as connection:
        initialize_database(connection)
        with connection:
            save_notices(connection, [notice(1)])
        assert prepare_batch(connection, 1) is not None
        stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        with connection:
            connection.execute("UPDATE pending_notices SET updated_at=?", (stale,))
            connection.execute("UPDATE sync_batches SET updated_at=?", (stale,))

    monkeypatch.setattr(
        SyncManager,
        "_cloud_status",
        lambda self: {"state": "connected", "message": "connected"},
    )
    app = create_app(
        database_path=database,
        runtime_dir=tmp_path / "runtime",
        start_background_services=False,
    )
    with TestClient(app) as client:
        counts = client.get("/api/sync/status").json()["counts"]
    assert counts["pending"] == 1
    assert counts["syncing"] == 0
