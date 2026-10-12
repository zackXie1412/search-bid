from __future__ import annotations

from datetime import date, datetime

from ccgp_crawler.job_service import FetchResult, JobManager
from ccgp_crawler.job_store import JobStore


SLICE_DAY = date(2026, 6, 4)


class SinglePageAdapter:
    def __init__(self, result: FetchResult):
        self.result = result
        self.calls = 0

    @property
    def running(self) -> bool:
        return True

    def fetch(self, _url: str, _slice_date: date, _page_index: int) -> FetchResult:
        self.calls += 1
        return self.result

    def close(self) -> None:
        pass


def setup_manager(tmp_path, *, end_day: date = SLICE_DAY):
    store = JobStore(tmp_path / "throttling.sqlite3")
    store.initialize()
    job, _ = store.create_job(SLICE_DAY, end_day)
    assert store.start_job(job["id"])
    manager = JobManager(store, tmp_path / "runtime")
    manager.fake = False
    return store, job["id"], manager


def test_request_reservation_uses_fifteen_to_twenty_five_second_delay(
    tmp_path, monkeypatch
):
    store, job_id, manager = setup_manager(tmp_path)
    fixed_now = "2026-09-22T02:00:00+00:00"
    ranges: list[tuple[float, float]] = []

    def choose(low: float, high: float) -> float:
        ranges.append((low, high))
        return 20.0 if (low, high) == (15.0, 25.0) else 240.0

    monkeypatch.setattr("ccgp_crawler.job_service.utc_now_iso", lambda: fixed_now)
    monkeypatch.setattr(
        "ccgp_crawler.job_service.random.uniform",
        choose,
    )

    assert manager._wait_for_request_slot(job_id)
    throttle = store.throttle_status(fixed_now)
    assert throttle["day_request_count"] == 1
    assert throttle["batch_request_count"] == 1
    assert throttle["next_request_at"] == "2026-09-22T02:00:20+00:00"
    assert ranges == [(15.0, 25.0), (180.0, 300.0)]


