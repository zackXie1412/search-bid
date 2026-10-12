import sqlite3
from datetime import date

from ccgp_crawler.job_store import JobStore, inclusive_dates
from ccgp_crawler.models import Notice


def make_store(tmp_path):
    store = JobStore(tmp_path / "test.sqlite3")
    store.initialize()
    return store


def test_date_slices_are_inclusive():
    assert inclusive_dates(date(2026, 9, 18), date(2026, 9, 20)) == [
        date(2026, 9, 18), date(2026, 9, 19), date(2026, 9, 20)
    ]


def test_job_queue_is_single_active_and_resume_keeps_checkpoint(tmp_path):
    store = make_store(tmp_path)
    first, _ = store.create_job(date(2026, 9, 18), date(2026, 9, 19))
    second, _ = store.create_job(date(2026, 9, 20), date(2026, 9, 20))
    assert store.start_job(first["id"])
    assert not store.start_job(second["id"])
    store.request_stop(first["id"])
    store.mark_stopped(first["id"])
    stopped = store.get_job(first["id"])
    assert stopped["status"] == "stopped"
    assert stopped["slices"][0]["next_page"] == 1
    resumed = store.resume_job(first["id"])
    assert resumed["status"] == "queued"
    assert resumed["slices"][0]["next_page"] == 1


def test_start_job_clears_stale_queue_message(tmp_path):
    store = make_store(tmp_path)
    job, _ = store.create_job(date(2026, 9, 20), date(2026, 9, 20))
    store.record_throttle_wait(job["id"], "站点冷却中")
    assert store.get_job(job["id"])["current_title"] == "站点冷却中"

    assert store.start_job(job["id"])

    assert store.get_job(job["id"])["current_title"] is None


