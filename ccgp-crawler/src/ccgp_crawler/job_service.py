from __future__ import annotations

import logging
import os
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .job_store import JobStore
from .local_store import record_soft_block, utc_now_iso
from .notifications import send_native_notification
from .normalize import SHANGHAI_TZ
from .search_source import (
    SearchPageError,
    SearchSoftBlockError,
    build_search_url,
    parse_search_page,
)


SEARCH_DOMAIN = "search.ccgp.gov.cn"
NAVIGATION_TIMEOUT_MS = 60_000
NAVIGATION_RETRY_DELAYS = (10.0, 30.0)
PAGE_DELAY_RANGE = (15.0, 25.0)
BATCH_DELAY_RANGE = (180.0, 300.0)
JOB_DELAY_RANGE = (900.0, 1200.0)
WORKER_ERROR_BACKOFF_SECONDS = 2.0


logger = logging.getLogger(__name__)


class SearchNavigationTimeout(SearchPageError):
    """The search page did not begin returning a response before the deadline."""


class SearchThrottlePaused(SearchPageError):
    """A running task reached a persisted site cooldown before navigation."""


def _beijing_time(value: str | None) -> str:
    if not value:
        return "未知时间"
    return datetime.fromisoformat(value).astimezone(SHANGHAI_TZ).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class FetchResult:
    html: str
    http_status: int


class SearchAdapter(Protocol):
    @property
    def running(self) -> bool: ...

    def fetch(self, url: str, slice_date: date, page_index: int) -> FetchResult: ...

    def close(self) -> None: ...


class PlaywrightSearchAdapter:
    def __init__(self, profile_dir: Path):
        self.profile_dir = profile_dir
        self._playwright = None
        self._context = None
        self._page = None

    @property
    def running(self) -> bool:
        if self._context is None or self._page is None:
            return False
        try:
            return not self._page.is_closed()
        except Exception:
            return False

    @staticmethod
    def _target_was_closed(exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            "target page, context or browser has been closed" in message
            or "targetclosederror" in message
            or "browser has been closed" in message
        )

    def _discard_context(self) -> None:
        context = self._context
        self._context = None
        self._page = None
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    def _ensure_browser(self) -> None:
        if self._context is not None:
            try:
                open_pages = [page for page in self._context.pages if not page.is_closed()]
                if self._page in open_pages:
                    return
                self._page = open_pages[0] if open_pages else self._context.new_page()
                return
            except Exception:
                self._discard_context()
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=False,
            channel="chrome",
            viewport={"width": 1360, "height": 900},
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def fetch(self, url: str, slice_date: date, page_index: int) -> FetchResult:
        for attempt in range(2):
            self._ensure_browser()
            page = self._page
            try:
                try:
                    response = page.goto(
                        url,
                        wait_until="commit",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    )
                except PlaywrightTimeoutError as exc:
                    raise SearchNavigationTimeout(
                        f"第 {page_index} 页等待页面响应超过 60 秒"
                    ) from exc
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=20_000)
                except Exception as exc:
                    if self._target_was_closed(exc):
                        raise
                    # CCGP occasionally leaves a document request open. The parser still
                    # validates the result count and rows before any data is committed.
                    pass
                page.wait_for_timeout(1_000)
                status = response.status if response else 0
                return FetchResult(page.content(), status)
            except SearchNavigationTimeout:
                raise
            except Exception as exc:
                if not self._target_was_closed(exc):
                    raise
                self._discard_context()
                if attempt == 1:
                    raise SearchPageError(
                        "采集浏览器已被关闭且自动重开失败，"
                        "请在页面中点击“重新打开并继续”"
                    ) from exc
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._discard_context()
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None


