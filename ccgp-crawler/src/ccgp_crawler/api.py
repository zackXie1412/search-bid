from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from .ocr_api import register_ocr_routes
from .ocr_config import OCRConfigStore
from .ai_api import register_ai_routes
from .ai_config import AIConfigStore
from .bid_skills import BidSkillRegistry
from .bid_models import BidCheckError
from .bid_documents import ALLOWED_EXTENSIONS, MAX_FILE_BYTES, MAX_TOTAL_BYTES
from .bid_service import BidCheckManager
from .bid_store import BidTaskStore
from .knowledge_store import KnowledgeStore
from .knowledge_service import KnowledgeManager
from .knowledge_api import register_knowledge_routes
from .job_service import JobManager
from .job_store import JobStore, TERMINAL_STATUSES
from .notice_query import NoticeQueryParams, NoticeQueryResponse, query_notices
from .scheduler import IncrementalScheduler
from .sync_service import SyncManager


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class CreateJobRequest(BaseModel):
    start_date: date
    end_date: date


class ScheduleRequest(BaseModel):
    enabled: bool


def _beijing_iso(value: str | None) -> str | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(BEIJING_TZ).isoformat(timespec="seconds")


def _public_throttle(throttle: dict) -> dict:
    data = dict(throttle)
    for key in (
        "next_request_at",
        "next_job_at",
        "cooldown_until",
        "wait_until",
        "updated_at",
    ):
        data[key] = _beijing_iso(data.get(key))
    return data


