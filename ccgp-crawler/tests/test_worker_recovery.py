import sqlite3
import threading

from ccgp_crawler.job_service import JobManager


class RecoveringStore:
    def __init__(self):
        self.calls = 0

    def next_queued_job(self):
        self.calls += 1
        if self.calls == 1:
            raise sqlite3.OperationalError("unable to open database file")
        if self.calls == 2:
            return {"id": "job-1"}
        return None

    def claim_job_slot(self, _job_id: str):
        return True, {}

    def start_job(self, _job_id: str):
        return True


class IdleStore:
    def next_queued_job(self):
        return None


def test_worker_survives_transient_database_error_and_processes_job(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CCGP_FAKE_CRAWLER", "1")
    monkeypatch.setattr(
        "ccgp_crawler.job_service.WORKER_ERROR_BACKOFF_SECONDS", 0.01
    )
    store = RecoveringStore()
    manager = JobManager(store, tmp_path)
    processed = threading.Event()

    def run_job(job_id: str):
        assert job_id == "job-1"
        processed.set()
        manager._stop_event.set()

    monkeypatch.setattr(manager, "_run_job", run_job)

    manager.start()
    assert processed.wait(1.0)
    assert store.calls >= 2
    if manager._thread:
        manager._thread.join(timeout=1.0)


def test_wake_restarts_dead_worker_without_creating_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("CCGP_FAKE_CRAWLER", "1")
    manager = JobManager(IdleStore(), tmp_path)
    dead_thread = threading.Thread(target=lambda: None)
    dead_thread.start()
    dead_thread.join()
    manager._thread = dead_thread
    manager._service_enabled = True

    manager.wake()
    restarted_thread = manager._thread

    assert restarted_thread is not dead_thread
    assert manager.running
    manager.wake()
    assert manager._thread is restarted_thread
    manager.stop()
