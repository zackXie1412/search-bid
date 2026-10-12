from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .ai_client import AIModelError
from .bid_documents import extract_documents
from .bid_models import (
    BID_INSTRUCTIONS, COMPANY_INSTRUCTIONS, BidCheckError, BidDecision, CompanyProfile,
    parse_json_output, validate_decision, validate_profile,
)
from .bid_store import BidTaskStore
from .bid_notice import attachment_entries, notice_snapshot
from .bid_screening import ScreeningOutput, build_evidence_index, evaluate_screening

logger = logging.getLogger(__name__)


class TaskInterrupted(Exception):
    pass


class NoticeReader:
    def __init__(self, *, transport=None):
        self.transport = transport

    async def read(self, url: str) -> str:
        return (await self.read_snapshot(url))['body']

    async def read_snapshot(self, url: str) -> dict:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.ccgp.gov.cn" or not any(parsed.path.startswith(path) for path in ("/cggg/zygg/gkzb/", "/cggg/dfgg/gkzb/")):
            raise BidCheckError("公告网址不在支持范围，本条未完成判断")
        try:
            async with httpx.AsyncClient(timeout=25, follow_redirects=False, transport=self.transport) as client:
                async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"}) as response:
                    if response.status_code == 403:
                        raise BidCheckError("公告访问受限（403），无法读取资格要求，可稍后重试")
                    if response.status_code != 200:
                        raise BidCheckError("公告无法读取，网站返回错误或重定向，可稍后重试")
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 5 * 1024 * 1024:
                            raise BidCheckError("公告正文过大，本条未完成判断")
                        chunks.append(chunk)
            soup = BeautifulSoup(b"".join(chunks), "lxml")
            for element in soup.select("script,style,nav,header,footer"):
                element.decompose()
            content = soup.select_one(".vF_detail_content") or soup.select_one(".vF_detail_content_container")
            if content is None:
                raise BidCheckError("公告正文无法识别，可能需要验证码或访问受限")
            # Inline spans do not create artificial clauses; preserve actual block boundaries.
            attachments = attachment_entries(content, url)
            for br in content.select('br'):
                br.replace_with('\n')
            for block in content.select('p,div,li,tr,h1,h2,h3,h4,h5,h6'):
                block.insert_before('\n')
                block.insert_after('\n')
            text = '\n'.join(line.strip() for line in content.get_text().splitlines() if line.strip())
            if len(text) < 100 or not any(term in text for term in ("资格", "申请人", "投标人", "供应商")):
                raise BidCheckError("公告没有可读取的资格要求，本条未完成判断")
            if len(text) > 120_000:
                raise BidCheckError("公告超出分析范围，本条未完成判断；未截断正文")
            return notice_snapshot(text, attachments)
        except BidCheckError:
            raise
        except Exception:
            raise BidCheckError("读取公告失败或超时，请稍后重试") from None


def knowledge_context_for_model(context):
    # Durable snapshots retain full evidence; model requests need short references only.
    return {**context,'material_support':[
        {**record,'sources':[{k:v for k,v in source.items() if k in {'document_id','filename','location','quote','ocr','entry_id'}} for source in record.get('sources',[])]}
        for record in context.get('material_support',[])]}


