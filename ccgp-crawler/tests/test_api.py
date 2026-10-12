from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from ccgp_crawler.api import create_app
from ccgp_crawler.local_store import record_soft_block


def test_job_api_and_future_date_validation(tmp_path):
    app = create_app(
        database_path=tmp_path / "api.sqlite3",
        runtime_dir=tmp_path / "runtime",
        start_background_services=False,
    )
    with TestClient(app) as client:
        today = date.today().isoformat()
        response = client.post("/api/jobs", json={"start_date": today, "end_date": today})
        assert response.status_code == 201
        job = response.json()
        assert job["status"] == "queued"
        assert len(job["slices"]) == 1
        assert client.get(f"/api/jobs/{job['id']}").status_code == 200
        assert client.get("/api/system").json()["worker"] == "stopped"

        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        invalid = client.post("/api/jobs", json={"start_date": today, "end_date": tomorrow})
        assert invalid.status_code == 422


def test_sse_events_preserve_order(tmp_path):
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        runtime_dir=tmp_path / "runtime",
        start_background_services=False,
    )
    store = app.state.store
    job, _ = store.create_job(date.today(), date.today())
    events = store.list_events(job["id"])
    assert [event["event_type"] for event in events] == ["queued"]
    assert events[0]["id"] > 0


def test_only_failed_jobs_can_be_deleted(tmp_path):
    app = create_app(
        database_path=tmp_path / "delete.sqlite3",
        runtime_dir=tmp_path / "runtime",
        start_background_services=False,
    )
    store = app.state.store
    queued, _ = store.create_job(date.today(), date.today())
    failed, _ = store.create_job(date.today(), date.today())
    store.start_job(failed["id"])
    store.mark_slice_running(failed["id"], date.today().isoformat())
    store.fail_page(
        failed["id"], date.today().isoformat(), 1,
        "https://search.ccgp.gov.cn/bxsearch", "测试失败",
    )

    with TestClient(app) as client:
        assert client.delete(f"/api/jobs/{queued['id']}").status_code == 409
        assert client.delete(f"/api/jobs/{failed['id']}").status_code == 204
        assert client.get(f"/api/jobs/{failed['id']}").status_code == 404


def test_system_exposes_throttle_and_cooldown_rejects_resume(tmp_path):
    app = create_app(
        database_path=tmp_path / "cooldown.sqlite3",
        runtime_dir=tmp_path / "runtime",
        start_background_services=False,
    )
    store = app.state.store
    job, _ = store.create_job(date.today(), date.today())
    store.start_job(job["id"])
    store.mark_slice_running(job["id"], date.today().isoformat())
    store.fail_page(
        job["id"], date.today().isoformat(), 1,
        "https://search.ccgp.gov.cn/bxsearch", "访问受限", blocked=True,
    )
    with store.connect() as connection:
        record_soft_block(
            connection,
            "search.ccgp.gov.cn",
            "soft block",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    with TestClient(app) as client:
        system = client.get("/api/system").json()
        assert system["throttle"]["daily_limit"] == 240
        assert system["throttle"]["state"] == "cooldown"
        response = client.post(f"/api/jobs/{job['id']}/resume")
        assert response.status_code == 409
        assert "冷却" in response.json()["detail"]
