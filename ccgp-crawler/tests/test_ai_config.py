import asyncio
import json
import stat
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from ccgp_crawler.ai_client import AIModelClient, TEST_INPUT
from ccgp_crawler.ai_config import AIConfig, AIConfigStore, AIConfigUpdate
from ccgp_crawler.api import create_app
from ccgp_crawler.bid_skills import BidSkillRegistry

SECRET = "test-secret-do-not-return"
PAYLOAD = {"base_url": "https://api.openai.com/v1", "model": "gpt-test", "protocol": "responses"}


@pytest.fixture
def app(tmp_path):
    return create_app(database_path=tmp_path / "db.sqlite3", runtime_dir=tmp_path / "runtime", start_background_services=False)


def install_upstream(app, handler):
    app.state.ai_client = AIModelClient(transport=httpx.MockTransport(handler))


def success(protocol="responses"):
    if protocol == "responses":
        return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]}
    return {"choices": [{"message": {"content": "OK"}}]}


def test_default_config_no_secret_and_default_screening_skill(app):
    with TestClient(app) as client:
        result = client.get("/api/ai/config")
    assert result.status_code == 200
    assert result.json() == {**PAYLOAD, "model": "", "api_key_configured": False}
    assert app.state.bid_skills.list_ids() == ['qualification-screening']
    assert not app.state.ai_config_store.path.exists()


def test_persist_reload_preserve_replace_clear_and_file_permissions(app):
    with TestClient(app) as client:
        saved = client.put("/api/ai/config", json={**PAYLOAD, "api_key": SECRET})
        assert saved.status_code == 200
        assert saved.json()["api_key_configured"]
        assert SECRET not in saved.text and "api_key" not in saved.json()
        assert stat.S_IMODE(app.state.ai_config_store.path.stat().st_mode) == 0o600
        reloaded = AIConfigStore(app.state.ai_config_store.path.parent)
        assert reloaded.get().api_key.get_secret_value() == SECRET
        for key in ({}, {"api_key": ""}, {"api_key": None}):
            assert client.put("/api/ai/config", json={**PAYLOAD, **key}).status_code == 200
            assert reloaded.get().api_key.get_secret_value() == SECRET
        assert client.put("/api/ai/config", json={**PAYLOAD, "api_key": "replacement"}).status_code == 200
        assert reloaded.get().api_key.get_secret_value() == "replacement"
        assert client.put("/api/ai/config", json={**PAYLOAD, "clear_api_key": True}).json()["api_key_configured"] is False
        assert reloaded.get().api_key.get_secret_value() == ""
        assert SECRET not in client.get("/api/ai/config").text


def test_changed_address_never_reuses_saved_key(app):
    calls = []
    install_upstream(app, lambda request: calls.append(request) or httpx.Response(200, json=success()))
    with TestClient(app) as client:
        client.put("/api/ai/config", json={**PAYLOAD, "api_key": SECRET})
        changed = {**PAYLOAD, "base_url": "https://other.example/v1"}
        for method, path in (("PUT", "/api/ai/config"), ("POST", "/api/ai/test")):
            response = client.request(method, path, json=changed)
            assert response.status_code == 422
            assert "Base URL 已改变" in response.json()["detail"]
        assert calls == []
        assert app.state.ai_config_store.get().base_url == PAYLOAD["base_url"]
        # Equivalent URLs may preserve the key; query parameters and credentials may not.
        assert client.put("/api/ai/config", json={**PAYLOAD, "base_url": "https://API.OPENAI.COM:443/v1/"}).status_code == 200
        assert client.post("/api/ai/test", json={**changed, "clear_api_key": True}).json()["ok"] is False
        assert calls == []
        result = client.post("/api/ai/test", json={**changed, "api_key": "new-key"})
        assert result.json()["ok"]
        assert str(calls[0].url) == "https://other.example/v1/responses"
        assert calls[0].headers["authorization"] == "Bearer new-key"
        assert client.put("/api/ai/config", json={**changed, "clear_api_key": True}).status_code == 200
        assert not client.get("/api/ai/config").json()["api_key_configured"]


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_draft_connection_generates_text_without_saving(app, protocol):
    calls = []
    def upstream(request):
        calls.append(request)
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        payload = json.loads(request.content)
        assert payload["model"] == PAYLOAD["model"]
        if protocol == "responses":
            assert request.url.path == "/v1/responses"
            assert payload["input"] == TEST_INPUT and payload["store"] is False
        else:
            assert request.url.path == "/v1/chat/completions"
            assert payload["messages"] == [{"role": "user", "content": TEST_INPUT}]
        return httpx.Response(200, json=success(protocol))
    install_upstream(app, upstream)
    with TestClient(app) as client:
        result = client.post("/api/ai/test", json={**PAYLOAD, "protocol": protocol, "api_key": SECRET})
        assert result.status_code == 200
        assert result.json()["ok"] is True
        assert result.json()["protocol"] == protocol
        assert result.json()["elapsed_ms"] >= 0
        assert SECRET not in result.text
        assert client.get("/api/ai/config").json()["model"] == ""
    assert len(calls) == 1
    assert not app.state.ai_config_store.path.exists()


