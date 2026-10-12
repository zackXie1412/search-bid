from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .local_store import (
    initialize_database,
    open_database,
    retry_failed_syncs,
    sync_statistics,
    utc_now_iso,
)
from .sync_client import SyncError, sync_pending
from .sync_client import ingest_api_key, recover_stale_syncs


@dataclass(slots=True)
class SyncRun:
    status: str = "idle"
    current_batch_key: str | None = None
    current_batch_size: int = 0
    current_batch_completed: int = 0
    batches_completed: int = 0
    synced: int = 0
    inserted: int = 0
    updated: int = 0
    replayed_batches: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    last_error: str | None = None


@dataclass(slots=True)
class SyncManager:
    database: Path
    _run: SyncRun = field(default_factory=SyncRun)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _starting: bool = False

    def _endpoint(self) -> str:
        import os

        return os.getenv("CCGP_INGEST_URL", "").rstrip("/")

    def _cloud_status(self) -> dict[str, str | None]:
        endpoint = self._endpoint()
        if not endpoint:
            return {"state": "not_configured", "message": "未配置云端写入地址"}
        try:
            api_key = ingest_api_key()
        except OSError:
            return {"state": "not_configured", "message": "无法读取本地 API Key 文件"}
        if not api_key:
            return {"state": "not_configured", "message": "未配置本地 API Key 文件"}
        try:
            response = httpx.get(f"{endpoint}/healthz", timeout=3.0)
        except httpx.RequestError:
            return {"state": "unavailable", "message": "SSH 隧道未连接或云端服务不可达"}
        if response.status_code == 200:
            try:
                auth_response = httpx.get(
                    f"{endpoint}/internal/v1/healthz",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=3.0,
                )
            except httpx.RequestError:
                return {"state": "unavailable", "message": "SSH 隧道未连接或云端服务不可达"}
            if auth_response.status_code in {401, 403}:
                return {"state": "unauthorized", "message": "云端 API 鉴权失败"}
            if auth_response.status_code not in {200, 404}:
                return {
                    "state": "error",
                    "message": f"云端服务异常（HTTP {auth_response.status_code}）",
                }
            message = "云端写入 API 已连接"
            if auth_response.status_code == 404:
                message += "（鉴权将在上传时校验）"
            return {"state": "connected", "message": message}
        if response.status_code in {401, 403}:
            return {"state": "unauthorized", "message": "云端 API 鉴权失败"}
        return {"state": "error", "message": f"云端服务异常（HTTP {response.status_code}）"}

    def _statistics(self) -> dict[str, object]:
        with open_database(self.database) as connection:
            initialize_database(connection)
            return sync_statistics(connection)

    def status(self) -> dict[str, object]:
        with self._lock:
            run = asdict(self._run)
        statistics = self._statistics()
        return {
            "connection": self._cloud_status(),
            "counts": statistics,
            "run": run,
        }

    def initialize(self) -> int:
        """Recover batches left in-flight by a previous process."""

        with open_database(self.database) as connection:
            initialize_database(connection)
            return recover_stale_syncs(connection)

    def start(self) -> dict[str, object]:
        with self._lock:
            if self._starting or (self._thread and self._thread.is_alive()):
                raise RuntimeError("同步任务已经在运行")
            self._starting = True
        try:
            statistics = self._statistics()
            if int(statistics["pending"]) == 0:
                raise ValueError("没有待上传的本地数据")
            cloud = self._cloud_status()
            if cloud["state"] != "connected":
                raise ConnectionError(str(cloud["message"]))
            now = utc_now_iso()
            with self._lock:
                self._run = SyncRun(status="running", started_at=now)
                self._stop_event.clear()
                self._thread = threading.Thread(
                    target=self._worker,
                    name="ccgp-sync-worker",
                    daemon=True,
                )
                self._thread.start()
        finally:
            with self._lock:
                self._starting = False
        return self.status()

    def retry_failed(self) -> int:
        with open_database(self.database) as connection:
            initialize_database(connection)
            return retry_failed_syncs(connection)

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)

    def _progress(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._run.current_batch_key = event.get("batch_key")
            self._run.current_batch_size = int(event.get("batch_size", 0))
            self._run.current_batch_completed = (
                self._run.current_batch_size if event["event"] == "batch_completed" else 0
            )
            self._run.batches_completed = int(event.get("batches_completed", 0))
            self._run.synced = int(event.get("synced", 0))
            self._run.inserted = int(event.get("inserted", 0))
            self._run.updated = int(event.get("updated", 0))
            self._run.replayed_batches = int(event.get("replayed_batches", 0))

    def _worker(self) -> None:
        try:
            summary = sync_pending(self.database, on_progress=self._progress)
        except Exception as exc:
            with self._lock:
                self._run.status = "failed"
                self._run.last_error = str(exc)
                self._run.finished_at = utc_now_iso()
            return
        with self._lock:
            self._run.status = "completed"
            self._run.finished_at = utc_now_iso()
            self._run.current_batch_completed = self._run.current_batch_size
            self._run.batches_completed = summary.batches
            self._run.synced = summary.synced
            self._run.inserted = summary.inserted
            self._run.updated = summary.updated
            self._run.replayed_batches = summary.replayed_batches
