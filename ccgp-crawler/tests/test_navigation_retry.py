from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from ccgp_crawler.job_service import (
    FetchResult,
    JobManager,
    SearchNavigationTimeout,
)
from ccgp_crawler.job_store import JobStore


SLICE_DAY = date(2026, 6, 4)


class SequenceAdapter:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, date, int]] = []

    @property
    def running(self) -> bool:
        return True

    def fetch(self, url: str, slice_date: date, page_index: int) -> FetchResult:
        self.calls.append((url, slice_date, page_index))
        if not self.outcomes:
            raise AssertionError("unexpected fetch")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        pass


def result_page(day: date = SLICE_DAY, *, status: int = 200) -> FetchResult:
    html = (
        "<!doctype html><html><body>"
        "<div>共找到 1 条</div>"
        '<ul class="vT-srch-result-list-bid"><li>'
        f'<a href="https://www.ccgp.gov.cn/cggg/dfgg/gkzb/{day:%Y%m}/t_test.htm" '
        'title="测试公开招标公告">测试公开招标公告</a>'
        f"<span>{day:%Y.%m.%d} 10:00:00</span>"
        "<strong>公开招标公告</strong> | 北京市"
        "</li></ul></body></html>"
    )
    return FetchResult(html, status)


def setup_manager(tmp_path, outcomes):
    store = JobStore(tmp_path / "retry.sqlite3")
    store.initialize()
    job, _ = store.create_job(SLICE_DAY, SLICE_DAY)
    assert store.start_job(job["id"])
    manager = JobManager(store, tmp_path / "runtime")
    adapter = SequenceAdapter(outcomes)
    manager.adapter = adapter
    manager.fake = True
    return store, job["id"], manager, adapter


@pytest.mark.parametrize(
    ("timeouts", "expected_waits"),
    [
        (1, [10.0]),
        (2, [10.0, 30.0]),
    ],
)
def test_navigation_timeout_retries_then_commits_page_once(
    tmp_path, monkeypatch, timeouts, expected_waits
):
    outcomes = [SearchNavigationTimeout("temporary timeout") for _ in range(timeouts)]
    outcomes.append(result_page())
    store, job_id, manager, adapter = setup_manager(tmp_path, outcomes)
    waits: list[float] = []
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        manager,
        "_interruptible_retry_wait",
        lambda _job_id, seconds: waits.append(seconds) or True,
    )
    monkeypatch.setattr(
        "ccgp_crawler.job_service.send_native_notification",
        lambda title, message: notifications.append((title, message)),
    )

    manager._run_job(job_id)

    job = store.get_job(job_id)
    assert job["status"] == "completed"
    assert job["pages_completed"] == 1
    assert job["new_count"] == 1
    assert job["slices"][0]["next_page"] == 2
    assert len(adapter.calls) == timeouts + 1
    assert waits == expected_waits
    retry_events = [
        event for event in store.list_events(job_id)
        if event["event_type"] == "page_retrying"
    ]
    assert [event["payload"]["retry_attempt"] for event in retry_events] == list(
        range(1, timeouts + 1)
    )
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM crawl_page_runs WHERE job_id=?", (job_id,)
        ).fetchone()[0] == 1
    assert [title for title, _ in notifications] == ["CCGP 采集完成"]


def test_navigation_timeout_retries_each_consume_a_request_slot(
    tmp_path, monkeypatch
):
    store, job_id, manager, adapter = setup_manager(
        tmp_path,
        [SearchNavigationTimeout("temporary timeout"), result_page()],
    )
    manager.fake = False
    reservations = 0

    def reserve(_job_id: str) -> bool:
        nonlocal reservations
        reservations += 1
        return True

    monkeypatch.setattr(manager, "_wait_for_request_slot", reserve)
    monkeypatch.setattr(manager, "_interruptible_retry_wait", lambda *_args: True)
    monkeypatch.setattr(manager, "_release_job_slot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "ccgp_crawler.job_service.send_native_notification", lambda *_args: None
    )

    manager._run_job(job_id)

    assert len(adapter.calls) == 2
    assert reservations == 2
    assert store.get_job(job_id)["status"] == "completed"


def test_transient_sqlite_error_does_not_fail_or_release_running_job(
    tmp_path, monkeypatch
):
    store, job_id, manager, adapter = setup_manager(tmp_path, [result_page()])
    monkeypatch.setattr(
        store,
        "commit_page",
        lambda **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("unable to open database file")
        ),
    )
    monkeypatch.setattr(
        "ccgp_crawler.job_service.send_native_notification", lambda *_args: None
    )

    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        manager._run_job(job_id)

    assert adapter.calls
    assert store.get_job(job_id)["status"] == "running"
    assert not any(
        event["event_type"] == "failed" for event in store.list_events(job_id)
    )


def test_navigation_timeout_exhaustion_fails_once_and_preserves_checkpoint(
    tmp_path, monkeypatch
):
    store, job_id, manager, adapter = setup_manager(
        tmp_path,
        [SearchNavigationTimeout("temporary timeout") for _ in range(3)],
    )
    waits: list[float] = []
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        manager,
        "_interruptible_retry_wait",
        lambda _job_id, seconds: waits.append(seconds) or True,
    )
    monkeypatch.setattr(
        "ccgp_crawler.job_service.send_native_notification",
        lambda title, message: notifications.append((title, message)),
    )

    manager._run_job(job_id)

    job = store.get_job(job_id)
    assert job["status"] == "failed"
    assert job["slices"][0]["next_page"] == 1
    assert "自动重试 2 次仍失败" in job["error_message"]
    assert len(adapter.calls) == 3
    assert waits == [10.0, 30.0]
    assert [title for title, _ in notifications] == ["CCGP 采集失败"]


def test_stopping_during_retry_backoff_prevents_next_request(tmp_path, monkeypatch):
    store, job_id, manager, adapter = setup_manager(
        tmp_path,
        [SearchNavigationTimeout("temporary timeout"), result_page()],
    )

    def stop_during_wait(waiting_job_id: str, _seconds: float) -> bool:
        store.request_stop(waiting_job_id)
        return False

    monkeypatch.setattr(manager, "_interruptible_retry_wait", stop_during_wait)
    monkeypatch.setattr(
        "ccgp_crawler.job_service.send_native_notification", lambda *_args: None
    )

    manager._run_job(job_id)

    job = store.get_job(job_id)
    assert job["status"] == "stopped"
    assert job["slices"][0]["next_page"] == 1
    assert len(adapter.calls) == 1


@pytest.mark.parametrize(
    "outcome",
    [
        RuntimeError("ordinary browser error"),
        result_page(status=503),
        result_page(SLICE_DAY + timedelta(days=1)),
        FetchResult("您的访问过于频繁，请稍后再试", 200),
    ],
)
def test_non_navigation_failures_are_not_retried(tmp_path, monkeypatch, outcome):
    store, job_id, manager, adapter = setup_manager(tmp_path, [outcome])
    waits: list[float] = []
    monkeypatch.setattr(
        manager,
        "_interruptible_retry_wait",
        lambda _job_id, seconds: waits.append(seconds) or True,
    )
    monkeypatch.setattr(
        "ccgp_crawler.job_service.send_native_notification", lambda *_args: None
    )

    manager._run_job(job_id)

    assert store.get_job(job_id)["status"] in {"failed", "blocked"}
    assert len(adapter.calls) == 1
    assert waits == []
    assert not any(
        event["event_type"] == "page_retrying"
        for event in store.list_events(job_id)
    )