def test_saved_key_used_for_same_address_and_clear_only_applies_to_draft(app):
    calls = []
    install_upstream(app, lambda request: calls.append(request) or httpx.Response(200, json=success()))
    with TestClient(app) as client:
        client.put("/api/ai/config", json={**PAYLOAD, "api_key": SECRET})
        assert client.post("/api/ai/test", json=PAYLOAD).json()["ok"]
        assert calls[0].headers["authorization"] == f"Bearer {SECRET}"
        assert not client.post("/api/ai/test", json={**PAYLOAD, "clear_api_key": True}).json()["ok"]
        assert len(calls) == 1
        assert client.get("/api/ai/config").json()["api_key_configured"]


def test_custom_root_url_joins_endpoint_without_double_slash(app):
    calls = []
    install_upstream(app, lambda request: calls.append(request) or httpx.Response(200, json=success()))
    with TestClient(app) as client:
        assert client.post("/api/ai/test", json={**PAYLOAD, "base_url": "http://localhost:9999/", "api_key": SECRET}).json()["ok"]
    assert str(calls[0].url) == "http://localhost:9999/responses"


@pytest.mark.parametrize("overrides", [
    {"base_url": "ftp://example.com"}, {"base_url": "https://user:secret@example.com/v1"},
    {"base_url": "https://example.com/v1?key=secret"}, {"base_url": "https://example.com/v1#fragment"},
    {"base_url": "https://example.com/v1/responses"}, {"base_url": "https://example.com:invalid/v1"},
    {"base_url": "https://example.com/v1\nfoo"}, {"model": ""}, {"model": "   "},
    {"base_url": "https://💩.com/v1"}, {"base_url": "https://example.com:0/v1"},
    {"base_url": "https://example.com/\\path"}, {"api_key": "中文密钥"},
    {"model": "has space"}, {"protocol": "unknown"}, {"api_key": [SECRET]},
    {"api_key": "abc\ndef"}, {"clear_api_key": True}, {"extra": SECRET},
])
def test_validation_422_never_echoes_secret(app, overrides):
    with TestClient(app) as client:
        for path, method in (("/api/ai/config", "PUT"), ("/api/ai/test", "POST")):
            response = client.request(method, path, json={**PAYLOAD, "api_key": SECRET, **overrides})
            assert response.status_code == 422
            assert SECRET not in response.text
            assert all("input" not in error and "ctx" not in error for error in response.json()["detail"])


@pytest.mark.parametrize("status,body,expected", [
    (401, {"error": {"message": SECRET}}, "认证失败"),
    (403, {}, "认证失败"),
    (404, {"error": {"code": "model_not_found", "message": SECRET}}, "模型不存在"),
    (429, {"error": SECRET}, "限流"),
    (400, {}, "不兼容"), (404, {}, "不兼容"), (500, {}, "暂时不可用"),
    (302, {}, "重定向"), (200, {"choices": []}, "有效文本"),
    (200, {"status": "failed", "error": SECRET}, "未完成"),
    (400, {"error": {"code": []}}, "不兼容"),
])
def test_upstream_failures_are_safe_and_not_retried(app, status, body, expected):
    calls = []
    install_upstream(app, lambda request: calls.append(request) or httpx.Response(status, json=body))
    with TestClient(app) as client:
        result = client.post("/api/ai/test", json={**PAYLOAD, "api_key": SECRET})
    assert result.status_code == 200
    assert not result.json()["ok"] and expected in result.json()["message"]
    assert SECRET not in result.text and len(calls) == 1


