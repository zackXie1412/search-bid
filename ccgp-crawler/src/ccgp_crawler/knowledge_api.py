from __future__ import annotations
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Literal
from fastapi import File, UploadFile, Query, HTTPException
from fastapi.responses import FileResponse,StreamingResponse,JSONResponse
from .knowledge_store import KnowledgeStorageError
from pydantic import BaseModel,Field,ValidationError,ConfigDict
from .bid_models import BidCheckError
from .bid_documents import ALLOWED_EXTENSIONS
from .knowledge_skill import skill_snapshot

class CompanyUpdate(BaseModel):
    name:str=Field(min_length=1,max_length=200)
class Action(BaseModel):
    action:Literal['start','stop','retry','reextract']
    expected_config:dict|None=None
    expected_ocr:dict|None=None
class ReviewItem(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:str
    status:Literal['accepted','rejected','pending']
    changes:dict=Field(default_factory=dict)
class Reviews(BaseModel):
    items:list[ReviewItem]=Field(min_length=1,max_length=500)
class Active(BaseModel):
    active:bool
class Ownership(BaseModel):
    confirmed:bool


def register_knowledge_routes(app,store,manager):
    @app.exception_handler(KnowledgeStorageError)
    async def storage_error(request,exc):
        return JSONResponse(status_code=503,content={'detail':str(exc)})
    def guard(fn):
        try:return fn()
        except KeyError:raise HTTPException(404,'知识库记录不存在') from None
        except (BidCheckError,ValidationError) as exc:
            raise HTTPException(422,str(exc) if isinstance(exc,BidCheckError) else '修改的信息格式无效') from None
    @app.get('/api/knowledge/company')
    def company():return guard(store.company)
    @app.put('/api/knowledge/company')
    def save_company(payload:CompanyUpdate):return guard(lambda:store.save_company(payload.name))
    @app.get('/api/knowledge/documents')
    def documents():return {'items':guard(store.documents)}
    @app.get('/api/knowledge/documents/{document_id}')
    def document(document_id:str):return guard(lambda:store.document(document_id))
    @app.post('/api/knowledge/documents')
    async def upload(files:list[UploadFile]=File(...)):
        if not store.company()['name']:raise HTTPException(422,'请先填写并确认本企业名称')
        staged=[];total=0
        try:
            # Stage and validate the entire request before adding records.
            for file in files:
                name=Path(file.filename or '').name;ext=Path(name).suffix.lower()[1:]
                if not name or ext not in ALLOWED_EXTENSIONS:raise HTTPException(422,'资料格式不支持')
                doc_id=uuid.uuid4().hex;directory=store.root/'documents'/doc_id;directory.mkdir(parents=True,mode=0o700)
                os.chmod(directory.parent,0o700)
                path=directory/f'original.{ext}';staged.append({'id':doc_id,'name':name,'path':path,'directory':directory})
                digest=hashlib.sha256();size=0
                fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                with os.fdopen(fd,'wb') as output:
                    while chunk:=await file.read(1024*1024):
                        size+=len(chunk);total+=len(chunk)
                        if size>300*1024*1024 or total>1024*1024*1024:raise HTTPException(422,'资料超过单文件 300 MB 或合计 1 GB 限制')
                        digest.update(chunk);output.write(chunk)
                if not size:raise HTTPException(422,'文件为空')
                staged[-1].update(size=size,hash=digest.hexdigest())
            results=[]
            for item in staged:
                doc_id,created=store.add_document(item['id'],item['name'],item['hash'],item['size'],item['path'])
                if not created:shutil.rmtree(item['directory'],ignore_errors=True)
                results.append({'document':store.document(doc_id),'duplicate':not created})
            manager.wake()
            return {'items':results}
        except Exception:
            for item in staged:
                if not store.find_hash(item.get('hash','')):shutil.rmtree(item['directory'],ignore_errors=True)
            raise
        finally:
            for file in files:await file.close()
    @app.get('/api/knowledge/documents/{document_id}/original')
    def original(document_id:str):
        path,name=guard(lambda:store.original_path(document_id))
        if not path.is_file():raise HTTPException(404,'原始文件不存在')
        return FileResponse(path,filename=name)
    @app.post('/api/knowledge/documents/{document_id}/action')
    def action(document_id:str,payload:Action):
        config=None
        needs_model=payload.action in {'start','retry'} and guard(lambda:store.document(document_id))['run']['total_batches']>0
        if needs_model:
            view=app.state.ai_config_store.view()
            config=view.model_dump(exclude={'api_key_configured'})
            if payload.expected_config!=config:raise HTTPException(409,'模型配置已改变，请重新确认后开始拆解')
            if not view.api_key_configured:raise HTTPException(422,'请先配置模型密钥')
            config['company_name']=store.company()['name']
            ocr=app.state.ocr_config_store.view()
            snapshot=ocr.model_dump(exclude={'access_token_configured'})
            if payload.expected_ocr is not None and payload.expected_ocr!=snapshot:raise HTTPException(409,'OCR 配置已改变，请重新确认后开始拆解')
            document=guard(lambda:store.document(document_id))
            prior=document['run']['config'] or {}
            if prior.get('ocr') and (document['run']['ocr']['total'] or document['run']['completed_batches']) and prior['ocr']!=snapshot:
                raise HTTPException(409,'OCR 配置已改变，请恢复本次拆解的 OCR 设置后继续，或重新拆解')
            if ocr.enabled and document['run']['pending_images'] and not ocr.access_token_configured:raise HTTPException(422,'请先配置 OCR Access Token')
            config['ocr']=snapshot
            config['knowledge_skill']=(store.raw_run(document['run']['id'])['config'] or {}).get('knowledge_skill') or skill_snapshot()
        result=guard(lambda:store.action(document_id,payload.action,config));manager.wake();return result
    @app.put('/api/knowledge/documents/{document_id}/active')
    def active(document_id:str,payload:Active):return guard(lambda:store.active(document_id,payload.active))
    @app.put('/api/knowledge/documents/{document_id}/ownership')
    def ownership(document_id:str,payload:Ownership):return guard(lambda:store.ownership(document_id,payload.confirmed))
    @app.get('/api/knowledge/summaries')
    def summaries(document_id:str|None=None):return {'items':guard(lambda:store.summaries(document_id))}
    @app.get('/api/knowledge/summaries/{summary_id}')
    def summary(summary_id:str):return guard(lambda:store.summary_detail(summary_id))
    @app.post('/api/knowledge/documents/{document_id}/summarize')
    def summarize(document_id:str,payload:Action):
        if payload.action not in {'start','retry'}:raise HTTPException(422,'整理已有结果仅支持开始')
        view=app.state.ai_config_store.view()
        config=view.model_dump(exclude={'api_key_configured'})
        if payload.expected_config!=config:raise HTTPException(409,'模型配置已改变，请重新确认后整理')
        if not view.api_key_configured:raise HTTPException(422,'请先配置模型密钥')
        doc=guard(lambda:store.document(document_id))
        if doc['run']['status'] in {'running','queued','stopping','indexing','indexing_running'}:raise HTTPException(422,'请先停止文档拆解，再整理已有结果')
        config.update(company_name=store.company()['name'],knowledge_skill=skill_snapshot())
        summary_id=guard(lambda:store.queue_summary(doc['run']['id'],config))
        if not summary_id:raise HTTPException(422,'暂无成功拆解记录可整理')
        manager.wake();return store.summary_detail(summary_id)
    @app.post('/api/knowledge/summaries/{summary_id}/action')
    def summary_action(summary_id:str,payload:Action):
        if payload.action in {'start','retry'}:
            view=app.state.ai_config_store.view();config=view.model_dump(exclude={'api_key_configured'})
            prior=guard(lambda:store.summary_raw(summary_id))['config']
            if payload.expected_config!=config or any(prior.get(k)!=config[k] for k in ('base_url','model','protocol')):raise HTTPException(409,'请恢复汇总所用模型配置后继续')
            if not view.api_key_configured:raise HTTPException(422,'请先配置模型密钥')
        result=guard(lambda:store.summary_action(summary_id,payload.action));manager.wake();return result
    @app.post('/api/knowledge/summaries/review')
    def summary_review(payload:Reviews):return guard(lambda:store.summary_review([v.model_dump() for v in payload.items]))
    @app.get('/api/knowledge/entries')
    def entries(document_id:str|None=None,status:Literal['pending','accepted','rejected']|None=None,q:str=Query('',max_length=200),category:str|None=None,page:int=Query(1,ge=1),view:Literal['review','trace']='review'):
        return guard(lambda:store.entries(document_id,status,q,category,page,view))
    @app.post('/api/knowledge/review')
    def review(payload:Reviews):return guard(lambda:store.review([v.model_dump() for v in payload.items]))
    @app.get('/api/knowledge/revisions/{revision_id}')
    def revision(revision_id:str):return guard(lambda:store.revision(revision_id))
    @app.get('/api/knowledge/backup')
    def backup():
        # SQLite backup obtains a consistent snapshot; originals are immutable.
        import sqlite3,tempfile,zipfile
        def stream():
            with tempfile.TemporaryDirectory(prefix='knowledge-backup-') as temp:
                dest=Path(temp)/'knowledge.sqlite3'
                with store.connect() as db:
                    with sqlite3.connect(dest) as output:db.backup(output)
                    paths=[r[0] for r in db.execute('SELECT path FROM documents')]
                archive=Path(temp)/'knowledge.zip'
                with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
                    z.write(dest,'knowledge.sqlite3')
                    for raw in paths:
                        path=Path(raw)
                        if path.is_file():z.write(path,str(path.relative_to(store.root)))
                with archive.open('rb') as f:
                    while chunk:=f.read(1024*1024):yield chunk
        return StreamingResponse(stream(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="knowledge-backup.zip"'})
