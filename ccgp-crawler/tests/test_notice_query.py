import pytest
from fastapi.testclient import TestClient

from ccgp_crawler.api import create_app
from ccgp_crawler.local_store import save_notices
from ccgp_crawler.models import Notice


@pytest.fixture
def query_app(tmp_path):
    app = create_app(
        database_path=tmp_path / "notices.sqlite3",
        runtime_dir=tmp_path / "runtime",
        start_background_services=False,
    )
    return app


def seed(app, *, count=1, region="370000", published="2026-10-07T10:00:00+08:00", title="设备采购", subtype="gkzb", category="dfgg", status="pending"):
    with app.state.store.connect() as db:
        offset = db.execute("SELECT count(*) FROM pending_notices").fetchone()[0]
        notices = [
            Notice(
                canonical_url=f"https://www.ccgp.gov.cn/cggg/{category}/{subtype}/202610/{offset + i}.htm",
                title=f"{title} {offset + i}",
                published_at=published,
                region_code=region,
                region_raw="山东" if region == "370000" else None,
                category=category,
                source_list_url="https://search.ccgp.gov.cn/bxsearch",
            )
            for i in range(count)
        ]
        save_notices(db, notices)
        for notice in notices:
            db.execute("UPDATE pending_notices SET sync_status=? WHERE canonical_url=?", (status, notice.canonical_url))


def test_default_query_needs_no_key_and_only_returns_open_tenders(query_app):
    seed(query_app, count=4, status="pending")
    seed(query_app, count=3, category="zygg", status="synced")
    seed(query_app, subtype="zbgg", title="中标公告")
    with TestClient(query_app) as client:
        response = client.get("/api/v1/notices")
    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 6
    assert len(data["items"]) == 6
    assert data["total_count"] == 7
    assert data["next_cursor"]
    assert all("/gkzb/" in notice["canonical_url"] for notice in data["items"])
    assert set(data["items"][0]) == {"id", "title", "canonical_url", "published_at", "region_code", "region_raw"}


def test_region_and_inclusive_beijing_date_boundaries(query_app):
    seed(query_app, published="2026-10-06T23:59:59+08:00")
    seed(query_app, published="2026-10-07T00:00:00+08:00")
    seed(query_app, published="2026-10-07T23:59:59+08:00", status="failed")
    seed(query_app, published="2026-10-08T00:00:00+08:00")
    seed(query_app, region="440000")
    with TestClient(query_app) as client:
        data = client.get("/api/v1/notices", params={"region_code": "370000", "start_date": "2026-10-07", "end_date": "2026-10-07"}).json()
        empty = client.get("/api/v1/notices", params={"region_code": "110000"}).json()
    assert [item["published_at"] for item in data["items"]] == ["2026-10-07T23:59:59+08:00", "2026-10-07T00:00:00+08:00"]
    assert data["total_count"] == 2
    assert empty == {"items": [], "next_cursor": None, "limit": 6, "total_count": 0}


def test_keywords_are_combined_and_sql_wildcards_are_literal(query_app):
    seed(query_app, title=r"卫星设备 100%_\预算")
    seed(query_app, title="卫星设备 100元预算")
    seed(query_app, title=r"100%_\预算 普通设备")
    with TestClient(query_app) as client:
        response = client.get("/api/v1/notices", params={"keyword": r"卫星 100%_\预算"})
        injection = client.get("/api/v1/notices", params={"keyword": "' OR 1=1 --"})
    assert len(response.json()["items"]) == 1
    assert response.json()["total_count"] == 1
    assert injection.json()["items"] == []
    assert injection.json()["total_count"] == 0


def test_equal_timestamps_paginate_without_duplicates_and_reads_do_not_write(query_app):
    seed(query_app, count=8)
    with query_app.state.store.connect() as db:
        before = [tuple(row) for row in db.execute("SELECT rowid,* FROM pending_notices ORDER BY rowid")]
    with TestClient(query_app) as client:
        first = client.get("/api/v1/notices", params={"region_code": "370000"}).json()
        second = client.get("/api/v1/notices", params={"region_code": "370000", "cursor": first["next_cursor"]}).json()
    ids = [item["id"] for page in (first, second) for item in page["items"]]
    assert ids == list(range(8, 0, -1))
    assert len(first["items"]) == 6 and len(second["items"]) == 2
    assert first["total_count"] == second["total_count"] == 8
    assert second["next_cursor"] is None
    with query_app.state.store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT rowid,* FROM pending_notices ORDER BY rowid")] == before


def test_cursor_rejects_other_filters_and_malformed_values(query_app):
    seed(query_app, count=7)
    with TestClient(query_app) as client:
        cursor = client.get("/api/v1/notices", params={"region_code": "370000"}).json()["next_cursor"]
        assert client.get("/api/v1/notices", params={"region_code": "110000", "cursor": cursor}).status_code == 400
        for value in ("not-a-cursor", "", "bnVsbA", "W10", "e30"):
            assert client.get("/api/v1/notices", params={"cursor": value}).status_code == 400


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 101}, {"region_code": "37000"},
    {"start_date": "invalid"}, {"start_date": "2026-10-08", "end_date": "2026-10-07"},
    {"keyword": "x" * 101}, {"end_date": "9999-12-31"},
])
def test_invalid_parameters_return_422(query_app, params):
    with TestClient(query_app) as client:
        assert client.get("/api/v1/notices", params=params).status_code == 422


def test_missing_database_returns_503_without_creating_a_new_one(query_app):
    with TestClient(query_app) as client:
        path = query_app.state.store.path
        path.unlink()
        response = client.get("/api/v1/notices")
    assert response.status_code == 503
    assert not path.exists()
    assert str(path) not in response.text
