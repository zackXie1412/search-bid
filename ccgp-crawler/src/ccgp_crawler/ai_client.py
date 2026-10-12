"""A small server-side client reusable by future bid checking."""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from .ai_config import AIConfig, AIProtocol

CONNECTION_TIMEOUT = 30.0
TEST_INPUT = "这是一次接口连接测试。请只回复 OK，不添加其他内容。"


class AIConnectionTestResult(BaseModel):
    ok: bool
    model: str
    protocol: AIProtocol
    elapsed_ms: int
    message: str


class AIModelError(Exception):
    """Only fixed, safe user-facing messages, never upstream bodies or URLs."""

    def __init__(self, message: str, *, status_code: int | None = None, category="unknown", retryable=False):
        super().__init__(message)
        self.status_code = status_code
        self.category = category
        self.retryable = retryable


def _upstream_error(response: httpx.Response) -> AIModelError:
    code = ""
    try:
        body = response.json()
        error = body.get("error", {}) if isinstance(body, dict) else {}
        if isinstance(error, dict):
            value = error.get("code", "")
            code = value if isinstance(value, str) else ""
    except ValueError:
        pass
    if response.status_code in {401, 403} or code == "invalid_api_key":
        return AIModelError("认证失败，请检查 API Key 和模型访问权限", status_code=response.status_code, category="authentication")
    if code in {"model_not_found", "invalid_model", "model_not_supported"}:
        return AIModelError("模型不存在或不可用，请检查模型名称及访问权限", status_code=response.status_code, category="model")
    if response.status_code == 429:
        return AIModelError("请求被限流或额度不足（HTTP 429），请检查服务商额度后重试", status_code=429, category="rate_limit")
    if response.status_code == 402:
        return AIModelError("服务商账户余额不足或未开通模型权限（HTTP 402），请检查 DeepSeek 账户额度", status_code=402, category="billing")
    if response.status_code == 413:
        return AIModelError("发送给模型的资料过大（HTTP 413），请减少资料或拆分任务", status_code=413, category="input_size")
    if response.status_code in {400, 404, 405, 415, 422}:
        return AIModelError(f"接口地址、模型或协议不兼容（HTTP {response.status_code}），请检查 Base URL、模型名称及调用协议", status_code=response.status_code, category="compatibility")
    if 300 <= response.status_code < 400:
        return AIModelError("接口返回了重定向，请填写最终 API 根地址")
    reasons = {500: "服务商内部错误", 502: "服务商网关错误", 503: "服务商过载或维护", 504: "服务商网关等待模型超时"}
    reason = reasons.get(response.status_code, "服务商返回异常状态")
    return AIModelError(f"模型服务暂时不可用：{reason}（HTTP {response.status_code}）", status_code=response.status_code,
                        category="upstream_server", retryable=response.status_code in {408, 500, 502, 503, 504})


def _output_text(body: object, protocol: AIProtocol) -> str:
    if not isinstance(body, dict):
        return ""
    if protocol == "responses":
        output = body.get("output")
        if not isinstance(output, list):
            return ""
        texts = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
                if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    texts.append(content["text"])
        return "".join(texts).strip()
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    text = message.get("content") if isinstance(message, dict) else None
    return text.strip() if isinstance(text, str) else ""


class AIModelClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    async def generate(
        self, config: AIConfig, input_text: str, *, instructions: str = "",
        images: list[dict] | None = None, schema: dict | None = None,
        timeout: float | None = None, max_output_tokens: int = 1024,
    ) -> str:
        """Future callers may supply the instruction text loaded from bid Skills."""
        key = config.api_key.get_secret_value()
        if not key:
            raise AIModelError("尚未配置 API Key，请输入密钥后测试")
        deadline = CONNECTION_TIMEOUT if timeout is None else timeout
        hostname = (urlsplit(config.base_url).hostname or "").lower()
        is_deepseek = hostname == "api.deepseek.com" or hostname.endswith(".deepseek.com") or config.model.lower().startswith("deepseek-")
        if is_deepseek and images and config.model.lower() in {"deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"}:
            raise AIModelError("此 DeepSeek 模型不支持图片资料，请使用 deepseek-flash 或上传可提取文字的资料", category="compatibility")
        if config.protocol == "responses":
            endpoint = "responses"
            content = [{"type": "input_text", "text": input_text}]
            for img in images or []:
                content.extend([
                    {"type": "input_text", "text": f"资料来源：{img['source_id']}"},
                    {"type": "input_image", "image_url": img["data_url"], "detail": "high"},
                ])
            payload = {"model": config.model, "input": [{"role": "user", "content": content}] if images else input_text, "max_output_tokens": max_output_tokens, "store": False}
            if instructions:
                payload["instructions"] = instructions
        else:
            endpoint = "chat/completions"
            messages = []
            if instructions:
                messages.append({"role": "system", "content": instructions})
            messages.append({"role": "user", "content": input_text})
            if images:
                content = [{"type": "text", "text": input_text}]
                for img in images:
                    content.extend([
                        {"type": "text", "text": f"资料来源：{img['source_id']}"},
                        {"type": "image_url", "image_url": {"url": img["data_url"], "detail": "high"}},
                    ])
                messages[-1]["content"] = content
            payload = {"model": config.model, "messages": messages}
            if schema or is_deepseek:
                payload["max_tokens" if is_deepseek else "max_completion_tokens"] = max_output_tokens
            if is_deepseek:
                # DeepSeek enables thinking by default; disable it for bounded JSON calls.
                payload["thinking"] = {"type": "disabled"}
        if schema:
            formatting = {"type": "json_schema", "name": "bid_check", "schema": schema, "strict": True}
            if config.protocol == "responses":
                payload["text"] = {"format": formatting}
            elif is_deepseek:
                # DeepSeek supports JSON Output rather than OpenAI's json_schema format.
                payload["response_format"] = {"type": "json_object"}
            else:
                payload["response_format"] = {"type": "json_schema", "json_schema": {key: value for key, value in formatting.items() if key != "type"}}
        try:
            # The outer deadline covers the whole request, not each network phase.
            async with asyncio.timeout(deadline):
                async with httpx.AsyncClient(
                    transport=self.transport, timeout=deadline,
                    follow_redirects=False, trust_env=False,
                ) as client:
                    response = await client.post(
                        f"{config.base_url}/{endpoint}", json=payload,
                        headers={"Authorization": f"Bearer {key}"},
                    )
                    # Compatible providers may support generation but not json_schema.
                    # Fall back only on an explicit unsupported-format error.
                    if schema and response.status_code in {400, 422}:
                        try:
                            error = response.json().get("error", {})
                            description = str(error.get("message", "")).lower() if isinstance(error, dict) else ""
                        except (ValueError, AttributeError):
                            description = ""
                        if any(term in description for term in ("json_schema", "response_format", "text.format")) and any(term in description for term in ("unsupported", "not support", "unknown", "unrecognized")):
                            payload.pop("text", None)
                            payload.pop("response_format", None)
                            response = await client.post(
                                f"{config.base_url}/{endpoint}", json=payload,
                                headers={"Authorization": f"Bearer {key}"},
                            )
                if not response.is_success:
                    raise _upstream_error(response)
                body = response.json()
                if config.protocol == "chat_completions" and isinstance(body, dict):
                    choices = body.get("choices")
                    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
                    if first.get("finish_reason") == "length":
                        raise AIModelError("模型输出达到长度上限，未完成 JSON 生成，请减少资料或更换模型", category="output_limit")
                if isinstance(body, dict) and (body.get("error") or body.get("status") in {"failed", "incomplete"}):
                    raise AIModelError("模型未完成文本生成，请检查模型和接口兼容性")
                text = _output_text(body, config.protocol)
                if not text:
                    raise AIModelError("接口未返回有效文本，请检查模型和调用协议")
                return text
        except (TimeoutError, httpx.TimeoutException):
            raise AIModelError(f"连接超时（{deadline:g} 秒），请检查接口地址和网络", category="timeout", retryable=True) from None
        except httpx.RequestError:
            raise AIModelError("无法连接模型接口，请检查 Base URL 和网络", category="network", retryable=True) from None
        except (ValueError, UnicodeError):
            raise AIModelError("接口返回格式无效，请检查 Base URL 和调用协议") from None

    async def test(self, config: AIConfig) -> AIConnectionTestResult:
        started = time.perf_counter()
        try:
            await self.generate(config, TEST_INPUT)
            ok, message = True, "连接成功，模型已返回有效文本"
        except AIModelError as exc:
            ok, message = False, str(exc)
        return AIConnectionTestResult(
            ok=ok, model=config.model, protocol=config.protocol,
            elapsed_ms=round((time.perf_counter() - started) * 1000), message=message,
        )
