import asyncio
import json
from .bid_service import BidCheckManager,TaskInterrupted
from .bid_models import BidCheckError,parse_json_output
from .ai_client import AIModelError
from .knowledge_summary_models import SummaryOutput,reconcile_summary
from .knowledge_models import reference_only

class SummaryAdapter:
    def __init__(self,store):self.store=store
    def raw(self,id):
        raw=self.store.summary_raw(id)
        return {**raw,'model_config':raw['config']}
    def update(self,id,**fields):self.store.summary_update(id,**fields)
    def recover(self):pass

class KnowledgeSummaryManager(BidCheckManager):
    def __init__(self,store,app):
        super().__init__(SummaryAdapter(store),app);self.knowledge=store
    async def _generate(self,*args,**kwargs):
        task=asyncio.create_task(super()._generate(*args,**kwargs))
        try:
            while not task.done():
                await asyncio.wait({task},timeout=0.2)
                self._check_interrupted(args[0])
            return await task
        finally:
            if not task.done():
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass

    async def process_summary(self,summary_id):
        store=self.knowledge
        if not store.summary_claim(summary_id):return
        raw=store.summary_raw(summary_id);part=None
        try:
            self._configuration(self.store.raw(summary_id))
            while True:
                self._check_interrupted(summary_id)
                part=store.summary_next_part(summary_id)
                if not part:break
                self._configuration(self.store.raw(summary_id))
                try:
                    await self._process_part(summary_id,raw,part)
                except TaskInterrupted:
                    raise
                except (AIModelError,BidCheckError) as exc:
                    store.summary_fail_part(summary_id,part['position'],str(exc))
                    if isinstance(exc,AIModelError) and exc.category in {'authentication','billing','rate_limit','model','compatibility'}:
                        raise
                    # Invalid output/evidence or an exhausted temporary failure is local
                    # to this part. Keep the gap visible and process later parts once.
                except Exception:
                    store.summary_fail_part(summary_id,part['position'],'本组汇总异常，未生成有效结果，可单独重试')
            self._check_interrupted(summary_id)
            store.summary_commit(summary_id)
        except TaskInterrupted:
            store.summary_update(summary_id,status='stopped' if store.summary_raw(summary_id)['stop_requested'] else 'queued')
        except (AIModelError,BidCheckError) as exc:
            store.summary_update(summary_id,status='failed',error=str(exc))
        except Exception:
            store.summary_update(summary_id,status='failed',error='材料汇总异常，已完成部分保留，可重试')

    async def _process_part(self,summary_id,raw,part):
        store=self.knowledge
        saved={value['id']:value for value in raw['inputs']}
        model_inputs=[value for value in part['inputs'] if not reference_only(value['data'])]
        inputs=[{**value,'original_quote':saved[value['id']]['source']['quote']} for value in model_inputs]
        clues=[value['id'] for value in part['inputs'] if reference_only(value['data'])]
        if not inputs:
            store.summary_finish_part(summary_id,part['position'],SummaryOutput(records=[],ignored_entry_ids=clues))
            return
        text=json.dumps({'company_name':raw['config'].get('company_name',''),'ownership':raw['config'].get('ownership',{}),'records':model_inputs},ensure_ascii=False)
        instructions=raw['skill']['instructions']+'\n本次为跨批次汇总。完整处理 records，只返回 SummaryOutput JSON。各条 evidence 引用输入 entry_id（输入 id），quote 匹配 text。目录和空白模板仅由后端关联，不生成独立候选；不从原文目录行推断实际持有材料。'
        instructions+='\n能力声明优先引用 original_quote 中的正文句。仅有标题时保留材料记录，能力写 inference/unknown 并标 needs_review，不宣称已明确声明。'
        instructions+='\nquote 必须复制对应 entry_id 的 text 中连续原文，不写归纳结论，不改写 OCR 错别字、标点、金额或日期。跨来源分别列 evidence，不能拼成同一句引用。'
        output=await self._generate(summary_id,'summarizing',text,instructions,SummaryOutput.model_json_schema())
        ids={value['id'] for value in inputs}
        parsed,diagnostic=reconcile_summary(parse_json_output(output,SummaryOutput),inputs,[value for value in raw['inputs'] if value['id'] in ids])
        parsed.ignored_entry_ids.extend(clues)
        self._check_interrupted(summary_id)
        store.summary_finish_part(summary_id,part['position'],parsed)
        previous=store.summary_raw(summary_id)['diagnostic']
        store.summary_update(summary_id,diagnostic=json.dumps({**previous,'part':part['position']+1,**diagnostic},ensure_ascii=False))