def create_app(
    *,
    database_path: str | Path | None = None,
    runtime_dir: str | Path | None = None,
    start_background_services: bool = True,
) -> FastAPI:
    runtime = Path(runtime_dir or os.getenv("CCGP_RUNTIME_DIR", PROJECT_ROOT / "runtime"))
    database = Path(database_path or os.getenv("CCGP_DATABASE_PATH", runtime / "ccgp.sqlite3"))
    store = JobStore(database)
    store.initialize()
    manager = JobManager(store, runtime)
    scheduler = IncrementalScheduler(store, manager)
    sync_manager = SyncManager(database)
    sync_manager.initialize()
    bid_store = BidTaskStore(runtime)
    bid_store.initialize()
    knowledge_store = KnowledgeStore(runtime)
    knowledge_store.initialize()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.recover_interrupted_jobs()
        if start_background_services:
            manager.start()
            scheduler.start()
            bid_manager.start()
            knowledge_manager.start()
        yield
        if start_background_services:
            scheduler.stop()
            manager.stop()
            bid_manager.stop()
            knowledge_manager.stop()
        sync_manager.stop()

    app = FastAPI(
        title="CCGP 可视化采集控制台",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.manager = manager
    app.state.scheduler = scheduler
    app.state.sync_manager = sync_manager
    app.state.bid_store = bid_store
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/system")
    def system_status() -> dict:
        try:
            with store.connect() as connection:
                connection.execute("SELECT 1").fetchone()
            sqlite_ok = True
        except Exception:
            sqlite_ok = False
        return {
            "service": "online",
            "worker": "running" if manager.running else "stopped",
            "sqlite": "connected" if sqlite_ok else "error",
            "chrome": "running" if manager.chrome_running else "ready",
            "source": "fake" if manager.fake else "search.ccgp.gov.cn",
            "database_path": str(database),
            "throttle": _public_throttle(store.throttle_status()),
        }

    @app.post("/api/jobs", status_code=201)
    def create_job(payload: CreateJobRequest) -> dict:
        today = datetime.now(BEIJING_TZ).date()
        if payload.start_date > payload.end_date:
            raise HTTPException(422, "开始日期不得晚于结束日期")
        if payload.end_date > today:
            raise HTTPException(422, "结束日期不得晚于北京时间今天")
        job, _ = store.create_job(payload.start_date, payload.end_date)
        throttle = store.throttle_status()
        if throttle["state"] != "cooldown":
            store.confirm_job_start(job["id"])
        manager.wake()
        return job

    @app.get("/api/jobs")
    def list_jobs(limit: int = Query(30, ge=1, le=100)) -> dict:
        return {"items": store.list_jobs(limit)}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        return job

    @app.delete("/api/jobs/{job_id}", status_code=204)
    def delete_job(job_id: str) -> Response:
        try:
            deleted = store.delete_failed_job(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not deleted:
            raise HTTPException(404, "任务不存在")
        return Response(status_code=204)

    @app.post("/api/jobs/{job_id}/stop")
    def stop_job(job_id: str) -> dict:
        job = store.request_stop(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        manager.wake()
        return job

    @app.post("/api/jobs/{job_id}/resume")
    def resume_job(job_id: str) -> dict:
        prior = store.get_job(job_id)
        if not prior:
            raise HTTPException(404, "任务不存在")
        if prior["status"] not in {"stopped", "blocked", "failed"}:
            raise HTTPException(409, "只有已停止、受限或失败的任务可以继续")
        throttle = store.throttle_status()
        if throttle["state"] == "cooldown":
            raise HTTPException(
                409,
                f"站点仍在冷却，请在北京时间 {_beijing_iso(throttle['cooldown_until'])} 后手动继续",
            )
        job = store.resume_job(job_id)
        store.confirm_job_start(job_id)
        manager.wake()
        return job

    @app.get("/api/jobs/{job_id}/notices")
    def get_notices(job_id: str, limit: int = Query(50, ge=1, le=200)) -> dict:
        if not store.get_job(job_id):
            raise HTTPException(404, "任务不存在")
        return {"items": store.recent_notices(job_id, limit)}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(
        job_id: str,
        request: Request,
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
        after: int = Query(0, ge=0),
    ) -> StreamingResponse:
        if not store.get_job(job_id):
            raise HTTPException(404, "任务不存在")
        try:
            cursor = int(last_event_id) if last_event_id else after
        except ValueError:
            cursor = after

        async def stream():
            nonlocal cursor
            idle_ticks = 0
            while not await request.is_disconnected():
                events = store.list_events(job_id, cursor)
                if events:
                    idle_ticks = 0
                    for event in events:
                        cursor = int(event["id"])
                        data = json.dumps(event["payload"], ensure_ascii=False)
                        yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {data}\n\n"
                else:
                    idle_ticks += 1
                    if idle_ticks % 15 == 0:
                        yield ": keepalive\n\n"
                    job = store.get_job(job_id)
                    if job and job["status"] in TERMINAL_STATUSES:
                        break
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/schedule")
    def get_schedule() -> dict:
        return scheduler.status()

    @app.put("/api/schedule")
    def update_schedule(payload: ScheduleRequest) -> dict:
        return scheduler.update_enabled(payload.enabled)

    @app.get("/api/sync/status")
    def sync_status() -> dict:
        return sync_manager.status()

    @app.post("/api/sync/start")
    def start_sync() -> dict:
        try:
            return sync_manager.start()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ConnectionError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/sync/retry-failed")
    def retry_failed_sync() -> dict:
        count = sync_manager.retry_failed()
        return {"reset": count, "status": sync_manager.status()}

    @app.get("/api/v1/notices", response_model=NoticeQueryResponse)
    def list_collected_notices(
        region_code: str | None = Query(default=None, pattern=r"^[0-9]{6}$"),
        start_date: date | None = None,
        end_date: date | None = None,
        keyword: str | None = Query(default=None, max_length=100),
        limit: int = Query(default=6, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=2048),
    ) -> NoticeQueryResponse:
        try:
            params = NoticeQueryParams(
                region_code=region_code,
                start_date=start_date,
                end_date=end_date,
                keyword=keyword,
                limit=limit,
            )
        except ValidationError as exc:
            raise HTTPException(422, str(exc.errors()[0]["msg"]).removeprefix("Value error, ")) from exc
        return query_notices(database, params, cursor)

    register_ai_routes(app, AIConfigStore(runtime))
    register_ocr_routes(app, OCRConfigStore(runtime))
    app.state.bid_skills = BidSkillRegistry(PROJECT_ROOT / "skills" / "bid_check")
    bid_manager = BidCheckManager(bid_store, app)
    app.state.bid_manager = bid_manager
    knowledge_manager = KnowledgeManager(knowledge_store, app)
    app.state.knowledge_store = knowledge_store
    app.state.knowledge_manager = knowledge_manager
    register_knowledge_routes(app, knowledge_store, knowledge_manager)

    @app.get("/api/bid-check/rules")
    def get_bid_check_rules():
        from .bid_skills import screening_snapshot, screening_metadata
        try:
            return screening_metadata(screening_snapshot(app.state.bid_skills))
        except BidCheckError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/bid-check/tasks")
    async def create_bid_check_task(
        region_code: str | None = Form(default=None), start_date: date | None = Form(default=None),
        end_date: date | None = Form(default=None), keyword: str | None = Form(default=None),
        files: list[UploadFile] = File(default=[]), expected_config: str | None = Form(default=None),
        knowledge_revision_id: str | None = Form(default=None),
    ):
        try:
            params = NoticeQueryParams(region_code=region_code, start_date=start_date, end_date=end_date, keyword=keyword, limit=100)
        except ValidationError as exc:
            raise HTTPException(422, str(exc.errors()[0]["msg"]).removeprefix("Value error, ")) from exc
        from .bid_skills import screening_snapshot
        try:
            rules = screening_snapshot(app.state.bid_skills)
        except BidCheckError as exc:
            raise HTTPException(503, str(exc)) from None
        if knowledge_revision_id:
            if files:
                raise HTTPException(422, "知识库版本与临时上传文件不能同时提交")
            try:
                snapshot = knowledge_store.revision(knowledge_revision_id)
                config = app.state.ai_config_store.get()
                public_config = {"base_url": config.base_url, "model": config.model, "protocol": config.protocol}
                if expected_config is not None and json.loads(expected_config) != public_config:
                    raise HTTPException(409, "模型配置已改变，请重新确认后提交")
                if not config.model or not config.api_key.get_secret_value():
                    raise HTTPException(422, "请先配置模型和 API Key")
                result = bid_store.create(database, params, [], public_config, uuid.uuid4().hex, snapshot=snapshot, knowledge_revision_id=knowledge_revision_id, rules_snapshot=rules)
                return {"id": result["id"], "status": "draft", "total": result["total"], "estimated_calls": result["estimated_calls"], "file_names": [], "knowledge_revision_id": knowledge_revision_id}
            except KeyError:
                raise HTTPException(404, "知识库版本不存在") from None
            except (ValueError, BidCheckError) as exc:
                raise HTTPException(422, str(exc) if isinstance(exc, BidCheckError) else "知识库或模型确认参数无效") from None
        if not files:
            raise HTTPException(422, "请至少上传一份公司资料")
        task_id = uuid.uuid4().hex
        task_dir = bid_store.root / task_id
        task_dir.mkdir(parents=True, mode=0o700)
        metadata = []
        total = 0
        try:
            for index, upload in enumerate(files, 1):
                name = Path(upload.filename or "").name
                extension = Path(name).suffix.lower().lstrip(".")
                if not name or extension not in ALLOWED_EXTENSIONS:
                    raise HTTPException(422, f"第 {index} 份资料格式不支持")
                target = task_dir / f"{index:03d}.{extension}"
                size = 0
                with target.open("wb") as output:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        total += len(chunk)
                        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                            raise HTTPException(422, "公司资料超过单文件 20 MB 或总计 50 MB 限制")
                        output.write(chunk)
                os.chmod(target, 0o600)
                if not size:
                    raise HTTPException(422, f"第 {index} 份资料为空")
                metadata.append({"name": name, "stored_name": target.name, "size": size, "content_type": upload.content_type or ""})
            config = app.state.ai_config_store.get()
            if expected_config is not None:
                try:
                    confirmed = json.loads(expected_config)
                except ValueError:
                    raise HTTPException(422, "模型确认参数无效") from None
                if confirmed != {"base_url": config.base_url, "model": config.model, "protocol": config.protocol}:
                    raise HTTPException(409, "模型配置已改变，请重新确认服务商和模型后提交")
            if not config.api_key.get_secret_value() or not config.model:
                raise HTTPException(422, "请先在模型配置中完成模型和 API Key 配置")
            result = bid_store.create(database, params, metadata, {"base_url": config.base_url, "model": config.model, "protocol": config.protocol}, task_id, rules_snapshot=rules)
            return {"id": task_id, "status": "draft", "total": result["total"], "estimated_calls": result["estimated_calls"], "file_names": result["file_names"], "message": "请确认后开始检标，模型将逐条处理全部匹配公告"}
        except HTTPException:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise
        except BidCheckError as exc:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise HTTPException(422, str(exc)) from None
        except Exception:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise HTTPException(503, "检标任务暂时无法创建，请稍后重试") from None

    @app.get("/api/bid-check/tasks")
    def list_bid_check_tasks(limit: int = Query(20, ge=1, le=100)):
        try:
            return {"items": bid_store.list_tasks(limit)}
        except BidCheckError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.get("/api/bid-check/tasks/{task_id}")
    def get_bid_check_task(task_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        try:
            return bid_store.detail(task_id, page, page_size)
        except KeyError:
            raise HTTPException(404, "检标任务不存在") from None
        except BidCheckError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/bid-check/tasks/{task_id}/start")
    def start_bid_check_task(task_id: str):
        try:
            task = bid_store.raw(task_id)
            if task["status"] == "stopped" and not task["profile"]:
                raise BidCheckError("任务已停止且资料已清理，请重新上传并创建任务")
            bid_store.transition(task_id, {"draft", "stopped"}, "queued")
        except KeyError:
            raise HTTPException(404, "检标任务不存在") from None
        except BidCheckError as exc:
            raise HTTPException(409, str(exc)) from None
        bid_manager.wake()
        return bid_store.detail(task_id)

    @app.post("/api/bid-check/tasks/{task_id}/stop")
    def stop_bid_check_task(task_id: str):
        try:
            if bid_store.request_stop(task_id):
                bid_manager.clean_files(task_id)
            bid_manager.wake()
            return bid_store.detail(task_id)
        except KeyError:
            raise HTTPException(404, "检标任务不存在") from None
        except BidCheckError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/bid-check/tasks/{task_id}/retry")
    def retry_bid_check_task(task_id: str):
        try:
            bid_store.transition(task_id, {"completed", "failed", "stopped"}, "queued", retry=True)
        except KeyError:
            raise HTTPException(404, "检标任务不存在") from None
        except BidCheckError as exc:
            raise HTTPException(409, str(exc)) from None
        bid_manager.wake()
        return bid_store.detail(task_id)

    frontend_dist = PROJECT_ROOT / "frontend" / "dist"
    assets = frontend_dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        index = frontend_dist / "index.html"
        requested = frontend_dist / path
        if path and requested.is_file() and frontend_dist in requested.resolve().parents:
            return FileResponse(requested)
        if index.exists():
            return FileResponse(index)
        return {
            "message": "CCGP API 已启动，前端尚未构建",
            "docs": "/docs",
        }

    return app


app = create_app()
