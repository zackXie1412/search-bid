"""Local model configuration. Secrets never appear in the public view."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

DEFAULT_BASE_URL = "https://api.openai.com/v1"
AIProtocol = Literal["responses", "chat_completions"]


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("Base URL 无效") from None
    if (
        url.scheme not in {"https", "http"} or not url.hostname
        or url.username is not None or url.password is not None
        or url.query or url.fragment or "\\" in value
        or (port is not None and port < 1)
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise ValueError("Base URL 必须是无账号、查询参数或片段的 HTTP(S) 地址")
    if url.path.endswith(("/responses", "/chat/completions")):
        raise ValueError("请填写 API 根地址，例如 https://api.openai.com/v1")
    host = url.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if url.scheme == "https" else 80
    netloc = host if port is None or port == default_port else f"{host}:{port}"
    try:
        return str(httpx.URL(urlunsplit((url.scheme, netloc, url.path.rstrip("/"), "", "")))).rstrip("/")
    except httpx.InvalidURL:
        raise ValueError("Base URL 无效") from None


class AIConfigFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(default=DEFAULT_BASE_URL, max_length=2048)
    model: str = Field(default="", max_length=200)
    protocol: AIProtocol = "responses"

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return normalize_base_url(value)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        value = value.strip()
        if any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("模型名称不得包含空白字符")
        return value


class AIConfig(AIConfigFields):
    api_key: SecretStr = SecretStr("")


class AIConfigView(AIConfigFields):
    api_key_configured: bool


class AIConfigUpdate(AIConfigFields):
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = None
    clear_api_key: bool = False

    @field_validator("model")
    @classmethod
    def require_model(cls, value: str) -> str:
        if not value:
            raise ValueError("请填写模型名称")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        key = value.get_secret_value().strip()
        if not key.isascii() or len(key) > 8192 or any(c.isspace() or ord(c) < 32 for c in key):
            raise ValueError("API Key 格式无效")
        return SecretStr(key)

    @model_validator(mode="after")
    def unambiguous_key_update(self):
        if self.clear_api_key and self.api_key and self.api_key.get_secret_value():
            raise ValueError("清除密钥时不能同时输入新密钥")
        return self


class AIConfigStorageError(Exception):
    pass


def public_view(config: AIConfig) -> AIConfigView:
    return AIConfigView(
        base_url=config.base_url, model=config.model, protocol=config.protocol,
        api_key_configured=bool(config.api_key.get_secret_value()),
    )


class AIConfigStore:
    def __init__(self, runtime_dir: Path):
        self.path = runtime_dir / "ai_config.json"
        self._lock = RLock()

    @contextmanager
    def _locked(self):
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
                with os.fdopen(fd, "a+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    yield
            except OSError:
                raise AIConfigStorageError("模型配置暂时无法读写，请检查本机存储") from None

    def _read(self) -> AIConfig:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return AIConfig()
        except UnicodeError:
            raise AIConfigStorageError("模型配置文件损坏，请修复本机配置文件后重试") from None
        try:
            return AIConfig.model_validate_json(content)
        except ValueError:
            raise AIConfigStorageError("模型配置文件损坏，请修复本机配置文件后重试") from None

    def get(self) -> AIConfig:
        with self._locked():
            return self._read()

    def view(self) -> AIConfigView:
        return public_view(self.get())

    def _resolve(self, payload: AIConfigUpdate, current: AIConfig) -> AIConfig:
        new_key = payload.api_key.get_secret_value() if payload.api_key else ""
        if (
            payload.base_url != current.base_url and current.api_key.get_secret_value()
            and not new_key and not payload.clear_api_key
        ):
            raise ValueError("Base URL 已改变，请输入新密钥或明确清除原密钥")
        key = "" if payload.clear_api_key else (new_key or current.api_key.get_secret_value())
        return AIConfig(
            base_url=payload.base_url, model=payload.model,
            protocol=payload.protocol, api_key=SecretStr(key),
        )

    def resolve(self, payload: AIConfigUpdate) -> AIConfig:
        """Resolve a draft for testing without saving it."""
        with self._locked():
            return self._resolve(payload, self._read())

    def save(self, payload: AIConfigUpdate) -> AIConfigView:
        with self._locked():
            config = self._resolve(payload, self._read())
            content = config.model_dump(exclude={"api_key"})
            content["api_key"] = config.api_key.get_secret_value()
            temporary: str | None = None
            try:
                fd, temporary = tempfile.mkstemp(prefix=".ai_config-", dir=self.path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    os.fchmod(file.fileno(), 0o600)
                    json.dump(content, file, ensure_ascii=False)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
            return public_view(config)
