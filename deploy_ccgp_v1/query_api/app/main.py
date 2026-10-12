from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pwd
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import psycopg
from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SHANGHAI = timezone(timedelta(hours=8))
MAX_CURSOR_AGE = 24 * 60 * 60
DEFAULT_RATE = 60


def read_secret(name: str) -> str:
    path = os.environ.get(name)
    if not path:
        raise RuntimeError(f"{name} is required")
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"{name} is empty")
    return value


def connection_info() -> dict[str, object]:
    return {
        "host": os.environ.get("DB_HOST", "postgres"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": read_secret("DB_PASSWORD_FILE"),
        "connect_timeout": 10,
        "options": "-c default_transaction_read_only=on -c statement_timeout=5000",
    }


class QueryParams(BaseModel):
    region_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")
    start_date: date | None = None
    end_date: date | None = None
    keyword: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("keyword")
    @classmethod
    def clean_keyword(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> "QueryParams":
        if self.start_date and self.end_date:
            if self.start_date > self.end_date:
                raise ValueError("start_date must not be after end_date")
            if (self.end_date - self.start_date).days >= 366:
                raise ValueError("date range must not exceed 366 days")
        return self

    def fingerprint(self) -> str:
        value = self.model_dump(mode="json", exclude={"limit"})
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class NoticeOutput(BaseModel):
    id: int
    title: str
    canonical_url: str
    published_at: str
    region_code: str | None
    region_raw: str | None


class NoticeResponse(BaseModel):
    items: list[NoticeOutput]
    next_cursor: str | None
    limit: int


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}
        self._last_prune = time.monotonic()

    def allow(self, key: str, limit: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            if now - self._last_prune >= 60:
                self._hits = {
                    bucket: [stamp for stamp in stamps if now - stamp < 60]
                    for bucket, stamps in self._hits.items()
                    if any(now - stamp < 60 for stamp in stamps)
                }
                self._last_prune = now
            hits = [stamp for stamp in self._hits.get(key, []) if now - stamp < 60]
            if len(hits) >= limit:
                retry = max(1, int(60 - (now - hits[0])))
                self._hits[key] = hits
                return False, retry
            hits.append(now)
            self._hits[key] = hits
            return True, 0


def encode_cursor(
    secret: str, published_at: datetime, row_id: int, fingerprint: str
) -> str:
    payload = {
        "v": 1,
        "published_at": published_at.astimezone(timezone.utc).isoformat(),
        "id": row_id,
        "f": fingerprint,
        "iat": int(time.time()),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    encoded_payload = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{encoded_payload}.{encoded_signature}"


def decode_base64url(value: str) -> bytes:
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != value:
        raise ValueError
    return decoded


def decode_cursor(secret: str, value: str, fingerprint: str) -> tuple[datetime, int]:
    try:
        encoded_payload, encoded_signature = value.split(".", 1)
        payload_raw = decode_base64url(encoded_payload)
        signature = decode_base64url(encoded_signature)
        expected = hmac.new(secret.encode(), payload_raw, hashlib.sha256).digest()
        payload = json.loads(payload_raw)
        issued = int(payload["iat"])
        published_at = datetime.fromisoformat(payload["published_at"])
        row_id = int(payload["id"])
        if (
            not hmac.compare_digest(signature, expected)
            or payload.get("v") != 1
            or payload.get("f") != fingerprint
        ):
            raise ValueError
        if time.time() - issued > MAX_CURSOR_AGE or time.time() - issued < -60:
            raise ValueError
        if published_at.tzinfo is None or row_id < 1:
            raise ValueError
        return published_at, row_id
    except (
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise HTTPException(400, "invalid cursor") from exc


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_query(
    params: QueryParams, cursor: tuple[datetime, int] | None
) -> tuple[str, list[object]]:
    conditions: list[str] = []
    values: list[object] = []
    if params.region_code:
        values.append(params.region_code)
        conditions.append("region_code = %s")
    if params.start_date:
        values.append(datetime.combine(params.start_date, dtime.min, SHANGHAI))
        conditions.append("published_at >= %s")
    if params.end_date:
        end = params.end_date + timedelta(days=1)
        values.append(datetime.combine(end, dtime.min, SHANGHAI))
        conditions.append("published_at < %s")
    if params.keyword:
        values.append(f"%{escape_like(params.keyword)}%")
        conditions.append("title ILIKE %s ESCAPE E'\\\\'")
    if cursor:
        values.extend(cursor)
        conditions.append("(published_at, id) < (%s, %s)")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = (
        "SELECT id, title, canonical_url, published_at, region_code, region_raw "
        f"FROM app.notices {where} ORDER BY published_at DESC, id DESC LIMIT %s"
    )
    values.append(params.limit + 1)
    return sql, values


def authenticate(request: Request, api_key: str | None) -> dict[str, object]:
    if not api_key or not 20 <= len(api_key) <= 200:
        raise HTTPException(401, "invalid API key")
    digest = hashlib.sha256(api_key.encode()).hexdigest()
    try:
        with request.app.state.pool.connection() as conn:
            row = conn.execute(
                "SELECT key_prefix, requests_per_minute FROM app.api_keys "
                "WHERE key_hash=%s AND enabled AND (expires_at IS NULL OR expires_at > now())",
                (digest,),
            ).fetchone()
    except psycopg.Error as exc:
        raise HTTPException(503, "database unavailable") from exc
    if not row:
        raise HTTPException(401, "invalid API key")
    return {"prefix": row[0], "rate": row[1] or DEFAULT_RATE}


@asynccontextmanager
async def lifespan(app: FastAPI):
    cursor_secret = read_secret("CURSOR_SECRET_FILE")
    if len(cursor_secret) < 32:
        raise RuntimeError("CURSOR_SECRET_FILE must contain at least 32 characters")
    if os.getuid() == 0:
        account = pwd.getpwnam("appuser")
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    pool = ConnectionPool(
        kwargs=connection_info(),
        min_size=1,
        max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "5")),
        open=False,
        timeout=10,
    )
    pool.open(wait=True)
    app.state.pool = pool
    app.state.cursor_secret = cursor_secret
    app.state.limiter = RateLimiter()
    app.state.ip_rate_limit = int(os.environ.get("IP_RATE_LIMIT", "300"))
    yield
    pool.close()


app = FastAPI(title="CCGP Query API", version="1.0.0", lifespan=lifespan)


@app.get("/healthz")
def health(request: Request) -> dict[str, str]:
    try:
        with request.app.state.pool.connection() as conn:
            conn.execute("SELECT 1").fetchone()
    except psycopg.Error as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ok", "database": "ok"}


@app.get("/api/v1/notices", response_model=NoticeResponse)
def list_notices(
    request: Request,
    region_code: str | None = Query(default=None, pattern=r"^[0-9]{6}$"),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1024),
    x_api_key: str | None = Header(default=None),
) -> NoticeResponse:
    auth = authenticate(request, x_api_key)
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry = request.app.state.limiter.allow(
        f"key:{auth['prefix']}", int(auth["rate"])
    )
    if allowed:
        allowed, retry = request.app.state.limiter.allow(
            f"ip:{client_ip}", request.app.state.ip_rate_limit
        )
    if not allowed:
        raise HTTPException(
            429, "rate limit exceeded", headers={"Retry-After": str(retry)}
        )
    try:
        params = QueryParams(
            region_code=region_code,
            start_date=start_date,
            end_date=end_date,
            keyword=keyword,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(422, "invalid query parameters") from exc
    marker = (
        decode_cursor(request.app.state.cursor_secret, cursor, params.fingerprint())
        if cursor
        else None
    )
    sql, values = build_query(params, marker)
    try:
        with request.app.state.pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, values)
                rows = cur.fetchall()
    except psycopg.Error as exc:
        raise HTTPException(503, "database unavailable") from exc
    has_more = len(rows) > params.limit
    rows = rows[: params.limit]
    items = [
        {**row, "published_at": row["published_at"].astimezone(SHANGHAI).isoformat()}
        for row in rows
    ]
    next_cursor = None
    if has_more and rows:
        next_cursor = encode_cursor(
            request.app.state.cursor_secret,
            rows[-1]["published_at"],
            rows[-1]["id"],
            params.fingerprint(),
        )
    return NoticeResponse(items=items, next_cursor=next_cursor, limit=params.limit)
