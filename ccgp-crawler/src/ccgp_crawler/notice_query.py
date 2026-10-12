"""Read-only queries against the notices already collected on this machine."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class NoticeQueryParams(BaseModel):
    region_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")
    start_date: date | None = None
    end_date: date | None = None
    keyword: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=6, ge=1, le=100)

    @model_validator(mode="after")
    def validate_dates(self) -> "NoticeQueryParams":
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("开始日期不得晚于结束日期")
        if self.end_date == date.max:
            raise ValueError("结束日期超出支持范围")
        return self

    def fingerprint(self) -> str:
        filters = self.model_dump(mode="json", exclude={"limit", "keyword"})
        filters["terms"] = (self.keyword or "").split()
        return hashlib.sha256(
            json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class NoticeOutput(BaseModel):
    id: int
    title: str
    canonical_url: str
    published_at: str
    region_code: str | None
    region_raw: str | None


class NoticeQueryResponse(BaseModel):
    items: list[NoticeOutput]
    next_cursor: str | None
    limit: int
    total_count: int


def encode_cursor(published_at: str, row_id: int, fingerprint: str) -> str:
    raw = json.dumps(
        {"v": 1, "published_at": published_at, "id": row_id, "f": fingerprint},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str, fingerprint: str) -> tuple[str, int]:
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        published_at = payload["published_at"]
        row_id = payload["id"]
        stamp = datetime.fromisoformat(published_at)
        if (
            payload.get("v") != 1
            or payload.get("f") != fingerprint
            or type(row_id) is not int
            or row_id < 1
            or stamp.utcoffset() != timedelta(hours=8)
            or stamp.isoformat(timespec="seconds") != published_at
        ):
            raise ValueError
        return published_at, row_id
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(400, "分页游标无效，请重新查询") from exc


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def notice_conditions(params: NoticeQueryParams) -> tuple[list[str], list[object]]:
    conditions = [
        "(canonical_url LIKE 'https://www.ccgp.gov.cn/cggg/zygg/gkzb/%' "
        "OR canonical_url LIKE 'https://www.ccgp.gov.cn/cggg/dfgg/gkzb/%')"
    ]
    values: list[object] = []
    if params.region_code:
        conditions.append("region_code = ?")
        values.append(params.region_code)
    if params.start_date:
        conditions.append("published_at >= ?")
        values.append(datetime.combine(params.start_date, time.min, BEIJING_TZ).isoformat())
    if params.end_date:
        conditions.append("published_at < ?")
        values.append(
            datetime.combine(params.end_date + timedelta(days=1), time.min, BEIJING_TZ).isoformat()
        )
    for term in (params.keyword or "").split():
        conditions.append("title LIKE ? ESCAPE '\\'")
        values.append(f"%{escape_like(term)}%")
    return conditions, values


def query_notices(
    database_path: str | Path, params: NoticeQueryParams, cursor: str | None = None
) -> NoticeQueryResponse:
    conditions, values = notice_conditions(params)
    count_sql = f"SELECT count(*) FROM pending_notices WHERE {' AND '.join(conditions)}"
    count_values = values.copy()
    if cursor is not None:
        conditions.append("(published_at, rowid) < (?, ?)")
        values.extend(decode_cursor(cursor, params.fingerprint()))
    values.append(params.limit + 1)
    sql = (
        "SELECT rowid AS id, title, canonical_url, published_at, region_code, region_raw "
        f"FROM pending_notices WHERE {' AND '.join(conditions)} "
        "ORDER BY published_at DESC, rowid DESC LIMIT ?"
    )
    try:
        # mode=ro also prevents accidentally creating an empty database on a bad path.
        uri = Path(database_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            total_count = connection.execute(count_sql, count_values).fetchone()[0]
            rows = connection.execute(sql, values).fetchall()
        items = [NoticeOutput(**dict(row)) for row in rows[: params.limit]]
    except (sqlite3.Error, OSError, ValidationError) as exc:
        raise HTTPException(503, "本地采集数据库暂时不可用，请稍后重试") from exc
    next_cursor = None
    if len(rows) > params.limit:
        next_cursor = encode_cursor(items[-1].published_at, items[-1].id, params.fingerprint())
    return NoticeQueryResponse(
        items=items, next_cursor=next_cursor, limit=params.limit, total_count=total_count
    )