def test_database_connection_retries_transient_open_failure(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    from ccgp_crawler import job_store as job_store_module

    original_open = job_store_module.open_database
    calls = 0
    sleeps: list[float] = []

    def flaky_open(path):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise sqlite3.OperationalError("unable to open database file")
        return original_open(path)

    monkeypatch.setattr(job_store_module, "open_database", flaky_open)
    monkeypatch.setattr(job_store_module.time, "sleep", sleeps.append)

    with store.connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    assert calls == 3
    assert sleeps == [0.1, 0.3]


def test_database_is_initialized_in_wal_mode_without_per_connection_reset(tmp_path):
    store = make_store(tmp_path)
    with store.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_schedule_key_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    first, created = store.create_job(
        date(2026, 9, 19), date(2026, 9, 20),
        job_type="scheduled", schedule_key="daily:2026-09-20", scheduled_for=date(2026, 9, 20),
    )
    second, duplicate_created = store.create_job(
        date(2026, 9, 19), date(2026, 9, 20),
        job_type="scheduled", schedule_key="daily:2026-09-20", scheduled_for=date(2026, 9, 20),
    )
    assert created is True
    assert duplicate_created is False
    assert first["id"] == second["id"]


def test_restart_moves_running_job_to_resumable_stopped_state(tmp_path):
    store = make_store(tmp_path)
    job, _ = store.create_job(date(2026, 9, 20), date(2026, 9, 20))
    store.start_job(job["id"])
    assert store.recover_interrupted_jobs() == 1
    recovered = store.get_job(job["id"])
    assert recovered["status"] == "stopped"
    assert "服务重启" in recovered["error_message"]
    throttle = store.throttle_status()
    assert throttle["active_job_id"] is None
    assert throttle["next_job_at"] is not None


def test_throttle_state_survives_store_reinitialization(tmp_path):
    path = tmp_path / "persistent.sqlite3"
    store = JobStore(path)
    store.initialize()
    job, _ = store.create_job(date(2026, 9, 22), date(2026, 9, 22))
    assert store.claim_job_slot(job["id"], "2026-09-22T02:00:00+00:00")[0]
    assert store.reserve_request(
        job["id"],
        now="2026-09-22T02:00:00+00:00",
        next_request_at="2026-09-22T02:00:20+00:00",
        batch_next_request_at="2026-09-22T02:04:00+00:00",
    )[0]

    reopened = JobStore(path)
    reopened.initialize()
    throttle = reopened.throttle_status("2026-09-22T02:00:01+00:00")
    assert throttle["day_request_count"] == 1
    assert throttle["next_request_at"] == "2026-09-22T02:00:20+00:00"
    assert throttle["active_job_id"] == job["id"]


def test_restart_clears_stale_throttle_job_slot(tmp_path):
    store = make_store(tmp_path)
    job, _ = store.create_job(date(2026, 9, 22), date(2026, 9, 22))
    assert store.claim_job_slot(job["id"], "2026-09-22T02:00:00+00:00")[0]
    assert store.start_job(job["id"])
    store.complete_job(job["id"])

    assert store.recover_interrupted_jobs() == 0
    throttle = store.throttle_status()
    assert throttle["active_job_id"] is None
    assert throttle["next_job_at"] is not None


def test_soft_block_preserves_the_current_page_checkpoint(tmp_path):
    store = make_store(tmp_path)
    job, _ = store.create_job(date(2026, 9, 20), date(2026, 9, 20))
    store.start_job(job["id"])
    store.mark_slice_running(job["id"], "2026-09-20")
    store.fail_page(
        job["id"], "2026-09-20", 1, "https://search.ccgp.gov.cn/bxsearch",
        "访问过于频繁", blocked=True,
    )
    blocked = store.get_job(job["id"])
    assert blocked["status"] == "blocked"
    assert blocked["slices"][0]["next_page"] == 1


def test_page_retry_event_updates_visible_activity_without_moving_checkpoint(tmp_path):
    store = make_store(tmp_path)
    job, _ = store.create_job(date(2026, 9, 20), date(2026, 9, 20))
    store.start_job(job["id"])
    store.mark_slice_running(job["id"], "2026-09-20")

    assert store.record_page_retry(
        job_id=job["id"],
        slice_date="2026-09-20",
        page_no=29,
        retry_attempt=1,
        max_retries=2,
        delay_seconds=10,
        message="第 29 页未收到响应，10 秒后进行第 1/2 次自动重试",
    )

    retrying = store.get_job(job["id"])
    assert retrying["status"] == "running"
    assert retrying["current_page"] == 29
    assert retrying["current_title"] == "第 29 页未收到响应，10 秒后进行第 1/2 次自动重试"
    assert retrying["slices"][0]["next_page"] == 1
    event = [item for item in store.list_events(job["id"]) if item["event_type"] == "page_retrying"][0]
    assert event["payload"] == {
        "slice_date": "2026-09-20",
        "page": 29,
        "retry_attempt": 1,
        "max_retries": 2,
        "delay_seconds": 10,
        "message": "第 29 页未收到响应，10 秒后进行第 1/2 次自动重试",
    }


def test_delete_failed_job_removes_history_but_preserves_notices(tmp_path):
    store = make_store(tmp_path)
    job, _ = store.create_job(date(2026, 9, 20), date(2026, 9, 20))
    store.start_job(job["id"])
    store.mark_slice_running(job["id"], "2026-09-20")
    notice = Notice(
        canonical_url="https://www.ccgp.gov.cn/cggg/dfgg/gkzb/example.htm",
        title="已采集公告",
        published_at="2026-09-20T10:00:00+08:00",
        region_raw="北京",
        region_code="110000",
        category="dfgg",
        source_list_url="https://search.ccgp.gov.cn/bxsearch",
    )
    store.commit_page(
        job_id=job["id"], slice_date="2026-09-20", page_no=1,
        page_url=notice.source_list_url, notices=[notice], total_pages=2,
        listing_count=1, parse_error_count=0, filtered_count=0,
        elapsed_ms=10, current_title=notice.title,
    )
    store.fail_page(
        job["id"], "2026-09-20", 2, notice.source_list_url, "测试失败"
    )

    assert store.delete_failed_job(job["id"]) is True
    assert store.get_job(job["id"]) is None
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM pending_notices WHERE canonical_url=?",
            (notice.canonical_url,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM crawl_page_runs WHERE job_id=?", (job["id"],)
        ).fetchone()[0] == 0


def test_delete_failed_job_rejects_non_failed_jobs(tmp_path):
    store = make_store(tmp_path)
    job, _ = store.create_job(date(2026, 9, 20), date(2026, 9, 20))
    try:
        store.delete_failed_job(job["id"])
    except ValueError as exc:
        assert str(exc) == "只能删除失败任务"
    else:
        raise AssertionError("queued job deletion should be rejected")


def test_old_database_is_migrated_without_losing_notices(tmp_path):
    from ccgp_crawler.local_store import initialize_database, open_database

    path = tmp_path / "legacy.sqlite3"
    with open_database(path) as connection:
        initialize_database(connection)
        connection.execute(
            """
            INSERT INTO pending_notices(
                canonical_url,title,published_at,category,source_list_url,
                created_at,updated_at
            ) VALUES ('https://example.test/1','旧公告','2026-09-19T00:00:00+08:00',
                'dfgg','https://example.test/list','now','now')
            """
        )
    store = JobStore(path)
    store.initialize()
    with store.connect() as connection:
        assert connection.execute("SELECT title FROM pending_notices").fetchone()[0] == "旧公告"
        columns = {row[1] for row in connection.execute("PRAGMA table_info(crawl_page_runs)")}
        assert {"job_id", "slice_date"} <= columns
