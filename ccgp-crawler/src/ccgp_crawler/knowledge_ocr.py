"""Per-generation OCR preparation; no cross-file hash cache or image copies."""
from __future__ import annotations
import asyncio
import json
from .knowledge_documents import image_bytes
from .ocr_client import OCRError


def pack_units(units):
    batches=[];current=[];chars=0
    for unit in units:
        if current and (len(current)==6 or chars+len(unit['text'])>20000):
            batches.append(current);current=[];chars=0
        current.append(unit);chars+=len(unit['text'])
    if current:batches.append(current)
    return batches or [[]]


def split_text(unit,text,*,ocr=False,context_only=False):
    result=[]
    for offset in range(0,len(text),4000):
        value=text[offset:offset+4000]
        suffix=f' · OCR字符{offset+1}—{offset+len(value)}' if ocr and len(text)>4000 else ' · OCR识别' if ocr else ''
        result.append({**unit,'source_id':unit['source_id']+('.ocr' if ocr else '.context')+str(offset//4000+1),
            'location':unit['location']+suffix,'text':value,'visual':None,'segments':[],
            'ocr':ocr,'ocr_text':value if ocr else '', 'context_only':context_only})
    return result


async def prepare_ocr(manager,run):
    snapshot=(run.get('config') or {}).get('ocr')
    if not snapshot or not snapshot.get('enabled'):return
    store=manager.knowledge;run_id=run['id'];pending=store.unprepared_batches(run_id)
    if not any(u.get('visual') for b in pending for u in b['units']):return
    config=manager.app.state.ocr_config_store.get()
    if any(getattr(config,k)!=snapshot[k] for k in ('enabled','provider','model')):
        raise OCRError('OCR 配置已改变，请恢复本次拆解的配置后继续',category='configuration',fatal=True)
    if not config.access_token.get_secret_value():
        raise OCRError('请先配置 OCR Access Token 后继续',category='authentication',fatal=True)
    store.initialize_ocr(run_id,(u for batch in pending for u in batch['units']))
    def report(location='',**values):
        previous=json.loads(store.raw_run(run_id)['diagnostic'])
        summary=store.ocr_summary(run_id)
        store.update_run(run_id,diagnostic=json.dumps({**previous,**values,'phase':'ocr','ocr_total':summary['total'],
            'ocr_processed':summary['processed'],'ocr_failed':summary['failed'],'ocr_current_source':location},ensure_ascii=False))
    report()
    while True:
        pending=store.unprepared_batches(run_id)
        if not pending:break
        batch=pending[0]
        transformed=[];limitations=[]
        for unit in batch['units']:
            manager._check_interrupted(run_id)
            if not unit.get('visual'):
                transformed.append(unit);continue
            row=store.ocr_result(run_id,unit)
            if row['status'] not in {'done','empty','failed'}:
                report(unit['location'])
                try:
                    image=None if row['job_id'] else await asyncio.to_thread(image_bytes,unit['visual'])
                    manager._check_interrupted(run_id)
                    def save_job(job):store.save_ocr(run_id,unit['source_id'],job_id=job,status='running')
                    result=await manager.app.state.ocr_client.recognize(config,image,job_id=row['job_id'],
                        save_job=save_job,check=lambda:manager._check_interrupted(run_id),
                        progress=lambda **kw:report(unit['location'],**kw))
                    state='done' if result['text'].strip() else 'empty'
                    store.save_ocr(run_id,unit['source_id'],status=state,text=result['text'],scores=result['scores'],
                        error='' if state=='done' else '未识别出文字，可能为无文字图片或文字不清')
                except OCRError as exc:
                    if exc.fatal:
                        store.save_ocr(run_id,unit['source_id'],error=str(exc));raise
                    store.save_ocr(run_id,unit['source_id'],status='failed',error=str(exc))
                row=store.ocr_result(run_id,unit);report(unit['location'])
            # Neighboring text is context, not proof of what an unreadable image contains.
            if unit['text']:
                transformed.extend(split_text(unit,unit['text'],context_only=unit['visual']['kind']=='docx'))
            if row['status']=='done':
                transformed.extend(split_text(unit,row['text'],ocr=True))
            else:
                limitations.append(f"{unit['location']}：OCR 待核实（{row['error']}）；不能视为材料缺失或资格不符")
        manager._check_interrupted(run_id)
        store.prepare_batch(run_id,batch['position'],pack_units(transformed),limitations)
    report()