class FakeSearchAdapter:
    """Deterministic two-page source for tests and local UI demonstrations."""

    def __init__(self):
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def fetch(self, url: str, slice_date: date, page_index: int) -> FetchResult:
        self._running = True
        total = 24
        count = 20 if page_index == 1 else 4
        rows: list[str] = []
        for offset in range(count):
            number = (page_index - 1) * 20 + offset + 1
            region = ["北京市", "广东省", "四川省", "江苏省"][number % 4]
            category = "zygg" if number % 3 == 0 else "dfgg"
            rows.append(
                "<li>"
                f'<a href="https://www.ccgp.gov.cn/cggg/{category}/gghtt/{slice_date:%Y%m}/t_fake_{slice_date:%Y%m%d}_{number}.htm" '
                f'title="{region}示例采购项目 {number}">{region}示例采购项目 {number}</a>'
                f"<span>{slice_date:%Y.%m.%d} {9 + number % 8:02d}:15:00</span>"
                "<strong>公开招标公告</strong> | "
                f"{region}</li>"
            )
        html = (
            "<!doctype html><html><body>"
            f"<div>共找到 {total} 条</div>"
            '<ul class="vT-srch-result-list-bid">'
            + "".join(rows)
            + "</ul></body></html>"
        )
        time.sleep(float(os.getenv("CCGP_FAKE_PAGE_DELAY", "0.18")))
        return FetchResult(html, 200)

    def close(self) -> None:
        self._running = False