def test_twentieth_request_persists_three_to_five_minute_batch_break(
    tmp_path, monkeypatch
):
    store, job_id, manager = setup_manager(tmp_path)
    fixed_now = "2026-09-22T02:00:00+00:00"
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE crawl_throttle_state
            SET active_job_id=?, batch_request_count=19, next_request_at=NULL,
                day_key='2026-09-22'
            WHERE id=1
            """,
            (job_id,),
        )
    monkeypatch.setattr("ccgp_crawler.job_service.utc_now_iso", lambda: fixed_now)
    monkeypatch.setattr(
        "ccgp_crawler.job_service.random.uniform",
        lambda low, high: 240.0 if (low, high) == (180.0, 300.0) else 20.0,
    )

    assert manager._wait_for_request_slot(job_id)
    throttle = store.throttle_status(fixed_now)
    assert throttle["batch_request_count"] == 0
    assert throttle["next_request_at"] == "2026-09-22T02:04:00+00:00"


def test_job_release_persists_fifteen_to_twenty_minute_gap(tmp_path, monkeypatch):
    store, job_id, manager = setup_manager(tmp_path)
    fixed_now = "2026-09-22T02:00:00+00:00"
    assert store.claim_job_slot(job_id, fixed_now)[0]
    monkeypatch.setattr("ccgp_crawler.job_service.utc_now_iso", lambda: fixed_now)
    monkeypatch.setattr(
        "ccgp_crawler.job_service.random.uniform",
        lambda low, high: 1_050.0,
    )

    manager._release_job_slot(job_id)
    throttle = store.throttle_status(fixed_now)
    assert throttle["active_job_id"] is None
    assert throttle["next_job_at"] == "2026-09-22T02:17:30+00:00"
    assert throttle["state"] == "waiting"


def test_same_manually_paused_job_bypasses_job_gap_but_keeps_page_gap(tmp_path):
    store, job_id, _manager = setup_manager(tmp_path)
    stopped_at = "2026-09-22T02:00:00+00:00"
    next_request_at = "2026-09-22T02:00:20+00:00"
    next_job_at = "2026-09-22T02:17:30+00:00"
    assert store.claim_job_slot(job_id, stopped_at)[0]
    with store.connect() as connection:
        connection.execute(
            "UPDATE crawl_throttle_state SET next_request_at=? WHERE id=1",
            (next_request_at,),
        )
    store.request_stop(job_id)
    store.mark_stopped(job_id)
    store.release_job_slot(
        job_id,
        next_job_at=next_job_at,
        allow_same_job_resume=True,
    )
    store.resume_job(job_id)

    claimed, _ = store.claim_job_slot(job_id, "2026-09-22T02:00:01+00:00")
    assert claimed
    reserved, throttle = store.reserve_request(
        job_id,
        now="2026-09-22T02:00:01+00:00",
        next_request_at="2026-09-22T02:00:21+00:00",
        batch_next_request_at="2026-09-22T02:04:01+00:00",
    )
    assert not reserved
    assert throttle["wait_until"] == next_request_at


def test_manual_pause_bypass_does_not_apply_to_another_job(tmp_path):
    store = JobStore(tmp_path / "switch.sqlite3")
    store.initialize()
    other, _ = store.create_job(SLICE_DAY, SLICE_DAY)
    paused, _ = store.create_job(SLICE_DAY, SLICE_DAY)
    assert store.start_job(paused["id"])
    stopped_at = "2026-09-22T02:00:00+00:00"
    next_job_at = "2026-09-22T02:17:30+00:00"
    assert store.claim_job_slot(paused["id"], stopped_at)[0]
    store.request_stop(paused["id"])
    store.mark_stopped(paused["id"])
    store.release_job_slot(
        paused["id"],
        next_job_at=next_job_at,
        allow_same_job_resume=True,
    )
    store.resume_job(paused["id"])

    assert store.next_queued_job()["id"] == paused["id"]
    claimed_other, reason = store.claim_job_slot(
        other["id"], "2026-09-22T02:00:01+00:00"
    )
    assert not claimed_other
    assert reason["wait_until"] == next_job_at
    assert store.claim_job_slot(
        paused["id"], "2026-09-22T02:00:01+00:00"
    )[0]


def test_failed_job_resume_still_waits_for_job_gap(tmp_path):
    store, job_id, _manager = setup_manager(tmp_path)
    stopped_at = "2026-09-22T02:00:00+00:00"
    next_job_at = "2026-09-22T02:17:30+00:00"
    assert store.claim_job_slot(job_id, stopped_at)[0]
    store.fail_page(
        job_id,
        SLICE_DAY.isoformat(),
        1,
        "https://search.ccgp.gov.cn/bxsearch",
        "测试失败",
    )
    store.release_job_slot(job_id, next_job_at=next_job_at)
    store.resume_job(job_id)

    claimed, reason = store.claim_job_slot(job_id, "2026-09-22T02:00:01+00:00")
    assert not claimed
    assert reason["wait_until"] == next_job_at


def test_stopping_same_queued_job_converts_its_own_gap_to_pause_resume(tmp_path):
    store, job_id, _manager = setup_manager(tmp_path)
    stopped_at = "2026-09-22T02:00:00+00:00"
    next_job_at = "2026-09-22T02:17:30+00:00"
    assert store.claim_job_slot(job_id, stopped_at)[0]
    store.fail_page(
        job_id,
        SLICE_DAY.isoformat(),
        1,
        "https://search.ccgp.gov.cn/bxsearch",
        "测试失败",
    )
    store.release_job_slot(job_id, next_job_at=next_job_at)
    store.resume_job(job_id)

    store.request_stop(job_id)
    assert store.throttle_status(stopped_at)["pause_resume_job_id"] == job_id
    store.resume_job(job_id)

    assert store.claim_job_slot(job_id, "2026-09-22T02:00:01+00:00")[0]


def test_stopping_other_queued_job_cannot_bypass_previous_job_gap(tmp_path):
    store, job_id, _manager = setup_manager(tmp_path)
    other, _ = store.create_job(SLICE_DAY, SLICE_DAY)
    stopped_at = "2026-09-22T02:00:00+00:00"
    next_job_at = "2026-09-22T02:17:30+00:00"
    assert store.claim_job_slot(job_id, stopped_at)[0]
    store.release_job_slot(job_id, next_job_at=next_job_at)

    store.request_stop(other["id"])
    store.resume_job(other["id"])

    claimed, reason = store.claim_job_slot(
        other["id"], "2026-09-22T02:00:01+00:00"
    )
    assert not claimed
    assert reason["wait_until"] == next_job_at


def test_daily_limit_blocks_new_job_but_not_active_job_requests(tmp_path):
    store, job_id, _manager = setup_manager(tmp_path)
    fixed_now = "2026-09-22T02:00:00+00:00"
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE crawl_throttle_state
            SET day_key='2026-09-22', day_request_count=240,
                active_job_id=NULL, next_job_at=NULL, next_request_at=NULL
            WHERE id=1
            """
        )
    claimed, reason = store.claim_job_slot(job_id, fixed_now)
    assert not claimed
    assert reason["state"] == "daily_limit"

    with store.connect() as connection:
        connection.execute(
            "UPDATE crawl_throttle_state SET active_job_id=? WHERE id=1", (job_id,)
        )
    reserved, _ = store.reserve_request(
        job_id,
        now=fixed_now,
        next_request_at="2026-09-22T02:00:20+00:00",
        batch_next_request_at="2026-09-22T02:04:00+00:00",
    )
    assert reserved
    assert store.throttle_status(fixed_now)["day_request_count"] == 241


def test_stop_during_page_delay_preserves_next_page_checkpoint(tmp_path, monkeypatch):
    store, job_id, manager = setup_manager(tmp_path)
    html = (
        "<!doctype html><html><body>"
        "<div>共找到 21 条</div>"
        '<ul class="vT-srch-result-list-bid"><li>'
        '<a href="https://www.ccgp.gov.cn/cggg/dfgg/gkzb/202606/t_throttle.htm" '
        'title="节流测试公开招标公告">节流测试公开招标公告</a>'
        "<span>2026.06.04 10:00:00</span>"
        "<strong>公开招标公告</strong> | 北京市"
        "</li></ul></body></html>"
    )
    adapter = SinglePageAdapter(FetchResult(html, 200))
    manager.adapter = adapter

    def stop_during_wait(waiting_job_id: str, _seconds: float) -> bool:
        store.request_stop(waiting_job_id)
        return False

    monkeypatch.setattr(manager, "_interruptible_wait", stop_during_wait)
    monkeypatch.setattr(
        "ccgp_crawler.job_service.send_native_notification", lambda *_args: None
    )

    manager._run_job(job_id)

    job = store.get_job(job_id)
    assert job["status"] == "stopped"
    assert job["slices"][0]["next_page"] == 2
    assert adapter.calls == 1
