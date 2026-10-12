from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

import httpx

from .local_store import initialize_database, open_database, utc_now_iso


NOTICE_COLUMNS = (
    "canonical_url",
    "title",
    "published_at",
    "region_raw",
    "region_code",
    "category",
    "source_list_url",
)
RETRY_DELAYS = (1.0, 5.0, 30.0)


class SyncError(RuntimeError):
    """A synchronization run cannot continue safely."""


def ingest_api_key() -> str | None:
    """Load the ingest key without ever exposing it through the frontend."""

    value = os.getenv("CCGP_INGEST_API_KEY")
    if value:
        return value
    key_file = os.getenv("CCGP_INGEST_API_KEY_FILE")
    if not key_file:
        return None
    value = Path(key_file).read_text(encoding="utf-8").strip()
    return value or None


@dataclass(frozen=True, slots=True)
class PreparedBatch:
    batch_key: str
    payload_sha256: str
    items: list[dict[str, str | None]]

    def request_json(self) -> dict[str, Any]:
        return {
            "batch_key": self.batch_key,
            "payload_sha256": self.payload_sha256,
            "items": self.items,
        }


@dataclass(slots=True)
class SyncSummary:
    batches: int = 0
    synced: int = 0
    inserted: int = 0
    updated: int = 0
    replayed_batches: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "batches": self.batches,
            "synced": self.synced,
            "inserted": self.inserted,
            "updated": self.updated,
            "replayed_batches": self.replayed_batches,
        }


def canonical_items_json(items: list[dict[str, str | None]]) -> str:
    return json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_sha256(items: list[dict[str, str | None]]) -> str:
    return hashlib.sha256(canonical_items_json(items).encode("utf-8")).hexdigest()


def make_batch_key(items: list[dict[str, str | None]], digest: str) -> str:
    urls = sorted(str(item["canonical_url"]) for item in items)
    url_digest = hashlib.sha256("\n".join(urls).encode("utf-8")).hexdigest()
    return f"ccgp-{len(items):03d}-{url_digest[:16]}-{digest[:16]}"


def recover_stale_syncs(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(minutes=30),
) -> int:
    instant = now or datetime.now(timezone.utc)
    cutoff = (instant - stale_after).isoformat(timespec="seconds")
    timestamp = instant.isoformat(timespec="seconds")
    with connection:
        rows = connection.execute(
            "SELECT canonical_url FROM pending_notices "
            "WHERE sync_status='syncing' AND updated_at < ?",
            (cutoff,),
        ).fetchall()
        connection.execute(
            """
            UPDATE pending_notices
            SET sync_status='pending', last_error='recovered stale sync', updated_at=?
            WHERE sync_status='syncing' AND updated_at < ?
            """,
            (timestamp, cutoff),
        )
        connection.execute(
            """
            UPDATE sync_batches
            SET status='failed', last_error='recovered stale sync', updated_at=?
            WHERE status='syncing' AND updated_at < ?
            """,
            (timestamp, cutoff),
        )
    return len(rows)


def _read_pending(connection: sqlite3.Connection, limit: int) -> list[dict[str, str | None]]:
    columns = ", ".join(NOTICE_COLUMNS)
    rows = connection.execute(
        f"SELECT {columns} FROM pending_notices "
        "WHERE sync_status='pending' ORDER BY created_at, canonical_url LIMIT ?",
        (limit,),
    ).fetchall()
    return [{column: row[column] for column in NOTICE_COLUMNS} for row in rows]