class JobManager:
    def __init__(self, store: JobStore, runtime_dir: Path):
        self.store = store
        self.runtime_dir = runtime_dir
        self.fake = os.getenv("CCGP_FAKE_CRAWLER", "0") == "1"
        self.adapter: SearchAdapter = (
            FakeSearchAdapter()
            if self.fake
            else PlaywrightSearchAdapter(runtime_dir / "chrome-profile")
        )
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._service_enabled = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def chrome_running(self) -> bool:
        return self.adapter.running

    def start(self) -> None:
        with self._lifecycle_lock:
            self._service_enabled = True
            if self.running:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._worker,
                name="ccgp-crawl-worker",
                daemon=True,
            )
            self._thread.start()

    def wake(self) -> None:
        self._wake_event.set()
        if self._service_enabled and not self.running:
            self.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._service_enabled = False
            self._stop_event.set()
            self._wake_event.set()
            thread = self._thread
        if thread:
            thread.join(timeout=10)
        self.adapter.close()

    def _worker(self) -> None:
        active_job_id: str | None = None
        try:
            while not self._stop_event.is_set():
                try:
                    if active_job_id is not None:
                        self._run_job(active_job_id)
                        active_job_id = None
                        continue

                    job = self.store.next_queued_job()
                    if job is None:
                        self._wake_event.wait(1.0)
                        self._wake_event.clear()
                        continue
                    claimed, throttle = self.store.claim_job_slot(job["id"])
                    if not claimed:
                        self.store.record_throttle_wait(
                            job["id"], self._throttle_message(throttle)
                        )
                        self._wake_event.wait(1.0)
                        self._wake_event.clear()
                        continue
                    if not self.store.start_job(job["id"]):
                        self._release_job_slot(job["id"])
                        time.sleep(0.2)
                        continue
                    active_job_id = job["id"]
                    self._run_job(active_job_id)
                    active_job_id = None
                except Exception:
                    logger.exception(
                        "CCGP crawl worker iteration failed; retrying in %.1f seconds",
                        WORKER_ERROR_BACKOFF_SECONDS,
                    )
                    self._wake_event.clear()
                    self._stop_event.wait(WORKER_ERROR_BACKOFF_SECONDS)
        finally:
            self.adapter.close()

    def _run_job(self, job_id: str) -> None:
        previous_signature: tuple[str, ...] | None = None
        previous_slice: str | None = None
        while not self._stop_event.is_set():
            if self.store.job_status(job_id) == "stopping":
                self.store.mark_stopped(job_id)
                self._release_job_slot(job_id, allow_same_job_resume=True)
                return
            slice_row = self.store.next_slice(job_id)
            if slice_row is None:
                job = self.store.complete_job(job_id)
                if job:
                    send_native_notification(
                        "CCGP 采集完成",
                        f"{job['start_date']} 至 {job['end_date']}：新增 {job['new_count']} 条，重复 {job['duplicate_count']} 条",
                    )
                self._release_job_slot(job_id)
                return

            slice_text = slice_row["slice_date"]
            slice_day = date.fromisoformat(slice_text)
            page_no = int(slice_row["next_page"])
            self.store.mark_slice_running(job_id, slice_text)
            page_url = build_search_url(slice_day, page_no)
            started = time.monotonic()
            try:
                fetched = self._fetch_with_navigation_retries(
                    job_id=job_id,
                    slice_text=slice_text,
                    slice_day=slice_day,
                    page_no=page_no,
                    page_url=page_url,
                )
                if fetched is None:
                    if self.store.job_status(job_id) == "stopping":
                        self.store.mark_stopped(job_id)
                        self._release_job_slot(job_id, allow_same_job_resume=True)
                    return
                if not 200 <= fetched.http_status < 300:
                    raise SearchPageError(f"搜索页 HTTP 状态异常：{fetched.http_status}")
                parsed = parse_search_page(
                    page_url,
                    fetched.html,
                    expected_date=slice_day,
                )
                if page_no > 1 and parsed.total_records == 0:
                    raise SearchPageError("非首分页异常返回 0 条结果，已保留当前断点")
                signature = tuple(notice.canonical_url for notice in parsed.notices)
                if (
                    page_no > 1
                    and previous_slice == slice_text
                    and previous_signature
                    and signature
                    and signature == previous_signature
                ):
                    raise SearchPageError("检测到重复分页内容，已停止以避免错误翻页")
                previous_signature = signature
                previous_slice = slice_text
                elapsed_ms = int((time.monotonic() - started) * 1000)
                title_summary = "；".join(notice.title for notice in parsed.notices[:2]) or "本页无结果"
                self.store.commit_page(
                    job_id=job_id,
                    slice_date=slice_text,
                    page_no=page_no,
                    page_url=page_url,
                    notices=parsed.notices,
                    total_pages=parsed.total_pages,
                    listing_count=parsed.listing_count,
                    parse_error_count=parsed.parse_error_count,
                    filtered_count=parsed.filtered_count,
                    elapsed_ms=elapsed_ms,
                    current_title=title_summary,
                )
                if self.store.job_status(job_id) == "stopping":
                    self.store.mark_stopped(job_id)
                    self._release_job_slot(job_id, allow_same_job_resume=True)
                    return
            except SearchSoftBlockError as exc:
                with self.store.connect() as connection:
                    until = record_soft_block(
                        connection, SEARCH_DOMAIN, str(exc), utc_now_iso()
                    )
                message = f"检测到频繁访问，已暂停至北京时间 {_beijing_time(until)}：{exc}"
                self.store.fail_page(
                    job_id, slice_text, page_no, page_url, message, blocked=True
                )
                self.store.require_manual_resume()
                send_native_notification("CCGP 采集已暂停", message)
                self._release_job_slot(job_id)
                return
            except SearchThrottlePaused as exc:
                message = str(exc)
                self.store.fail_page(
                    job_id, slice_text, page_no, page_url, message, blocked=True
                )
                self.store.require_manual_resume()
                send_native_notification("CCGP 采集仍在冷却", message)
                self._release_job_slot(job_id)
                return
            except sqlite3.Error:
                # A local database interruption must not become a crawl failure or
                # create a new site-level job boundary. The worker retries it.
                raise
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                self.store.fail_page(job_id, slice_text, page_no, page_url, message)
                send_native_notification("CCGP 采集失败", message)
                self._release_job_slot(job_id)
                return

    def _fetch_with_navigation_retries(
        self,
        *,
        job_id: str,
        slice_text: str,
        slice_day: date,
        page_no: int,
        page_url: str,
    ) -> FetchResult | None:
        max_retries = len(NAVIGATION_RETRY_DELAYS)
        for attempt_index in range(max_retries + 1):
            if not self._wait_for_request_slot(job_id):
                return None
            try:
                return self.adapter.fetch(page_url, slice_day, page_no)
            except SearchNavigationTimeout as exc:
                if attempt_index == max_retries:
                    if self.store.job_status(job_id) == "stopping":
                        return None
                    raise SearchPageError(
                        f"{slice_text} 第 {page_no} 页连续 3 次未收到页面响应，"
                        "自动重试 2 次仍失败"
                    ) from exc

                retry_attempt = attempt_index + 1
                delay_seconds = NAVIGATION_RETRY_DELAYS[attempt_index]
                delay_label = int(delay_seconds)
                message = (
                    f"第 {page_no} 页未收到响应，{delay_label} 秒后进行"
                    f"第 {retry_attempt}/{max_retries} 次自动重试"
                )
                recorded = self.store.record_page_retry(
                    job_id=job_id,
                    slice_date=slice_text,
                    page_no=page_no,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    delay_seconds=delay_label,
                    message=message,
                )
                if not recorded or not self._interruptible_retry_wait(
                    job_id, delay_seconds
                ):
                    return None
        raise AssertionError("unreachable")

    def _interruptible_retry_wait(self, job_id: str, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self._stop_event.is_set():
            if self.store.job_status(job_id) == "stopping":
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.2, remaining))
        return False

    @staticmethod
    def _throttle_message(throttle: dict) -> str:
        state = throttle.get("state")
        until = throttle.get("cooldown_until") or throttle.get("wait_until")
        if state == "cooldown":
            return f"站点冷却中，请在北京时间 {_beijing_time(until)} 后手动继续"
        if state == "daily_limit":
            return f"今日请求额度已用尽，请在北京时间 {_beijing_time(until)} 后继续"
        return f"等待全局节流，预计北京时间 {_beijing_time(until)} 后可运行" if until else "等待全局节流"

    def _wait_for_request_slot(self, job_id: str) -> bool:
        if self.fake:
            return True
        while not self._stop_event.is_set():
            if self.store.job_status(job_id) == "stopping":
                return False
            now = utc_now_iso()
            delay = random.uniform(*PAGE_DELAY_RANGE)
            next_request = (
                datetime.fromisoformat(now) + timedelta(seconds=delay)
            ).isoformat(timespec="seconds")
            batch_delay = random.uniform(*BATCH_DELAY_RANGE)
            batch_next_request = (
                datetime.fromisoformat(now) + timedelta(seconds=batch_delay)
            ).isoformat(timespec="seconds")
            reserved, throttle = self.store.reserve_request(
                job_id,
                now=now,
                next_request_at=next_request,
                batch_next_request_at=batch_next_request,
            )
            if reserved:
                return True
            if throttle.get("state") == "cooldown":
                raise SearchThrottlePaused(
                    f"站点冷却中，请在北京时间 {_beijing_time(throttle.get('cooldown_until'))} 后手动继续"
                )
            wait_until = throttle.get("wait_until")
            if not wait_until:
                raise SearchPageError("全局访问闸门状态异常，已保留当前断点")
            remaining = (
                datetime.fromisoformat(wait_until) - datetime.fromisoformat(now)
            ).total_seconds()
            if remaining <= 0:
                continue
            if not self._interruptible_wait(job_id, min(remaining, 1.0)):
                return False
        return False

    def _release_job_slot(
        self, job_id: str, *, allow_same_job_resume: bool = False
    ) -> None:
        now = utc_now_iso()
        delay = random.uniform(*JOB_DELAY_RANGE)
        next_job_at = (
            datetime.fromisoformat(now) + timedelta(seconds=delay)
        ).isoformat(timespec="seconds")
        self.store.release_job_slot(
            job_id,
            next_job_at=next_job_at,
            allow_same_job_resume=allow_same_job_resume,
        )

    def _interruptible_wait(self, job_id: str, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self._stop_event.is_set():
            if self.store.job_status(job_id) == "stopping":
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.2, remaining))
        return False