@pytest.mark.parametrize("failure", ["timeout", "network", "invalid_json"])
def test_network_timeout_and_invalid_response(app, failure):
    def upstream(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET)
        if failure == "network":
            raise httpx.ConnectError(SECRET)
        return httpx.Response(200, text=SECRET)
    install_upstream(app, upstream)
    with TestClient(app) as client:
        result = client.post("/api/ai/test", json={**PAYLOAD, "api_key": SECRET})
    assert result.status_code == 200 and not result.json()["ok"]
    assert SECRET not in result.text


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_failures_include_http_status_without_upstream_secrets(app, status):
    install_upstream(app, lambda request: httpx.Response(status, json={"error": {"message": SECRET, "code": SECRET}}))
    with TestClient(app) as client:
        result = client.post("/api/ai/test", json={**PAYLOAD, "api_key": SECRET}).json()
    assert f"HTTP {status}" in result["message"] and SECRET not in result["message"]


def test_chat_completions_json_schema_has_correct_nesting(app):
    config = AIConfig(**PAYLOAD, api_key=SECRET).model_copy(update={"protocol": "chat_completions"})
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    def upstream(request):
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_schema", "json_schema": {"name": "bid_check", "schema": schema, "strict": True}}
        return httpx.Response(200, json=success("chat_completions"))
    asyncio.run(AIModelClient(transport=httpx.MockTransport(upstream)).generate(config, "测试", schema=schema))


def test_deepseek_chat_uses_json_object_max_tokens_and_bounded_thinking(app):
    config = AIConfig(base_url="https://api.deepseek.com", model="deepseek-flash", protocol="chat_completions", api_key=SECRET)
    schema = {"type": "object", "properties": {"company_name": {"type": "string"}}, "required": ["company_name"], "additionalProperties": False}
    def upstream(request):
        payload = json.loads(request.content)
        assert request.url.path == "/chat/completions"
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["max_tokens"] == 8192 and "max_completion_tokens" not in payload
        assert "json schema" in payload["messages"][0]["content"].lower()
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"company_name":"测试公司"}'}}]})
    output = asyncio.run(AIModelClient(transport=httpx.MockTransport(upstream)).generate(config, "资料", instructions="输出 JSON schema", schema=schema, max_output_tokens=8192))
    assert json.loads(output)["company_name"] == "测试公司"


@pytest.mark.parametrize("base", ["https://api.deepseek.com", "https://api.deepseek.com/v1", "https://proxy.example/v1"])
def test_deepseek_test_and_vision_payload(base):
    config = AIConfig(base_url=base, model="deepseek-flash", protocol="chat_completions", api_key=SECRET)
    calls = []
    def upstream(request):
        payload = json.loads(request.content); calls.append(payload)
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["max_tokens"] == 1024
        assert "max_completion_tokens" not in payload
        return httpx.Response(200, json=success("chat_completions"))
    model = AIModelClient(transport=httpx.MockTransport(upstream))
    assert asyncio.run(model.test(config)).ok
    assert len(calls) == 1
    assert asyncio.run(model.generate(config, "资料", images=[{"source_id": "img1", "data_url": "data:image/png;base64,test"}])) == "OK"
    assert calls[1]["messages"][-1]["content"][-1]["type"] == "image_url"


def test_deepseek_text_only_model_rejects_images_before_sending():
    calls = []
    model = AIModelClient(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, json=success("chat_completions"))))
    config = AIConfig(base_url="https://api.deepseek.com", model="deepseek-v4-pro", protocol="chat_completions", api_key=SECRET)
    from ccgp_crawler.ai_client import AIModelError
    with pytest.raises(AIModelError, match="不支持图片"):
        asyncio.run(model.generate(config, "资料", images=[{"source_id":"img1","data_url":"data:image/png;base64,test"}]))
    assert not calls


def test_deepseek_truncated_output_is_not_accepted():
    config = AIConfig(base_url="https://api.deepseek.com", model="deepseek-flash", protocol="chat_completions", api_key=SECRET)
    model = AIModelClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices":[{"finish_reason":"length","message":{"content":"{\"ok\":true}"}}]})))
    from ccgp_crawler.ai_client import AIModelError
    with pytest.raises(AIModelError, match="长度上限"):
        asyncio.run(model.generate(config, "JSON"))