def prepare_batch(
    connection: sqlite3.Connection,
    limit: int,
    *,
    dry_run: bool = False,
) -> PreparedBatch | None:
    if not 1 <= limit <= 500:
        raise ValueError("batch size must be between 1 and 500")
    items = _read_pending(connection, limit)
    if not items:
        return None
    digest = payload_sha256(items)
    batch_key = make_batch_key(items, digest)
    batch = PreparedBatch(batch_key, digest, items)
    if dry_run:
        return batch

    now = utc_now_iso()
    urls = [str(item["canonical_url"]) for item in items]
    with connection:
        prior = connection.execute(
            "SELECT payload_sha256, status FROM sync_batches WHERE batch_key=?",
            (batch_key,),
        ).fetchone()
        if prior and prior["payload_sha256"] != digest:
            raise SyncError(f"local batch key collision: {batch_key}")
        connection.execute(
            """
            INSERT INTO sync_batches(
                batch_key, payload_sha256, item_count, status, attempt_count,
                last_error, created_at, updated_at, synced_at
            ) VALUES (?, ?, ?, 'syncing', 1, NULL, ?, ?, NULL)
            ON CONFLICT(batch_key) DO UPDATE SET
                status='syncing', attempt_count=sync_batches.attempt_count+1,
                last_error=NULL, updated_at=excluded.updated_at, synced_at=NULL
            """,
            (batch_key, digest, len(items), now, now),
        )
        connection.execute("DELETE FROM sync_batch_items WHERE batch_key=?", (batch_key,))
        connection.executemany(
            "INSERT INTO sync_batch_items(batch_key, canonical_url, position) VALUES (?, ?, ?)",
            [(batch_key, url, position) for position, url in enumerate(urls)],
        )
        placeholders = ",".join("?" for _ in urls)
        connection.execute(
            f"UPDATE pending_notices SET sync_status='syncing', last_error=NULL, updated_at=? "
            f"WHERE sync_status='pending' AND canonical_url IN ({placeholders})",
            (now, *urls),
        )
    return batch


def mark_batch_synced(connection: sqlite3.Connection, batch_key: str) -> int:
    now = utc_now_iso()
    with connection:
        result = connection.execute(
            """
            UPDATE pending_notices
            SET sync_status='synced', last_error=NULL, synced_at=?, updated_at=?
            WHERE sync_status='syncing' AND canonical_url IN (
                SELECT canonical_url FROM sync_batch_items WHERE batch_key=?
            )
            """,
            (now, now, batch_key),
        )
        connection.execute(
            "UPDATE sync_batches SET status='synced', last_error=NULL, synced_at=?, updated_at=? "
            "WHERE batch_key=?",
            (now, now, batch_key),
        )
    return result.rowcount


def fail_batch(
    connection: sqlite3.Connection,
    batch_key: str,
    error: str,
    *,
    terminal: bool,
) -> None:
    now = utc_now_iso()
    next_status = "failed" if terminal else "pending"
    message = error[:1000]
    with connection:
        connection.execute(
            f"""
            UPDATE pending_notices
            SET sync_status='{next_status}', retry_count=retry_count+1,
                last_error=?, updated_at=?
            WHERE sync_status='syncing' AND canonical_url IN (
                SELECT canonical_url FROM sync_batch_items WHERE batch_key=?
            )
            """,
            (message, now, batch_key),
        )
        connection.execute(
            "UPDATE sync_batches SET status='failed', last_error=?, updated_at=? WHERE batch_key=?",
            (message, now, batch_key),
        )


def _response_error(response: httpx.Response) -> str:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    return f"HTTP {response.status_code}: {str(detail)[:700]}"


def _retry_after(response: httpx.Response, fallback: float) -> float:
    value = response.headers.get("Retry-After")
    if value:
        try:
            return min(300.0, max(0.0, float(value)))
        except ValueError:
            pass
    return fallback