class BidCheckManager:
    def __init__(self, store: BidTaskStore, app, *, interval=2.0):
        self.store, self.app, self.interval = store, app, interval
        self.reader = NoticeReader()
        self.retry_delays = (3.0, 8.0)
        self._shutdown = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._shutdown.clear()
        self.store.recover()
        self._thread = threading.Thread(target=self._run, name="bid-check-worker", daemon=True)
        self._thread.start()

    def stop(self):
        self._shutdown.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)

    def wake(self):
        self._wake.set()

    def clean_files(self, task_id):
        shutil.rmtree(self.store.root / task_id, ignore_errors=True)

    def _run(self):
        while not self._shutdown.is_set():
            try:
                task_id = self.store.queued()
                if task_id:
                    asyncio.run(self.process_task(task_id))
                    continue
            except Exception:
                # Do not log exceptions containing company material or provider bodies.
                self._shutdown.wait(2)
            self._wake.wait(1)
            self._wake.clear()

    def _configuration(self, task):
        config = self.app.state.ai_config_store.get()
        snapshot = task["model_config"]
        if config.base_url != snapshot["base_url"] or config.model != snapshot["model"] or config.protocol != snapshot["protocol"]:
            raise BidCheckError("模型配置已改变，请恢复本任务的模型配置后重试，或新建任务")
        if not config.api_key.get_secret_value():
            raise BidCheckError("模型密钥未配置，请配置密钥后重试")
        return config

    def _check_interrupted(self, task_id):
        if self._shutdown.is_set() or self.store.raw(task_id)["stop_requested"]:
            raise TaskInterrupted()

    async def _retry_wait(self, task_id, delay):
        until = time.monotonic() + delay
        while time.monotonic() < until:
            self._check_interrupted(task_id)
            await asyncio.sleep(min(0.25, max(0, until - time.monotonic())))

    async def _generate(self, task_id, phase, input_text, instructions, schema, images=None):
        # Schema is also in the prompt for compatible providers without json_schema support.
        instructions += "\n输出 JSON schema：" + json.dumps(schema, ensure_ascii=False)
        started = time.perf_counter()
        attempts = len(self.retry_delays) + 1
        for attempt in range(1, attempts + 1):
            self._check_interrupted(task_id)
            config = self._configuration(self.store.raw(task_id))
            diagnostic = {"phase": phase, "attempt": attempt, "max_attempts": attempts, "retrying": False}
            self.store.update(task_id, diagnostic=json.dumps(diagnostic))
            try:
                output = await self.app.state.ai_client.generate(
                    config, input_text, instructions=instructions, schema=schema,
                    images=images, timeout=120, max_output_tokens=8192,
                )
                diagnostic["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
                self.store.update(task_id, diagnostic=json.dumps(diagnostic))
                return output
            except AIModelError as exc:
                diagnostic.update(status_code=exc.status_code, category=exc.category, retryable=exc.retryable,
                                  elapsed_ms=round((time.perf_counter() - started) * 1000))
                logger.warning("bid_model_failure task=%s phase=%s attempt=%s http_status=%s category=%s elapsed_ms=%s",
                               task_id, phase, attempt, exc.status_code, exc.category, diagnostic["elapsed_ms"])
                retry = exc.retryable and attempt < attempts
                diagnostic["retrying"] = retry
                if retry:
                    diagnostic["retry_in_seconds"] = self.retry_delays[attempt - 1]
                self.store.update(task_id, diagnostic=json.dumps(diagnostic))
                if not retry:
                    if attempt > 1:
                        raise AIModelError(f"{exc}；已尝试 {attempt} 次，可稍后手动重试", status_code=exc.status_code,
                                           category=exc.category, retryable=exc.retryable) from None
                    raise
                await self._retry_wait(task_id, self.retry_delays[attempt - 1])

    async def process_task(self, task_id):
        if not self.store.claim(task_id):
            return
        task = self.store.raw(task_id)
        try:
            self._configuration(task)
            if not task["profile"]:
                self.store.update(task_id, diagnostic=json.dumps({"phase": "documents"}))
                sources, images = await asyncio.to_thread(extract_documents, task["files"], self.store.root / task_id)
                text = json.dumps({"sources": sources}, ensure_ascii=False)
                output = await self._generate(task_id, "company_profile", text, COMPANY_INSTRUCTIONS, CompanyProfile.model_json_schema(), images)
                profile = validate_profile(parse_json_output(output, CompanyProfile), sources)
                self.store.update(task_id, profile=profile.model_dump_json(), sources=json.dumps(sources, ensure_ascii=False))
                self.clean_files(task_id)
            else:
                profile = CompanyProfile.model_validate(task["profile"])
            if task.get('rules_snapshot') and not task.get('evidence_index'):
                current = self.store.raw(task_id)
                index = build_evidence_index(profile.model_dump(), current.get('sources') or [])
                self.store.update(task_id, evidence_index=json.dumps(index,ensure_ascii=False))
            self.store.update(task_id, status="running")
            while not self._shutdown.is_set():
                current = self.store.raw(task_id)
                if current["stop_requested"]:
                    self.store.update(task_id, status="stopped")
                    self.clean_files(task_id)
                    return
                item = self.store.next_item(task_id)
                if item is None:
                    self.store.update(task_id, status="completed", diagnostic="{}")
                    self.clean_files(task_id)
                    return
                self.store.item_update(task_id, item["position"], "running")
                try:
                    self._configuration(current)
                    self.store.update(task_id, diagnostic=json.dumps({"phase": "announcement_read"}))
                    rules = current.get('rules_snapshot')
                    if rules:
                        snapshot = item.get('notice_snapshot')
                        if snapshot is None:
                            if hasattr(self.reader, 'read_snapshot'):
                                snapshot = await self.reader.read_snapshot(item['notice']['canonical_url'])
                            else:
                                snapshot = notice_snapshot(await self.reader.read(item['notice']['canonical_url']))
                            self._check_interrupted(task_id)
                            self.store.save_notice_snapshot(task_id, item['position'], snapshot)
                        input_text = json.dumps({'notice':item['notice'], 'announcement':snapshot['body'],
                            'clause_index':snapshot['clauses'], 'relation_index':snapshot['groups'],
                            'company_evidence':current['evidence_index'],
                            'company_profile':profile.model_dump(), 'knowledge_context':knowledge_context_for_model(current.get('knowledge_context',{}))}, ensure_ascii=False)
                        output = await self._generate(task_id, 'qualification_check', input_text, rules['instructions'], ScreeningOutput.model_json_schema())
                        self._check_interrupted(task_id)
                        self.store.update(task_id, diagnostic=json.dumps({'phase':'qualification_validate'}))
                        decision = evaluate_screening(parse_json_output(output, ScreeningOutput), snapshot, current['evidence_index'])
                        self.store.item_update(task_id, item['position'], decision['verdict'], decision=decision)
                    else:
                        announcement = await self.reader.read(item['notice']['canonical_url'])
                        input_text = json.dumps({'notice': item['notice'], 'announcement': announcement, 'company_profile': profile.model_dump(),'knowledge_context':knowledge_context_for_model(current.get('knowledge_context',{}))}, ensure_ascii=False)
                        output = await self._generate(task_id, 'qualification_check', input_text, BID_INSTRUCTIONS, BidDecision.model_json_schema())
                        unsafe = [source['source_id'] for source in current.get('sources') or [] if source.get('unsafe_for_conflict')]
                        decision = validate_decision(parse_json_output(output, BidDecision), profile, announcement, unsafe)
                        from .bid_review import REVIEW_SCOPE_VERSION
                        self.store.item_update(task_id, item['position'], decision.verdict, decision={**decision.model_dump(), 'review_scope_version': REVIEW_SCOPE_VERSION})
                except (BidCheckError, AIModelError) as exc:
                    self.store.item_update(task_id, item["position"], "failed", error=str(exc))
                    if (isinstance(exc, AIModelError) and (exc.retryable or exc.category in {"authentication", "billing", "rate_limit", "model", "compatibility"})) or any(term in str(exc) for term in ("认证失败", "限流", "密钥", "模型配置已改变", "模型不存在", "不兼容")):
                        self.store.update(task_id, status="failed", error=str(exc))
                        return
                except TaskInterrupted:
                    self.store.item_update(task_id, item["position"], "pending")
                    raise
                except Exception:
                    self.store.item_update(task_id, item["position"], "failed", error="本条处理异常，未完成判断，可重试")
                if self.interval:
                    await asyncio.to_thread(self._shutdown.wait, self.interval)
        except TaskInterrupted:
            if self.store.raw(task_id)["stop_requested"]:
                self.store.update(task_id, status="stopped")
                self.clean_files(task_id)
            # Otherwise leave it recoverable on service restart.
        except (BidCheckError, AIModelError) as exc:
            if self.store.raw(task_id)["stop_requested"]:
                self.store.update(task_id, status="stopped")
                self.clean_files(task_id)
            else:
                self.store.update(task_id, status="failed", error=str(exc))
            # Retain uploads for retry until a profile succeeds or the user cancels.
        except Exception:
            self.store.update(task_id, status="failed", error="公司资料整理或任务处理失败，请重新提交资料")
            # Keep original uploads available for a manual retry or explicit cancellation.
