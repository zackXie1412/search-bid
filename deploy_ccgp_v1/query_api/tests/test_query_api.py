from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
import psycopg

import app.main as query_app
from app.main import (
    QueryParams,
    RateLimiter,
    build_query,
    decode_cursor,
    encode_cursor,
    escape_like,
)


SECRET = "test-secret"
AUTH = {"X-API-Key": "valid-test-key-value"}


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class Cursor:
    def __init__(self, pool):
        self.pool = pool

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, values):
        self.pool.last_query = (sql, values)

    def fetchall(self):
        return self.pool.rows


class Connection:
    def __init__(self, pool):
        self.pool = pool

    def execute(self, sql, values=None):
        if self.pool.fail:
            raise psycopg.OperationalError("secret database detail")
        if "app.api_keys" in sql:
            return Result(self.pool.auth_row)
        return Result((1,))

    def cursor(self, **_):
        if self.pool.fail:
            raise psycopg.OperationalError("secret database detail")
        return Cursor(self.pool)


class Pool:
    def __init__(self, auth_row=("ccgp_test", 60), rows=None, fail=False):
        self.auth_row = auth_row
        self.rows = rows or []
        self.fail = fail
        self.last_query = None

    @contextmanager
    def connection(self):
        yield Connection(self)


def client(pool=None):
    query_app.app.state.pool = pool or Pool()
    query_app.app.state.cursor_secret = SECRET
    query_app.app.state.limiter = RateLimiter()
    query_app.app.state.ip_rate_limit = 300
    return TestClient(query_app.app)


def test_query_builds_bound_filters_and_inclusive_end_date():
    params = QueryParams(
        region_code="420000",
        start_date="2026-09-01",
        end_date="2026-09-30",
        keyword="100%_",
        limit=20,
    )
    sql, values = build_query(params, None)
    assert "420000" not in sql
    assert "2026-09" not in sql
    assert "ILIKE %s" in sql
    assert values[0] == "420000"
    assert values[2].isoformat() == "2026-10-01T00:00:00+08:00"
    assert values[3] == r"%100\%\_%"
    assert values[-1] == 21


def test_cursor_is_signed_and_bound_to_filters():
    published = datetime(2026, 9, 19, 2, 0, tzinfo=timezone.utc)
    cursor = encode_cursor(SECRET, published, 12, "fingerprint")
    assert decode_cursor(SECRET, cursor, "fingerprint") == (published, 12)
    with pytest.raises(HTTPException):
        decode_cursor(
            SECRET, cursor[:-1] + ("A" if cursor[-1] != "A" else "B"), "fingerprint"
        )
    with pytest.raises(HTTPException):
        decode_cursor(SECRET, cursor, "other")


def test_expired_cursor_is_rejected(monkeypatch):
    published = datetime(2026, 9, 19, 2, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(query_app.time, "time", lambda: 1_000_000)
    cursor = encode_cursor(SECRET, published, 12, "fingerprint")
    monkeypatch.setattr(
        query_app.time, "time", lambda: 1_000_000 + query_app.MAX_CURSOR_AGE + 1
    )
    with pytest.raises(HTTPException):
        decode_cursor(SECRET, cursor, "fingerprint")


def test_query_validation():
    with pytest.raises(ValueError):
        QueryParams(start_date="2026-09-30", end_date="2026-09-01")
    with pytest.raises(ValueError):
        QueryParams(start_date="2024-01-01", end_date="2025-01-01")
    with pytest.raises(ValueError):
        QueryParams(region_code="42000")


def test_like_escaping():
    assert escape_like(r"a%_\\b") == r"a\%\_\\\\b"


@pytest.mark.parametrize("state", ["missing", "invalid", "disabled", "expired"])
def test_missing_or_unusable_keys_return_same_error(state):
    pool = Pool(auth_row=None)
    headers = {} if state == "missing" else AUTH
    response = client(pool).get("/api/v1/notices", headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid API key"}


def test_parameter_validation_at_http_boundary():
    api = client()
    assert api.get("/api/v1/notices?limit=0", headers=AUTH).status_code == 422
    assert api.get("/api/v1/notices?limit=101", headers=AUTH).status_code == 422
    assert api.get("/api/v1/notices?region_code=42000", headers=AUTH).status_code == 422
    assert (
        api.get("/api/v1/notices?keyword=" + "x" * 101, headers=AUTH).status_code == 422
    )
    assert api.get("/api/v1/notices?start_date=bad", headers=AUTH).status_code == 422
    assert (
        api.get(
            "/api/v1/notices?start_date=2026-10-01&end_date=2026-09-01",
            headers=AUTH,
        ).status_code
        == 422
    )


def test_stable_pagination_and_response_fields():
    published = datetime(2026, 9, 19, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    pool = Pool(
        rows=[
            {
                "id": 3,
                "title": "three",
                "canonical_url": "https://example/3",
                "published_at": published,
                "region_code": "420000",
                "region_raw": "湖北",
            },
            {
                "id": 2,
                "title": "two",
                "canonical_url": "https://example/2",
                "published_at": published,
                "region_code": "420000",
                "region_raw": "湖北",
            },
            {
                "id": 1,
                "title": "one",
                "canonical_url": "https://example/1",
                "published_at": published,
                "region_code": "420000",
                "region_raw": "湖北",
            },
        ]
    )
    api = client(pool)
    first = api.get("/api/v1/notices?region_code=420000&limit=2", headers=AUTH)
    assert first.status_code == 200
    assert [item["id"] for item in first.json()["items"]] == [3, 2]
    assert first.json()["items"][0]["published_at"].endswith("+08:00")
    assert set(first.json()["items"][0]) == {
        "id",
        "title",
        "canonical_url",
        "published_at",
        "region_code",
        "region_raw",
    }

    pool.rows = [
        {
            "id": 1,
            "title": "one",
            "canonical_url": "https://example/1",
            "published_at": published,
            "region_code": "420000",
            "region_raw": "湖北",
        }
    ]
    second = api.get(
        "/api/v1/notices",
        params={
            "region_code": "420000",
            "limit": 2,
            "cursor": first.json()["next_cursor"],
        },
        headers=AUTH,
    )
    assert second.status_code == 200
    assert [item["id"] for item in second.json()["items"]] == [1]
    assert second.json()["next_cursor"] is None
    assert "(published_at, id) < (%s, %s)" in pool.last_query[0]


def test_cursor_cannot_be_reused_with_other_filters():
    published = datetime.now(timezone.utc)
    params = QueryParams(region_code="420000")
    cursor = encode_cursor(SECRET, published, 1, params.fingerprint())
    response = client().get(
        "/api/v1/notices",
        params={"region_code": "440000", "cursor": cursor},
        headers=AUTH,
    )
    assert response.status_code == 400


def test_rate_limit_has_retry_after():
    api = client(Pool(auth_row=("ccgp_test", 1)))
    assert api.get("/api/v1/notices", headers=AUTH).status_code == 200
    response = api.get("/api/v1/notices", headers=AUTH)
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


def test_database_error_is_sanitized():
    response = client(Pool(fail=True)).get("/api/v1/notices", headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    assert "secret" not in response.text