def sync_pending(
    database: str | Path,
    *,
    endpoint: str | None = None,
    api_key: str | None = None,
    batch_size: int = 200,
    max_items: int | None = None,
    max_batches: int | None = None,
    dry_run: bool = False,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    output: TextIO | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> SyncSummary:
    if not 1 <= batch_size <= 500:
        raise ValueError("batch size must be between 1 and 500")
    if max_items is not None and max_items < 1:
        raise ValueError("max items must be positive")
    if max_batches is not None and max_batches < 1:
        raise ValueError("max batches must be positive")
    endpoint = (endpoint or os.getenv("CCGP_INGEST_URL", "")).rstrip("/")
    api_key = api_key or ingest_api_key()
    if not dry_run and not endpoint:
        raise ValueError("ingest endpoint is required")
    if not dry_run and not api_key:
        raise ValueError("ingest API key is required")

    stream = output
    summary = SyncSummary()
    owns_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
    try:
        with open_database(database) as connection:
            initialize_database(connection)
            recover_stale_syncs(connection)
            while True:
                if max_batches is not None and summary.batches >= max_batches:
                    break
                remaining = None if max_items is None else max_items - summary.synced
                if remaining is not None and remaining <= 0:
                    break
                limit = batch_size if remaining is None else min(batch_size, remaining)
                batch = prepare_batch(connection, limit, dry_run=dry_run)
                if batch is None:
                    break
                if dry_run:
                    if stream:
                        print(json.dumps(batch.request_json(), ensure_ascii=False), file=stream)
                    summary.batches += 1
                    summary.synced += len(batch.items)
                    break

                if on_progress:
                    on_progress({
                        "event": "batch_started",
                        "batch_key": batch.batch_key,
                        "batch_size": len(batch.items),
                        "batches_completed": summary.batches,
                        "synced": summary.synced,
                        "inserted": summary.inserted,
                        "updated": summary.updated,
                        "replayed_batches": summary.replayed_batches,
                    })

                response: httpx.Response | None = None
                request_error: Exception | None = None
                for attempt in range(len(RETRY_DELAYS) + 1):
                    request_error = None
                    try:
                        response = http.post(
                            f"{endpoint}/internal/v1/notices/batch",
                            headers={"Authorization": f"Bearer {api_key}"},
                            json=batch.request_json(),
                        )
                    except httpx.RequestError as exc:
                        request_error = exc
                    retryable = request_error is not None or (
                        response is not None
                        and (response.status_code == 429 or response.status_code >= 500)
                    )
                    if not retryable or attempt == len(RETRY_DELAYS):
                        break
                    delay = RETRY_DELAYS[attempt]
                    if response is not None and response.status_code == 429:
                        delay = _retry_after(response, delay)
                    sleep(delay)

                if request_error is not None:
                    error = f"network error: {request_error}"
                    fail_batch(connection, batch.batch_key, error, terminal=False)
                    raise SyncError(error) from request_error
                assert response is not None
                if response.status_code in {401, 403}:
                    error = _response_error(response)
                    fail_batch(connection, batch.batch_key, error, terminal=False)
                    raise SyncError(error)
                if response.status_code in {400, 409, 422}:
                    error = _response_error(response)
                    fail_batch(connection, batch.batch_key, error, terminal=True)
                    raise SyncError(error)
                if response.status_code == 429 or response.status_code >= 500:
                    error = _response_error(response)
                    fail_batch(connection, batch.batch_key, error, terminal=False)
                    raise SyncError(error)
                try:
                    response.raise_for_status()
                    result = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    error = f"invalid API response: {exc}"
                    fail_batch(connection, batch.batch_key, error, terminal=False)
                    raise SyncError(error) from exc
                if result.get("batch_key") != batch.batch_key:
                    error = "API returned a different batch_key"
                    fail_batch(connection, batch.batch_key, error, terminal=False)
                    raise SyncError(error)

                synced = mark_batch_synced(connection, batch.batch_key)
                summary.batches += 1
                summary.synced += synced
                summary.inserted += int(result.get("inserted", 0))
                summary.updated += int(result.get("updated", 0))
                summary.replayed_batches += int(bool(result.get("replayed", False)))
                if on_progress:
                    on_progress({
                        "event": "batch_completed",
                        "batch_key": batch.batch_key,
                        "batch_size": len(batch.items),
                        "batches_completed": summary.batches,
                        "synced": summary.synced,
                        "inserted": summary.inserted,
                        "updated": summary.updated,
                        "replayed_batches": summary.replayed_batches,
                    })
                if stream:
                    print(json.dumps(result, ensure_ascii=False, sort_keys=True), file=stream)
    finally:
        if owns_client:
            http.close()
    return summary
