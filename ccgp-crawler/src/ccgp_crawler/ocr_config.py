"""Private configuration for the fixed PaddleOCR AI Studio service."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator
from .ai_config import AIConfigStore, AIConfigStorageError

OCR_BASE_URL = 'https://paddleocr.aistudio-app.com'


class OCRFields(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    provider: Literal['paddle_aistudio'] = 'paddle_aistudio'
    model: Literal['PP-OCRv5'] = 'PP-OCRv5'


class OCRConfig(OCRFields):
    access_token: SecretStr = SecretStr('')


class OCRConfigView(OCRFields):
    access_token_configured: bool


class OCRConfigUpdate(OCRFields):
    access_token: SecretStr | None = None
    clear_access_token: bool = False

    @field_validator('access_token')
    @classmethod
    def token_format(cls, value):
        if value is not None:
            token = value.get_secret_value().strip()
            if len(token) > 8192 or not token.isascii() or any(c.isspace() or ord(c)<32 for c in token):
                raise ValueError('Access Token 格式无效')
            return SecretStr(token)
        return value

    @model_validator(mode='after')
    def clear_or_replace(self):
        if self.clear_access_token and self.access_token and self.access_token.get_secret_value():
            raise ValueError('替换和清除令牌不能同时执行')
        return self


def public_view(config):
    return OCRConfigView(**config.model_dump(exclude={'access_token'}),
                         access_token_configured=bool(config.access_token.get_secret_value()))


class OCRConfigStore(AIConfigStore):
    def __init__(self, runtime_dir: Path):
        super().__init__(runtime_dir)
        self.path = runtime_dir / 'ocr_config.json'

    def _read(self):
        try:
            return OCRConfig.model_validate_json(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return OCRConfig()
        except (UnicodeError, ValueError):
            raise AIConfigStorageError('OCR 配置文件损坏，请检查本机配置') from None

    def view(self):
        return public_view(self.get())

    def _resolve(self, payload, current):
        token = payload.access_token.get_secret_value() if payload.access_token else ''
        return OCRConfig(**payload.model_dump(exclude={'access_token','clear_access_token'}),
                         access_token=SecretStr('' if payload.clear_access_token else token or current.access_token.get_secret_value()))

    def save(self, payload):
        with self._locked():
            config = self._resolve(payload, self._read())
            content = config.model_dump(exclude={'access_token'})
            content['access_token'] = config.access_token.get_secret_value()
            temporary = None
            try:
                fd, temporary = tempfile.mkstemp(prefix='.ocr_config-', dir=self.path.parent)
                with os.fdopen(fd,'w',encoding='utf-8') as file:
                    os.fchmod(file.fileno(),0o600)
                    json.dump(content,file,ensure_ascii=False)
                    file.flush();os.fsync(file.fileno())
                os.replace(temporary,self.path)
            finally:
                if temporary and os.path.exists(temporary):os.unlink(temporary)
            return public_view(config)
