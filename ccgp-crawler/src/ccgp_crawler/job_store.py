from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .local_store import initialize_database, open_database, save_notices, utc_now_iso
from .models import Notice


JOB_STATUSES = {
    "queued",
    "running",
    "stopping",
    "stopped",
    "blocked",
    "failed",
    "completed",
}
TERMINAL_STATUSES = {"stopped", "blocked", "failed", "completed"}
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
DAILY_REQUEST_LIMIT = 240
MIN_JOB_GAP_SECONDS = 15 * 60


def inclusive_dates(start: date, end: date) -> list[date]:
    if start > end:
        raise ValueError("start date must not be after end date")
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


class JobStore:
    """Small connection-per-operation store safe for API and worker threads."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        delays = (0.0, 0.1, 0.3)
        for attempt, delay in enumerate(delays):
            if delay:
                time.sleep(delay)
            connection: sqlite3.Connection | None = None
            try:
                connection = open_database(self.path)
                connection.execute("PRAGMA busy_timeout = 5000")
                return connection
            except sqlite3.OperationalError:
                if connection is not None:
                    connection.close()
                if attempt == len(delays) - 1:
                    raise
        raise AssertionError("unreachable")

    def initialize(self) -> None:
        with self.connect() as connection:
            initialize_database(connection)
            migration = Path(__file__).resolve().parents[2] / "migrations" / "sqlite_003_jobs.sql"
            connection.executescript(migration.read_text(encoding="utf-8"))
            throttle_migration = Path(__file__).resolve().parents[2] / "migrations" / "sqlite_005_throttle.sql"
            connection.executescript(throttle_migration.read_text(encoding="utf-8"))
            throttle_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(crawl_throttle_state)")
            }
            if "resume_required" not in throttle_columns:
                connection.execute(
                    "ALTER TABLE crawl_throttle_state ADD COLUMN resume_required INTEGER NOT NULL DEFAULT 0"
                )
            if "confirmed_job_id" not in throttle_columns:
                connection.execute(
                    "ALTER TABLE crawl_throttle_state ADD COLUMN confirmed_job_id TEXT"
                )
            if "pause_resume_job_id" not in throttle_columns:
                connection.execute(
                    "ALTER TABLE crawl_throttle_state ADD COLUMN pause_resume_job_id TEXT"
                )
            if "last_job_id" not in throttle_columns:
                connection.execute(
                    "ALTER TABLE crawl_throttle_state ADD COLUMN last_job_id TEXT"
                )
            page_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(crawl_page_runs)")
            }
            if "job_id" not in page_columns:
                connection.execute("ALTER TABLE crawl_page_runs ADD COLUMN job_id TEXT")
            if "slice_date" not in page_columns:
                connection.execute("ALTER TABLE crawl_page_runs ADD COLUMN slice_date TEXT")
            now = utc_now_iso()
            connection.execute(
                """
                INSERT INTO schedule_settings(id, enabled, run_time, timezone, updated_at)
                VALUES (1, 1, '02:00', 'Asia/Shanghai', ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO crawl_throttle_state(
                    id, domain, day_key, updated_at
                ) VALUES (1, 'search.ccgp.gov.cn', ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (self._beijing_day(now), now),
            )
            local_now = datetime.fromisoformat(now).astimezone(BEIJING_TZ)
            local_midnight = datetime.combine(
                local_now.date(), datetime.min.time(), tzinfo=BEIJING_TZ
            ).astimezone(timezone.utc).isoformat(timespec="seconds")
            audited_requests = connection.execute(
                """
                SELECT count(*) FROM crawl_page_runs
                WHERE started_at >= ?
                  AND NOT (
                    blocked=1 AND error_message LIKE '站点冷却中%'
                  )
                """,
                (local_midnight,),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE crawl_throttle_state
                SET day_request_count=MAX(day_request_count, ?), updated_at=?
                WHERE id=1 AND day_key=?
                """,
                (audited_requests, now, self._beijing_day(now)),
            )

    @staticmethod
    def _beijing_day(now: str | None = None) -> str:
        instant = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return instant.astimezone(BEIJING_TZ).date().isoformat()

    @staticmethod
    def _iso_after(now: str, seconds: float) -> str:
        instant = datetime.fromisoformat(now)
        return (instant + timedelta(seconds=seconds)).isoformat(timespec="seconds")

    def _normalize_throttle_day(self, connection: sqlite3.Connection, now: str) -> None:
        day_key = self._beijing_day(now)
        connection.execute(
            """
            UPDATE crawl_throttle_state
            SET day_key=?, day_request_count=0, batch_request_count=0, updated_at=?
            WHERE id=1 AND day_key != ?
            """,
            (day_key, now, day_key),
        )

    def throttle_status(self, now: str | None = None) -> dict[str, Any]:
        now = now or utc_now_iso()
        with self.connect() as connection:
            self._normalize_throttle_day(connection, now)
            row = connection.execute(
                "SELECT * FROM crawl_throttle_state WHERE id=1"
            ).fetchone()
            cooldown = connection.execute(
                """
                SELECT until_at FROM domain_cooldown
                WHERE domain=? AND until_at > ?
                """,
                ("search.ccgp.gov.cn", now),
            ).fetchone()
            if row is None:
                return {
                    "state": "ready",
                    "day_key": self._beijing_day(now),
                    "day_request_count": 0,
                    "daily_limit": DAILY_REQUEST_LIMIT,
                    "batch_request_count": 0,
                    "next_request_at": None,
                    "next_job_at": None,
                    "cooldown_until": cooldown[0] if cooldown else None,
                    "active_job_id": None,
                }
            data = dict(row)
            state = "ready"
            wait_until = None
            if cooldown:
                state = "cooldown"
                wait_until = cooldown[0]
            elif data["next_request_at"] and data["next_request_at"] > now:
                state = "waiting"
                wait_until = data["next_request_at"]
            elif data["next_job_at"] and data["next_job_at"] > now and data["active_job_id"] is None:
                state = "waiting"
                wait_until = data["next_job_at"]
            elif data["day_request_count"] >= DAILY_REQUEST_LIMIT and data["active_job_id"] is None:
                state = "daily_limit"
                local_day = datetime.fromisoformat(now).astimezone(BEIJING_TZ).date()
                next_day = datetime.combine(
                    local_day + timedelta(days=1), datetime.min.time(), tzinfo=BEIJING_TZ
                ).astimezone(timezone.utc)
                wait_until = next_day.isoformat(timespec="seconds")
            elif data["resume_required"] and data["active_job_id"] is None:
                state = "waiting"
            data.update(
                {
                    "state": state,
                    "daily_limit": DAILY_REQUEST_LIMIT,
                    "cooldown_until": cooldown[0] if cooldown else None,
                    "wait_until": wait_until,
                }
            )
            return data

    def claim_job_slot(self, job_id: str, now: str | None = None) -> tuple[bool, dict[str, Any]]:
        now = now or utc_now_iso()
        with self.connect() as connection:
            self._normalize_throttle_day(connection, now)
            row = connection.execute(
                "SELECT * FROM crawl_throttle_state WHERE id=1"
            ).fetchone()
            cooldown = connection.execute(
                """
                SELECT until_at FROM domain_cooldown
                WHERE domain=? AND until_at > ?
                """,
                ("search.ccgp.gov.cn", now),
            ).fetchone()
            if row is None:
                return False, {"state": "cooldown" if cooldown else "waiting"}
            if cooldown:
                return False, {"state": "cooldown", "cooldown_until": cooldown[0]}
            if row["resume_required"] and row["confirmed_job_id"] != job_id:
                return False, {
                    "state": "waiting",
                    "wait_until": None,
                    "resume_required": True,
                }
            if row["active_job_id"] and row["active_job_id"] != job_id:
                return False, {"state": "waiting", "wait_until": row["next_job_at"]}
            same_paused_job = row["pause_resume_job_id"] == job_id
            if (
                row["next_job_at"]
                and row["next_job_at"] > now
                and row["active_job_id"] is None
                and not same_paused_job
            ):
                return False, {"state": "waiting", "wait_until": row["next_job_at"]}
            if row["day_request_count"] >= DAILY_REQUEST_LIMIT:
                local_day = datetime.fromisoformat(now).astimezone(BEIJING_TZ).date()
                next_day = datetime.combine(
                    local_day + timedelta(days=1), datetime.min.time(), tzinfo=BEIJING_TZ
                ).astimezone(timezone.utc)
                return False, {
                    "state": "daily_limit",
                    "wait_until": next_day.isoformat(timespec="seconds"),
                }
            result = connection.execute(
                """
                UPDATE crawl_throttle_state
                SET active_job_id=?, resume_required=0, confirmed_job_id=NULL,
                    pause_resume_job_id=NULL, updated_at=?
                WHERE id=1 AND (active_job_id IS NULL OR active_job_id=?)
                """,
                (job_id, now, job_id),
            )
            return bool(result.rowcount), {"state": "ready"}

    def reserve_request(
        self,
        job_id: str,
        *,
        now: str,
        next_request_at: str,
        batch_next_request_at: str,
    ) -> tuple[bool, dict[str, Any]]:
        with self.connect() as connection:
            self._normalize_throttle_day(connection, now)
            row = connection.execute(
                "SELECT * FROM crawl_throttle_state WHERE id=1"
            ).fetchone()
            cooldown = connection.execute(
                """
                SELECT until_at FROM domain_cooldown
                WHERE domain=? AND until_at > ?
                """,
                ("search.ccgp.gov.cn", now),
            ).fetchone()
            if cooldown:
                return False, {"state": "cooldown", "cooldown_until": cooldown[0]}
            if row is None:
                return False, {"state": "waiting", "wait_until": None}
            if row["active_job_id"] is None:
                job = connection.execute(
                    "SELECT status FROM crawl_jobs WHERE id=?", (job_id,)
                ).fetchone()
                if not job or job["status"] != "running":
                    return False, {"state": "waiting", "wait_until": None}
                connection.execute(
                    "UPDATE crawl_throttle_state SET active_job_id=?, updated_at=? WHERE id=1",
                    (job_id, now),
                )
                row = connection.execute(
                    "SELECT * FROM crawl_throttle_state WHERE id=1"
                ).fetchone()
            if row["active_job_id"] != job_id:
                return False, {"state": "waiting", "wait_until": row["next_job_at"]}
            if row["next_request_at"] and row["next_request_at"] > now:
                return False, {"state": "waiting", "wait_until": row["next_request_at"]}
            next_batch_count = row["batch_request_count"] + 1
            connection.execute(
                """
                UPDATE crawl_throttle_state
                SET day_request_count=day_request_count+1,
                    batch_request_count=?, next_request_at=?, updated_at=?
                WHERE id=1 AND active_job_id=?
                """,
                (
                    0 if next_batch_count >= 20 else next_batch_count,
                    batch_next_request_at if next_batch_count >= 20 else next_request_at,
                    now,
                    job_id,
                ),
            )
            return True, {"state": "ready", "batch_completed": next_batch_count >= 20}

    def release_job_slot(
        self,
        job_id: str,
        *,
        next_job_at: str,
        allow_same_job_resume: bool = False,
    ) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_throttle_state
                SET active_job_id=NULL, next_job_at=?,
                    pause_resume_job_id=CASE WHEN ? THEN ? ELSE NULL END,
                    last_job_id=?,
                    updated_at=?
                WHERE id=1 AND active_job_id=?
                """,
                (
                    next_job_at,
                    int(allow_same_job_resume),
                    job_id,
                    job_id,
                    now,
                    job_id,
                ),
            )

    def require_manual_resume(self) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_throttle_state
                SET resume_required=1, confirmed_job_id=NULL, updated_at=?
                WHERE id=1
                """,
                (now,),
            )

    def confirm_job_start(self, job_id: str) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_throttle_state
                SET confirmed_job_id=?, updated_at=?
                WHERE id=1 AND resume_required=1
                """,
                (job_id, now),
            )

    def record_throttle_wait(self, job_id: str, message: str) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT current_title FROM crawl_jobs WHERE id=? AND status='queued'", (job_id,)
            ).fetchone()
            if row and row[0] != message:
                connection.execute(
                    "UPDATE crawl_jobs SET current_title=?, updated_at=? WHERE id=? AND status='queued'",
                    (message, now, job_id),
                )
                self._append_event(
                    connection,
                    job_id,
                    "throttle_waiting",
                    {"message": message},
                    now,
                )

    def recover_interrupted_jobs(self) -> int:
        now = utc_now_iso()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM crawl_jobs WHERE status IN ('running', 'stopping')"
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE crawl_jobs
                    SET status = 'stopped', error_message = ?, finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    ("服务重启，任务已安全暂停，可继续运行", now, now, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE crawl_slices SET status = 'stopped', updated_at = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (now, row["id"]),
                )
                self._append_event(
                    connection,
                    row["id"],
                    "stopped",
                    {"message": "服务重启，任务已安全暂停，可继续运行"},
                    now,
                )
            throttle = connection.execute(
                "SELECT active_job_id FROM crawl_throttle_state WHERE id=1"
            ).fetchone()
            active_job_id = throttle["active_job_id"] if throttle else None
            active_job = (
                connection.execute(
                    "SELECT status FROM crawl_jobs WHERE id=?", (active_job_id,)
                ).fetchone()
                if active_job_id
                else None
            )
            stale_slot = active_job_id and (
                active_job is None or active_job["status"] not in {"running", "stopping"}
            )
            if rows or stale_slot:
                next_job_at = self._iso_after(now, MIN_JOB_GAP_SECONDS)
                connection.execute(
                    """
                    UPDATE crawl_throttle_state
                    SET active_job_id=NULL,
                        pause_resume_job_id=NULL,
                        last_job_id=COALESCE(?, last_job_id),
                        next_job_at=CASE
                            WHEN next_job_at IS NULL OR next_job_at < ? THEN ?
                            ELSE next_job_at END,
                        updated_at=?
                    WHERE id=1
                    """,
                    (active_job_id, next_job_at, next_job_at, now),
                )
            return len(rows)

    def create_job(
        self,
        start: date,
        end: date,
        *,
        job_type: str = "manual",
        schedule_key: str | None = None,
        scheduled_for: date | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if job_type not in {"manual", "scheduled"}:
            raise ValueError("invalid job type")
        days = inclusive_dates(start, end)
        now = utc_now_iso()
        job_id = uuid.uuid4().hex
        with self.connect() as connection:
            if schedule_key:
                existing = connection.execute(
                    "SELECT * FROM crawl_jobs WHERE schedule_key = ?", (schedule_key,)
                ).fetchone()
                if existing:
                    return self._job_dict(connection, existing), False
            connection.execute(
                """
                INSERT INTO crawl_jobs(
                    id, job_type, schedule_key, scheduled_for, start_date, end_date,
                    status, dates_total, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    job_id,
                    job_type,
                    schedule_key,
                    scheduled_for.isoformat() if scheduled_for else None,
                    start.isoformat(),
                    end.isoformat(),
                    len(days),
                    now,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO crawl_slices(job_id, slice_date, status, next_page, updated_at)
                VALUES (?, ?, 'queued', 1, ?)
                """,
                [(job_id, day.isoformat(), now) for day in days],
            )
            self._append_event(
                connection,
                job_id,
                "queued",
                {"message": "任务已加入单任务队列"},
                now,
            )
            row = connection.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
            return self._job_dict(connection, row), True

    def list_jobs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM crawl_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._job_dict(connection, row, include_slices=False) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
            return self._job_dict(connection, row) if row else None

    def delete_failed_job(self, job_id: str) -> bool:
        """Delete failed-job bookkeeping while preserving collected notices."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM crawl_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return False
            if row["status"] != "failed":
                raise ValueError("只能删除失败任务")
            # crawl_page_runs predates jobs and has no foreign key, so it must
            # be cleaned explicitly. Other job-owned rows cascade on delete.
            connection.execute("DELETE FROM crawl_page_runs WHERE job_id = ?", (job_id,))
            connection.execute("DELETE FROM crawl_jobs WHERE id = ?", (job_id,))
            return True

    def next_queued_job(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM crawl_jobs WHERE status = 'queued'
                ORDER BY CASE
                    WHEN id=(SELECT pause_resume_job_id FROM crawl_throttle_state WHERE id=1) THEN 0
                    WHEN id=(SELECT confirmed_job_id FROM crawl_throttle_state WHERE id=1) THEN 1
                    ELSE 2 END,
                    created_at LIMIT 1
                """
            ).fetchone()
            return self._job_dict(connection, row) if row else None

    def start_job(self, job_id: str) -> bool:
        now = utc_now_iso()
        with self.connect() as connection:
            active = connection.execute(
                "SELECT id FROM crawl_jobs WHERE status IN ('running', 'stopping') AND id != ?",
                (job_id,),
            ).fetchone()
            if active:
                return False
            result = connection.execute(
                """
                UPDATE crawl_jobs SET status = 'running', started_at = COALESCE(started_at, ?),
                    finished_at = NULL, current_title = NULL, error_message = NULL, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now, now, job_id),
            )
            if not result.rowcount:
                return False
            connection.execute(
                """
                UPDATE crawl_slices SET status = 'queued', error_message = NULL, updated_at = ?
                WHERE job_id = ? AND status IN ('stopped', 'blocked', 'failed')
                """,
                (now, job_id),
            )
            self._append_event(connection, job_id, "running", {"message": "任务开始运行"}, now)
            return True

    def request_stop(self, job_id: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self.connect() as connection:
            row = connection.execute("SELECT status FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return None
            if row["status"] == "queued":
                connection.execute(
                    """
                    UPDATE crawl_jobs
                    SET status='stopped', current_title=NULL, finished_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (now, now, job_id),
                )
                connection.execute(
                    "UPDATE crawl_slices SET status='stopped', updated_at=? WHERE job_id=? AND status='queued'",
                    (now, job_id),
                )
                connection.execute(
                    """
                    UPDATE crawl_throttle_state
                    SET pause_resume_job_id=?, updated_at=?
                    WHERE id=1 AND last_job_id=?
                    """,
                    (job_id, now, job_id),
                )
                event_type = "stopped"
            elif row["status"] == "running":
                connection.execute(
                    "UPDATE crawl_jobs SET status='stopping', updated_at=? WHERE id=?",
                    (now, job_id),
                )
                event_type = "stopping"
            else:
                return self._job_dict(
                    connection,
                    connection.execute("SELECT * FROM crawl_jobs WHERE id=?", (job_id,)).fetchone(),
                )
            self._append_event(connection, job_id, event_type, {"message": "已请求停止"}, now)
            updated = connection.execute("SELECT * FROM crawl_jobs WHERE id=?", (job_id,)).fetchone()
            return self._job_dict(connection, updated)

    def resume_job(self, job_id: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE crawl_jobs SET status='queued', finished_at=NULL, error_message=NULL, updated_at=?
                WHERE id=? AND status IN ('stopped', 'blocked', 'failed')
                """,
                (now, job_id),
            )
            if result.rowcount:
                connection.execute(
                    """
                    UPDATE crawl_slices SET status='queued', error_message=NULL, updated_at=?
                    WHERE job_id=? AND status IN ('stopped', 'blocked', 'failed')
                    """,
                    (now, job_id),
                )
                self._append_event(
                    connection, job_id, "queued", {"message": "任务已从断点重新入队"}, now
                )
            row = connection.execute("SELECT * FROM crawl_jobs WHERE id=?", (job_id,)).fetchone()
            return self._job_dict(connection, row) if row else None

    def next_slice(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM crawl_slices
                WHERE job_id=? AND status != 'completed'
                ORDER BY slice_date LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            return dict(row) if row else None

    def mark_slice_running(self, job_id: str, slice_date: str) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                "UPDATE crawl_slices SET status='running', updated_at=? WHERE job_id=? AND slice_date=?",
                (now, job_id, slice_date),
            )
            connection.execute(
                "UPDATE crawl_jobs SET current_date=?, updated_at=? WHERE id=?",
                (slice_date, now, job_id),
            )

    def record_page_retry(
        self,
        *,
        job_id: str,
        slice_date: str,
        page_no: int,
        retry_attempt: int,
        max_retries: int,
        delay_seconds: int,
        message: str,
    ) -> bool:
        now = utc_now_iso()
        payload = {
            "slice_date": slice_date,
            "page": page_no,
            "retry_attempt": retry_attempt,
            "max_retries": max_retries,
            "delay_seconds": delay_seconds,
            "message": message,
        }
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE crawl_jobs SET current_date=?, current_page=?, current_title=?, error_message=NULL,
                    updated_at=? WHERE id=? AND status='running'
                """,
                (slice_date, page_no, message, now, job_id),
            )
            if not result.rowcount:
                return False
            self._append_event(connection, job_id, "page_retrying", payload, now)
            return True

    def job_status(self, job_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT status FROM crawl_jobs WHERE id=?", (job_id,)).fetchone()
            return str(row[0]) if row else None

    def commit_page(
        self,
        *,
        job_id: str,
        slice_date: str,
        page_no: int,
        page_url: str,
        notices: list[Notice],
        total_pages: int,
        listing_count: int,
        parse_error_count: int,
        filtered_count: int,
        elapsed_ms: int,
        current_title: str | None,
    ) -> dict[str, int | bool]:
        now = utc_now_iso()
        effective_total = max(1, total_pages)
        with self.connect() as connection:
            urls = [notice.canonical_url for notice in notices]
            existing_urls = {
                row[0]
                for row in connection.execute(
                    "SELECT canonical_url FROM pending_notices WHERE canonical_url IN ({})".format(
                        ",".join("?" for _ in urls)
                    ),
                    urls,
                ).fetchall()
            } if urls else set()
            new_count, duplicate_count = save_notices(connection, notices, now=now)
            connection.executemany(
                """
                INSERT INTO notice_job_links(job_id, canonical_url, is_new, seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, canonical_url) DO UPDATE SET seen_at=excluded.seen_at
                """,
                [
                    (job_id, notice.canonical_url, int(notice.canonical_url not in existing_urls), now)
                    for notice in notices
                ],
            )
            connection.execute(
                """
                INSERT INTO crawl_page_runs(
                    category, page_no, page_url, http_status, row_count, new_count,
                    duplicate_count, parse_error_count, filtered_count, blocked,
                    elapsed_ms, started_at, finished_at, job_id, slice_date
                ) VALUES ('search', ?, ?, 200, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    page_no,
                    page_url,
                    listing_count,
                    new_count,
                    duplicate_count,
                    parse_error_count,
                    filtered_count,
                    elapsed_ms,
                    now,
                    now,
                    job_id,
                    slice_date,
                ),
            )
            slice_row = connection.execute(
                "SELECT total_pages FROM crawl_slices WHERE job_id=? AND slice_date=?",
                (job_id, slice_date),
            ).fetchone()
            learned_pages = slice_row["total_pages"] is None
            completed = page_no >= effective_total
            connection.execute(
                """
                UPDATE crawl_slices SET total_pages=?, next_page=?, pages_completed=pages_completed+1,
                    status=?, error_message=NULL, updated_at=?
                WHERE job_id=? AND slice_date=?
                """,
                (
                    effective_total,
                    page_no + 1,
                    "completed" if completed else "running",
                    now,
                    job_id,
                    slice_date,
                ),
            )
            connection.execute(
                """
                UPDATE crawl_jobs SET current_date=?, current_page=?, current_total_pages=?,
                    current_title=?, pages_total=pages_total+?, pages_completed=pages_completed+1,
                    dates_completed=dates_completed+?, new_count=new_count+?,
                    duplicate_count=duplicate_count+?, parse_error_count=parse_error_count+?,
                    filtered_count=filtered_count+?, updated_at=?
                WHERE id=?
                """,
                (
                    slice_date,
                    page_no,
                    effective_total,
                    current_title,
                    effective_total if learned_pages else 0,
                    int(completed),
                    new_count,
                    duplicate_count,
                    parse_error_count,
                    filtered_count,
                    now,
                    job_id,
                ),
            )
            job = connection.execute("SELECT * FROM crawl_jobs WHERE id=?", (job_id,)).fetchone()
            payload = {
                "slice_date": slice_date,
                "page": page_no,
                "total_pages": effective_total,
                "new_count": new_count,
                "duplicate_count": duplicate_count,
                "parse_error_count": parse_error_count,
                "current_title": current_title,
                "job": self._job_dict(connection, job),
            }
            self._append_event(connection, job_id, "page_completed", payload, now)
            return {
                "new_count": new_count,
                "duplicate_count": duplicate_count,
                "completed": completed,
            }

    def fail_page(
        self,
        job_id: str,
        slice_date: str,
        page_no: int,
        page_url: str,
        message: str,
        *,
        blocked: bool = False,
    ) -> None:
        now = utc_now_iso()
        state = "blocked" if blocked else "failed"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO crawl_page_runs(
                    category, page_no, page_url, row_count, blocked, error_message,
                    started_at, finished_at, job_id, slice_date
                ) VALUES ('search', ?, ?, 0, ?, ?, ?, ?, ?, ?)
                """,
                (page_no, page_url, int(blocked), message, now, now, job_id, slice_date),
            )
            connection.execute(
                "UPDATE crawl_slices SET status=?, error_message=?, updated_at=? WHERE job_id=? AND slice_date=?",
                (state, message, now, job_id, slice_date),
            )
            connection.execute(
                """
                UPDATE crawl_jobs SET status=?, error_message=?, finished_at=?, updated_at=?
                WHERE id=?
                """,
                (state, message, now, now, job_id),
            )
            self._append_event(connection, job_id, state, {"message": message}, now)

    def mark_stopped(self, job_id: str) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_slices SET status='stopped', updated_at=?
                WHERE job_id=? AND status='running'
                """,
                (now, job_id),
            )
            connection.execute(
                """
                UPDATE crawl_jobs SET status='stopped', finished_at=?, updated_at=?
                WHERE id=? AND status='stopping'
                """,
                (now, now, job_id),
            )
            self._append_event(
                connection, job_id, "stopped", {"message": "当前页已提交，任务已停止"}, now
            )

    def complete_job(self, job_id: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_jobs SET status='completed', current_title=NULL,
                    finished_at=?, updated_at=? WHERE id=?
                """,
                (now, now, job_id),
            )
            job = connection.execute("SELECT * FROM crawl_jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                return None
            if job["job_type"] == "scheduled" and job["scheduled_for"]:
                connection.execute(
                    """
                    UPDATE schedule_settings SET last_success_date = CASE
                        WHEN last_success_date IS NULL OR last_success_date < ? THEN ?
                        ELSE last_success_date END, updated_at=? WHERE id=1
                    """,
                    (job["scheduled_for"], job["scheduled_for"], now),
                )
            data = self._job_dict(connection, job)
            self._append_event(connection, job_id, "completed", {"job": data}, now)
            return data

    def list_events(self, job_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, event_type, payload_json, created_at FROM crawl_events
                WHERE job_id=? AND id>? ORDER BY id LIMIT 200
                """,
                (job_id, after_id),
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def recent_notices(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT n.canonical_url, n.title, n.published_at, n.region_raw,
                    n.region_code, n.category, n.source_list_url, l.is_new, l.seen_at
                FROM notice_job_links l
                JOIN pending_notices n ON n.canonical_url = l.canonical_url
                WHERE l.job_id=? ORDER BY l.seen_at DESC, n.published_at DESC LIMIT ?
                """,
                (job_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_schedule(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM schedule_settings WHERE id=1").fetchone()
            return dict(row)

    def update_schedule(self, enabled: bool) -> dict[str, Any]:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                "UPDATE schedule_settings SET enabled=?, updated_at=? WHERE id=1",
                (int(enabled), now),
            )
        return self.get_schedule()

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO crawl_events(job_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (job_id, event_type, json.dumps(payload, ensure_ascii=False), created_at),
        )

    def _job_dict(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_slices: bool = True,
    ) -> dict[str, Any]:
        data = dict(row)
        if include_slices:
            data["slices"] = [
                dict(item)
                for item in connection.execute(
                    "SELECT * FROM crawl_slices WHERE job_id=? ORDER BY slice_date", (row["id"],)
                ).fetchall()
            ]
        return data
