from __future__ import annotations

import os
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .job_service import JobManager
from .job_store import JobStore, inclusive_dates


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class IncrementalScheduler:
    def __init__(self, store: JobStore, manager: JobManager):
        self.store = store
        self.manager = manager
        self.scheduler = BackgroundScheduler(timezone=BEIJING_TZ)

    def start(self) -> None:
        self._apply_setting()
        self.scheduler.start()
        setting = self.store.get_schedule()
        if setting["enabled"] and os.getenv("CCGP_SKIP_STARTUP_CATCHUP") != "1":
            self.enqueue_catch_up()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def update_enabled(self, enabled: bool) -> dict:
        self.store.update_schedule(enabled)
        self._apply_setting()
        if enabled:
            self.enqueue_catch_up()
        return self.status()

    def _apply_setting(self) -> None:
        setting = self.store.get_schedule()
        if setting["enabled"]:
            self.scheduler.add_job(
                self.enqueue_today,
                CronTrigger(hour=2, minute=0, timezone=BEIJING_TZ),
                id="ccgp-daily-incremental",
                replace_existing=True,
                coalesce=True,
                misfire_grace_time=24 * 60 * 60,
            )
        else:
            try:
                self.scheduler.remove_job("ccgp-daily-incremental")
            except Exception:
                pass

    def enqueue_today(self) -> None:
        self.enqueue_for(self._today())

    @staticmethod
    def _today() -> date:
        from datetime import datetime

        return datetime.now(BEIJING_TZ).date()

    def enqueue_for(self, scheduled_day: date) -> bool:
        _, created = self.store.create_job(
            scheduled_day - timedelta(days=1),
            scheduled_day,
            job_type="scheduled",
            schedule_key=f"daily:{scheduled_day.isoformat()}",
            scheduled_for=scheduled_day,
        )
        if created:
            self.manager.wake()
        return created

    def enqueue_catch_up(self) -> int:
        setting = self.store.get_schedule()
        today = self._today()
        last = (
            date.fromisoformat(setting["last_success_date"])
            if setting.get("last_success_date")
            else None
        )
        scheduled_days = inclusive_dates(last + timedelta(days=1), today) if last and last < today else [today]
        return sum(int(self.enqueue_for(day)) for day in scheduled_days)

    def status(self) -> dict:
        setting = self.store.get_schedule()
        job = self.scheduler.get_job("ccgp-daily-incremental") if self.scheduler.running else None
        return {
            **setting,
            "enabled": bool(setting["enabled"]),
            "next_run_at": job.next_run_time.isoformat() if job and job.next_run_time else None,
        }