def test_total_deadline_covers_entire_request(app, monkeypatch):
    monkeypatch.setattr("ccgp_crawler.ai_client.CONNECTION_TIMEOUT", 0.01)
    async def slow(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=success())
    install_upstream(app, slow)
    with TestClient(app) as client:
        result = client.post("/api/ai/test", json={**PAYLOAD, "api_key": SECRET}).json()
    assert not result["ok"] and "超时" in result["message"]


@pytest.mark.parametrize("failure", ["corrupt", "unreadable", "unwritable", "non_utf8"])
def test_storage_exceptions_are_503_without_secrets(app, failure, monkeypatch):
    path = app.state.ai_config_store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    if failure == "corrupt":
        path.write_text('{"api_key": "' + SECRET, encoding="utf-8")
    elif failure == "non_utf8":
        path.write_bytes(b"\xff\xfe")
    elif failure == "unreadable":
        path.mkdir()
    else:
        def denied(*args, **kwargs):
            raise PermissionError(SECRET)
        monkeypatch.setattr("ccgp_crawler.ai_config.os.replace", denied)
    with TestClient(app) as client:
        if failure != "unwritable":
            get_result = client.get("/api/ai/config")
            assert get_result.status_code == 503 and SECRET not in get_result.text
        result = client.put("/api/ai/config", json={**PAYLOAD, "api_key": SECRET})
    assert result.status_code == 503 and SECRET not in result.text
    assert not list(path.parent.glob(".ai_config-*"))


def test_failed_atomic_write_preserves_old_config(app, monkeypatch):
    store = app.state.ai_config_store
    store.save(AIConfigUpdate(**PAYLOAD, api_key=SECRET))
    original = store.path.read_bytes()
    def denied(*args, **kwargs):
        raise OSError("write failure")
    monkeypatch.setattr("ccgp_crawler.ai_config.os.replace", denied)
    with TestClient(app) as client:
        assert client.put("/api/ai/config", json={**PAYLOAD, "api_key": "new"}).status_code == 503
    assert store.path.read_bytes() == original


def test_concurrent_store_instances_preserve_key(tmp_path):
    AIConfigStore(tmp_path).save(AIConfigUpdate(**PAYLOAD, api_key=SECRET))
    def save(index):
        return AIConfigStore(tmp_path).save(AIConfigUpdate(**{**PAYLOAD, "model": f"gpt-{index}"}))
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(save, range(12)))
    assert all(result.api_key_configured for result in results)
    assert AIConfigStore(tmp_path).get().api_key.get_secret_value() == SECRET


def test_instruction_skills_load_explicit_ids_only(tmp_path):
    registry = BidSkillRegistry(tmp_path / "skills")
    assert registry.list_ids() == []
    registry.root.mkdir()
    (registry.root / "SKILL.template.md").write_text("模板", encoding="utf-8")
    directory = registry.root / "qualification"
    directory.mkdir()
    (directory / "SKILL.md").write_text("根据资质证据判断，不得补造事实。", encoding="utf-8")
    assert registry.list_ids() == ["qualification"]
    skills = registry.load(["qualification", "qualification"])
    assert len(skills) == 1 and skills[0].id == "qualification"
    assert "不得补造事实" in skills[0].instructions
    for invalid in ("../qualification", "/tmp", "missing"):
        with pytest.raises(ValueError):
            registry.get(invalid)
    (registry.root / "external").symlink_to(directory, target_is_directory=True)
    (directory / "SKILL.md").unlink()
    (directory / "SKILL.md").symlink_to(registry.root / "SKILL.template.md")
    assert registry.list_ids() == []
    with pytest.raises(ValueError):
        registry.get("qualification")


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_shared_client_accepts_future_skill_instructions(protocol):
    def upstream(request):
        body = json.loads(request.content)
        if protocol == "responses":
            assert body["instructions"] == "检标规则"
        else:
            assert body["messages"][0] == {"role": "system", "content": "检标规则"}
        return httpx.Response(200, json=success(protocol))
    client = AIModelClient(transport=httpx.MockTransport(upstream))
    config = AIConfig(**{**PAYLOAD, "protocol": protocol}, api_key=SECRET)
    assert asyncio.run(client.generate(config, "模拟输入", instructions="检标规则")) == "OK"
