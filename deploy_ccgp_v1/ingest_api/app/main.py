from __future__ import annotations

import hashlib
import hmac
import json
import os
import pwd
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


MAX_REQUEST_BYTES = 1_000_000
NOTICE_COLUMNS = (
    "canonical_url",
    "title",
    "published_at",
    "region_raw",
    "region_code",
    "category",
    "source_list_url",
)


def read_secret(file_env: str) -> str:
    path = os.environ.get(file_env)
    if not path:
        raise RuntimeError(f"{file_env} is required")
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"{file_env} is empty")
    return value


def database_connection_info() -> dict[str, object]:
    return {
        "host": os.environ.get("DB_HOST", "postgres"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": read_secret("DB_PASSWORD_FILE"),
        "connect_timeout": 10,
    }


class NoticeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    canonical_url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=1000)
    published_at: str = Field(min_length=1, max_length=64)
    region_raw: str | None = Field(default=None, max_length=100)
    region_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")
    category: str = Field(pattern=r"^(zygg|dfgg)$")
    source_list_url: str = Field(min_length=1, max_length=4096)

    @field_validator("canonical_url")
    @classmethod
    def validate_notice_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.ccgp.gov.cn"
            or not parsed.path.startswith("/cggg/")
            or not parsed.path.endswith(".htm")
            or parsed.fragment
        ):
            raise ValueError("canonical_url must be an HTTPS CCGP notice URL")
        return value

    @field_validator("source_list_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname not in {
            "www.ccgp.gov.cn",
            "search.ccgp.gov.cn",
        }:
            raise ValueError("source_list_url must be an HTTPS CCGP URL")
        return value

    @field_validator("published_at")
    @classmethod
    def validate_published_at(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("published_at must be ISO 8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(hours=8):
            raise ValueError("published_at must include the +08:00 timezone")
        return value

    def canonical_dict(self) -> dict[str, str | None]:
        return {column: getattr(self, column) for column in NOTICE_COLUMNS}


class BatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    batch_key: str = Field(min_length=8, max_length=160, pattern=r"^[A-Za-z0-9._-]+$")
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    items: list[NoticeInput] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_unique_urls(self) -> "BatchInput":
        urls = [item.canonical_url for item in self.items]
        if len(set(urls)) != len(urls):
            raise ValueError("items contain duplicate canonical_url values")
        return self

    def computed_sha256(self) -> str:
        items = [item.canonical_dict() for item in self.items]
        encoded = json.dumps(
            items, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(401, "missing bearer token")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "invalid authorization header")
    return token


UPSERT = """
INSERT INTO app.notices(
    canonical_url, title, published_at, region_raw, region_code,
    category, source_list_url
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (canonical_url) DO UPDATE SET
    title = excluded.title,
    published_at = excluded.published_at,
    region_raw = excluded.region_raw,
    region_code = excluded.region_code,
    category = excluded.category,
    source_list_url = excluded.source_list_url,
    last_seen_at = now(),
    updated_at = now()
RETURNING (xmax = 0) AS inserted
"""


def process_batch(pool: ConnectionPool, batch: BatchInput) -> dict[str, object]:
    digest = batch.computed_sha256()
    if not hmac.compare_digest(digest, batch.payload_sha256):
        raise HTTPException(422, "payload_sha256 does not match items")

    try:
        with pool.connection() as connection:
            with connection.transaction():
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (batch.batch_key,))
                    cursor.execute(
                        """
                        SELECT batch_key, payload_sha256, received_count,
                               inserted_count, updated_count
                        FROM app.ingest_batches WHERE batch_key=%s
                        """,
                        (batch.batch_key,),
                    )
                    prior = cursor.fetchone()
                    if prior:
                        if not hmac.compare_digest(prior["payload_sha256"], digest):
                            raise HTTPException(409, "batch_key already has a different payload")
                        return {
                            "batch_key": prior["batch_key"],
                            "received": prior["received_count"],
                            "inserted": prior["inserted_count"],
                            "updated": prior["updated_count"],
                            "rejected": 0,
                            "replayed": True,
                        }

                    inserted = 0
                    for item in batch.items:
                        cursor.execute(
                            UPSERT,
                            (
                                item.canonical_url,
                                item.title,
                                item.published_at,
                                item.region_raw,
                                item.region_code,
                                item.category,
                                item.source_list_url,
                            ),
                        )
                        inserted += int(cursor.fetchone()["inserted"])
                    updated = len(batch.items) - inserted
                    cursor.execute(
                        """
                        INSERT INTO app.ingest_batches(
                            batch_key, payload_sha256, received_count,
                            inserted_count, updated_count, source_name
                        ) VALUES (%s, %s, %s, %s, %s, 'local-crawler')
                        """,
                        (batch.batch_key, digest, len(batch.items), inserted, updated),
                    )
        return {
            "batch_key": batch.batch_key,
            "received": len(batch.items),
            "inserted": inserted,
            "updated": updated,
            "rejected": 0,
            "replayed": False,
        }
    except HTTPException:
        raise
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(422, "one or more region_code values are unknown") from exc
    except psycopg.Error as exc:
        raise HTTPException(503, "database operation failed") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    api_key = read_secret("INGEST_API_KEY_FILE")
    connection_info = database_connection_info()
    # Compose file-backed secrets preserve their host mode (0600). Read them
    # once as the container launcher, then permanently drop privileges before
    # accepting traffic or opening database connections.
    if os.getuid() == 0:
        account = pwd.getpwnam("appuser")
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    pool = ConnectionPool(
        kwargs=connection_info,
        min_size=int(os.environ.get("DB_POOL_MIN_SIZE", "1")),
        max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "5")),
        open=False,
        timeout=10,
    )
    pool.open(wait=True)
    app.state.api_key = api_key
    app.state.pool = pool
    yield
    pool.close()


app = FastAPI(title="CCGP Ingest API", version="1.0.0", lifespan=lifespan)


@app.get("/healthz")
def health(request: Request) -> dict[str, str]:
    try:
        with request.app.state.pool.connection() as connection:
            connection.execute("SELECT 1").fetchone()
    except psycopg.Error as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ok", "database": "ok"}


@app.get("/internal/v1/healthz")
def authenticated_health(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    token = bearer_token(authorization)
    if not hmac.compare_digest(token, request.app.state.api_key):
        raise HTTPException(403, "invalid bearer token")
    return {"status": "ok", "authorization": "ok"}


@app.post("/internal/v1/notices/batch")
async def ingest_batch(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    token = bearer_token(authorization)
    if not hmac.compare_digest(token, request.app.state.api_key):
        raise HTTPException(403, "invalid bearer token")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                raise HTTPException(413, "request body exceeds 1 MB")
        except ValueError as exc:
            raise HTTPException(400, "invalid content-length") from exc
    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        raise HTTPException(413, "request body exceeds 1 MB")
    try:
        batch = BatchInput.model_validate_json(body)
    except ValueError as exc:
        raise HTTPException(422, "invalid batch payload") from exc
    return process_batch(request.app.state.pool, batch)
