from __future__ import annotations
import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from .bid_service import BidCheckManager, TaskInterrupted
from .bid_models import BidCheckError, parse_json_output
from .ai_client import AIModelError
from .ocr_client import OCRError
from .knowledge_ocr import prepare_ocr
from .knowledge_documents import index_document,batch_images
from .knowledge_models import EXTRACTION_INSTRUCTIONS,ExtractionOutput,validate_extraction
from .knowledge_summary_service import KnowledgeSummaryManager

class GenerationStore:
    """Use the shared bounded retry policy with knowledge run persistence."""
    def __init__(self,store):self.store=store
    def raw(self,run_id):
        raw=self.store.raw_run(run_id)
        return {**raw,'model_config':raw['config'] or {}}
    def update(self,run_id,**fields):
        if 'diagnostic' in fields:
            previous=json.loads(self.store.raw_run(run_id)['diagnostic'])
            fields['diagnostic']=json.dumps({**previous,**json.loads(fields['diagnostic'])},ensure_ascii=False)
        self.store.update_run(run_id,**fields)
    def recover(self):self.store.recover()

class KnowledgeManager(BidCheckManager):
    def __init__(self,store,app):
        super().__init__(GenerationStore(store),app)
        self.knowledge=store
        self.summary_manager=KnowledgeSummaryManager(store,app)
        self.summary_manager._shutdown=self._shutdown
    def _run(self):
        while not self._shutdown.is_set():
            try:
                run_id=self.knowledge.work()
                if run_id:
                    asyncio.run(self.process_run(run_id));continue
                summary_id=self.knowledge.summary_work()
                if summary_id:
                    asyncio.run(self.summary_manager.process_summary(summary_id));continue
            except Exception:
                self._shutdown.wait(2)
            self._wake.wait(1);self._wake.clear()
    async def process_run(self,run_id):
        run=self.knowledge.raw_run(run_id)
        status=run['status']
        if not self.knowledge.claim(run_id,status):return
        index_work=None
        index_committed=False
        try:
            if status=='indexing':
                work_root=Path(run['path']).parent/'work';work_root.mkdir(mode=0o700,exist_ok=True)
                work=Path(tempfile.mkdtemp(prefix='index-',dir=work_root));index_work=work
                self.knowledge.update_run(run_id,diagnostic=json.dumps({'phase':'indexing'}))
                def progress(**values):
                    previous=json.loads(self.knowledge.raw_run(run_id)['diagnostic'])
                    nested=values.pop('stats',{})
                    self.knowledge.update_run(run_id,diagnostic=json.dumps({**previous,**nested,**values},ensure_ascii=False))
                pages,batches=await asyncio.to_thread(index_document,Path(run['path']),run['name'],work,progress=progress,check=lambda:self._check_interrupted(run_id))
                self._check_interrupted(run_id)
                for path in work.rglob('*'):
                    os.chmod(path,0o700 if path.is_dir() else 0o600)
                self.knowledge.set_batches(run_id,pages,batches)
                index_committed=True
                if not any(Path(u['visual']['path']).is_relative_to(work) for b in batches for u in b if u.get('visual')):
                    shutil.rmtree(work,ignore_errors=True)
                return
            self._configuration(self.store.raw(run_id))
            await prepare_ocr(self,run)
            company=run['config'].get('company_name') or self.knowledge.company()['name']
            while not self._shutdown.is_set():
                self._check_interrupted(run_id)
                batch=self.knowledge.next_batch(run_id)
                if batch is None:
                    self.knowledge.finish_run(run_id)
                    self._queue_completed_summary(run)
                    return
                position=batch['position'];units=batch['units']
                self.knowledge.batch_state(run_id,position,'running')
                try:
                    images=await asyncio.to_thread(batch_images,units)
                    if not units:
                        self.knowledge.finish_batch(run_id,position,ExtractionOutput(entries=[],limitations=[]),[])
                        continue
                    input_text=json.dumps({'company_name':company,'filename':run['name'],'sources':[{'source_id':u['source_id'],'location':u['location'],'text':u['text'],'image':bool(u['visual']),'ocr':bool(u.get('ocr')),'context_only':bool(u.get('context_only')),'segments':[{k:v for k,v in segment.items() if k!='text'} for segment in u.get('segments',[])]} for u in units]},ensure_ascii=False)
                    instructions=EXTRACTION_INSTRUCTIONS
                    if run['config'].get('knowledge_skill'):instructions+='\n'+run['config']['knowledge_skill']['instructions']
                    text=await self._generate(run_id,'extracting',input_text,instructions,ExtractionOutput.model_json_schema(),images)
                    output=validate_extraction(parse_json_output(text,ExtractionOutput),units,company)
                    self.knowledge.finish_batch(run_id,position,output,units)
                    del images
                except TaskInterrupted:
                    self.knowledge.batch_state(run_id,position,'pending');raise
                except (AIModelError,BidCheckError) as exc:
                    self.knowledge.batch_state(run_id,position,'failed',str(exc))
                    if isinstance(exc,AIModelError):
                        self.knowledge.update_run(run_id,status='failed',error=str(exc))
                        if exc.category not in {'authentication','billing','rate_limit'}:self._queue_completed_summary(run)
                        return
                except Exception:
                    self.knowledge.batch_state(run_id,position,'failed','本批处理异常，可重试')
                    self.knowledge.update_run(run_id,status='failed',error='本批处理异常，原件已保留，可重试')
                    return
                detail=self.knowledge.document(run['document_id'])['run']
                self.store.update(run_id,diagnostic=json.dumps({'phase':'extracting','completed_batches':detail['completed_batches']}))
                if self.interval:await asyncio.to_thread(self._shutdown.wait,self.interval)
        except TaskInterrupted:
            if self.knowledge.raw_run(run_id)['stop_requested']:
                self.knowledge.update_run(run_id,status='stopped')
        except OCRError as exc:
            self.knowledge.update_run(run_id,status='stopped',error=str(exc))
        except (AIModelError,BidCheckError) as exc:
            diagnostic=json.loads(self.knowledge.raw_run(run_id)['diagnostic'])
            if status=='indexing' and not diagnostic.get('unreadable'):diagnostic['unreadable']=[str(exc)]
            self.knowledge.update_run(run_id,status='failed',error=str(exc),diagnostic=json.dumps(diagnostic,ensure_ascii=False))
        except Exception:
            self.knowledge.update_run(run_id,status='failed',error='文档拆解异常，原件已保留，可重试')

        finally:
            if index_work is not None and not index_committed:
                shutil.rmtree(index_work,ignore_errors=True)

    def _queue_completed_summary(self,run):
        if not run['config'].get('knowledge_skill'):return
        try:self.knowledge.queue_summary(run['id'],run['config'])
        except Exception:
            # A summary setup failure must never invalidate successful extraction batches.
            diagnostic=json.loads(self.knowledge.raw_run(run['id'])['diagnostic'])
            diagnostic['summary_error']='自动汇总未能启动，已拆解资料保留，可点击整理已有结果重试'
            self.knowledge.update_run(run['id'],diagnostic=json.dumps(diagnostic,ensure_ascii=False))
