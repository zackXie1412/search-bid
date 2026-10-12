from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .ai_client import AIConnectionTestResult, AIModelClient
from .ai_config import AIConfigStorageError, AIConfigStore, AIConfigUpdate, AIConfigView


def register_ai_routes(app: FastAPI, store: AIConfigStore) -> None:
    app.state.ai_config_store = store
    app.state.ai_client = AIModelClient()

    @app.exception_handler(RequestValidationError)
    async def safe_validation_errors(request: Request, exc: RequestValidationError):
        if request.url.path.startswith(("/api/ai/", "/api/ocr/", "/api/bid-check/", "/api/knowledge/")):
            # FastAPI's default validation errors can echo the submitted key.
            errors = [{"loc": error["loc"], "type": error["type"], "msg": error["msg"]} for error in exc.errors()]
            return JSONResponse(status_code=422, content={"detail": errors})
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(AIConfigStorageError)
    async def storage_errors(request: Request, exc: AIConfigStorageError):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/api/ai/config", response_model=AIConfigView)
    def get_ai_config():
        return store.view()

    @app.put("/api/ai/config", response_model=AIConfigView)
    def save_ai_config(payload: AIConfigUpdate):
        try:
            return store.save(payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/ai/test", response_model=AIConnectionTestResult)
    async def test_ai_connection(payload: AIConfigUpdate):
        try:
            config = store.resolve(payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return await app.state.ai_client.test(config)
