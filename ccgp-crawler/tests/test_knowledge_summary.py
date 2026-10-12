"""Summary jobs use mock models; no uploaded evidence is sent to cloud services."""
import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from ccgp_crawler.ai_client import AIModelError
from ccgp_crawler.bid_models import BidCheckError,contains_quote
from ccgp_crawler.knowledge_models import ExtractionOutput,validate_extraction
from ccgp_crawler.knowledge_summary_models import SummaryOutput,validate_summary,original_quote_match
from ccgp_crawler.knowledge_skill import skill_snapshot
from test_bid_check import app,seed,FakeReader
from test_knowledge import CONFIG,entry,setup_document,extract,KnowledgeAI,TEXT,confirm


def record(inputs,**changes):
    d=inputs[0]['data']
    return {**{k:d[k] for k in ('kind','category','name','value','subject','occurrence','evidence_kind','applicable_date','industry','notes','needs_review')},
            'subject_role':d.get('subject_role','supplier'),'evidence':[{'entry_id':i['id'],'quote':i['text']} for i in inputs],
            'certificates':[],**changes}

class SummaryAI:
    def __init__(self,outputs=None):self.calls=[];self.outputs=list(outputs or [])
    async def generate(self,config,input_text,**kwargs):
        data=json.loads(input_text);self.calls.append((data,kwargs))
        if self.outputs:
            output=self.outputs.pop(0)
            if isinstance(output,Exception):raise output
            if callable(output):output=output(data['records'])
        else:output={'records':[record([i]) for i in data['records']],'ignored_entry_ids':[]}
        return json.dumps(output,ensure_ascii=False)


def make(app,client,values,texts=None):
    doc=setup_document(app,client)
    store=app.state.knowledge_store;rid=doc['run']['id']
    units=[{'source_id':f'S{i+1}','location':f'正文段落 {i+1}','text':text,'visual':None} for i,text in enumerate(texts or [v.get('quote',TEXT) for v in values])]
    store.set_batches(rid,len(units),[[u] for u in units])
    for i,(value,unit) in enumerate(zip(values,units)):
        store.finish_batch(rid,i,ExtractionOutput.model_validate({'entries':[{**entry(),**value,'source_id':unit['source_id']}],'limitations':['本批未发现设备证明']}),[unit])
    store.finish_run(rid)
    return store.document(doc['id'])


def start(app,client,doc):
    response=client.post(f"/api/knowledge/documents/{doc['id']}/summarize",json={'action':'start','expected_config':CONFIG})
    assert response.status_code==200,response.text
    return response.json()['id']

def process(app,id,ai=None):
    app.state.ai_client=ai or SummaryAI()
    app.state.knowledge_manager.summary_manager.retry_delays=(0,0)
    asyncio.run(app.state.knowledge_manager.summary_manager.process_summary(id))
    return app.state.knowledge_store.summary_detail(id)

def review(client,items,status='accepted',changes=None):
    r=client.post('/api/knowledge/summaries/review',json={'items':[{'id':v['id'],'status':status,'changes':changes or {}} for v in items]})
    assert r.status_code==200,r.text
    return r.json()['revision_id']


def test_directory_actual_merge_and_multiple_certificates_and_snapshot(app):
    seed(app,count=1)
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',category='证照资质',name='营业执照',occurrence='mention',quote='目录：营业执照'),
                        entry(kind='material',category='证照资质',name='营业执照副本',quote='营业执照 编号 A001 日期 2020年'),
                        entry(kind='material',category='证照资质',name='营业执照',quote='营业执照 编号 B002 日期 2021年')])
        c.put(f"/api/knowledge/documents/{doc['id']}/ownership",json={'confirmed':True})
        def merge(inputs):
            return {'records':[record(inputs,kind='material',name='营业执照',value='已发现营业执照',occurrence='actual',evidence_kind='proof',subject_role='supplier',needs_review=False,notes='',
                certificates=[{'number':'A001','date':'2020年','entry_ids':[inputs[0]['id']]},{'number':'B002','date':'2021年','entry_ids':[inputs[1]['id']]}])],'ignored_entry_ids':[]}
        ai=SummaryAI([merge]);sid=start(app,c,doc)
        assert start(app,c,doc)==sid
        job=process(app,sid,ai);assert job['status']=='awaiting_review' and len(ai.calls)==1
        assert ai.calls[0][1]['images'] is None
        assert len(job['items'])==1 and len(job['items'][0]['sources'])==3
        assert len(job['items'][0]['data']['certificates'])==2
        assert 'instructions' not in job['skill'] and 'test-secret' not in json.dumps(job)
        assert c.get('/api/knowledge/company').json()['revision']['material_count']==0
        rid=review(c,job['items']);store=app.state.knowledge_store;version=store.revision(rid)
        assert len(version['material_support'])==1 and version['records'][0]['basic_support']
        assert not any('未发现' in v for v in version['profile']['limitations'])
        assert not any('仅材料' in v for v in version['profile']['limitations'])
        created=c.post('/api/bid-check/tasks',data={'knowledge_revision_id':rid,'expected_config':json.dumps(CONFIG)}).json()
        task=app.state.bid_store.raw(created['id'])
        assert task['knowledge_context']['material_support']==version['material_support']
        assert task['knowledge_context']['coverage']==version['coverage']
        c.put(f"/api/knowledge/documents/{doc['id']}/ownership",json={'confirmed':False})
        assert not c.get('/api/knowledge/company').json()['revision']['material_support']
        assert store.revision(rid)==version and app.state.bid_store.raw(created['id'])['knowledge_context']==task['knowledge_context']


def test_partial_gaps_dual_declaration_and_unrelated_raw_retained(app):
    with TestClient(app) as c:
        quote='测试公司声明具备履约设备和专业技术能力。'
        doc=make(app,c,[entry(kind='material',name='设备能力声明函',quote=quote),entry(name='民事责任能力')])
        store=app.state.knowledge_store;rid=doc['run']['id'];raw=store.entries()['items']
        confirm(c,raw)
        # Failed content remains explicit, separate from confirmed positive materials.
        with store.connect() as db:
            db.execute("INSERT INTO batches(run_id,position,units,status,error) VALUES (?,?,?,'failed','读取失败')",(rid,2,json.dumps([{'source_id':'S3','location':'图片3','text':'','visual':None}])))
            db.execute("INSERT INTO ocr_results(run_id,source_id,location,status,error,updated_at) VALUES (?,?,'图片3','empty','识别为空','now')",(rid,'S3'))
        store.update_run(rid,status='failed')
        c.put(f"/api/knowledge/documents/{doc['id']}/ownership",json={'confirmed':True})
        def dual(inputs):
            first=next(i for i in inputs if i['data']['kind']=='material')
            return {'records':[record([first],name='设备能力声明函',subject_role='supplier',needs_review=False),record([first],kind='fact',name='设备与技术能力',value='企业声明具备履约设备和专业技术能力',subject_role='supplier',needs_review=False)],'ignored_entry_ids':[i['id'] for i in inputs if i!=first]}
        job=process(app,start(app,c,doc),SummaryAI([dual]));v=store.revision(review(c,job['items']))
        assert v['material_count']==1 and v['fact_count']==2
        assert v['coverage'][0]['gaps'][0]['locations']==['图片3']
        assert v['coverage'][0]['ocr_gaps'][0]['error']=='识别为空'
        assert len(v['records'])==3  # adopted material replaced; unrelated confirmed fact survives


def test_scope_and_role_guards():
    units=[{'source_id':'S1','text':TEXT,'visual':None}]
    out=validate_extraction(ExtractionOutput.model_validate({'entries':[entry(value='本批未发现设备证明')],'limitations':[]}),units,'测试公司')
    assert not out.entries and '本批' in out.limitations[0]
    inputs=[{'id':'one','data':entry(occurrence='actual',subject_role='manufacturer'),'text':'许可证 已填写证明'}]
    for changes in [dict(subject_role='supplier'),dict(value='企业未提供设备证明'),dict(evidence=[{'entry_id':'one','quote':'伪造'}]),dict(certificates=[{'number':'bad','entry_ids':['one']}])]:
        with pytest.raises(BidCheckError):validate_summary(SummaryOutput.model_validate({'records':[record(inputs,**changes)]}),inputs)


def test_legacy_recovery_rejection_and_review_audit(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='营业执照',occurrence='third_party',subject='旧企业名',quote='营业执照 统一社会信用代码 A123'),entry(kind='material',name='厂家许可证',subject_role='manufacturer')])
        c.put(f"/api/knowledge/documents/{doc['id']}/ownership",json={'confirmed':True})
        sid=start(app,c,doc)
        def recover(inputs):
            return {'records':[record([i],occurrence='actual',subject_role='supplier' if i['data']['name']=='营业执照' else 'manufacturer',evidence_kind='proof',value='有'+i['data']['name']) for i in inputs],'ignored_entry_ids':[]}
        job=process(app,sid,SummaryAI([recover]));rid=review(c,job['items'],changes={'notes':'人工核对记录'})
        v=app.state.knowledge_store.revision(rid)
        assert len(v['material_support'])==1
        assert all(i['original']['notes']!=i['data']['notes'] for i in app.state.knowledge_store.summary_detail(sid)['items'])
        raw=app.state.knowledge_store.entries(view='trace')['items']
        c.post('/api/knowledge/review',json={'items':[{'id':raw[0]['id'],'status':'rejected'}]})
        assert c.post('/api/knowledge/summaries/review',json={'items':[{'id':job['items'][0]['id'],'status':'accepted'}]}).status_code==422
        nextsid=start(app,c,doc);assert nextsid!=sid
        snapshot=app.state.knowledge_store.summary_raw(nextsid)
        assert all(i['id']!=raw[0]['id'] for i in snapshot['inputs'])
        assert app.state.knowledge_store.revision(rid)==v


def test_stop_resume_retry_completed_parts_and_skill_snapshot(app):
    with TestClient(app) as c:
        values=[entry(name='能力'+str(i),quote=TEXT+('详述'*1900)) for i in range(5)]
        doc=make(app,c,values)
        sid=start(app,c,doc);store=app.state.knowledge_store
        assert store.summary_detail(sid)['total_parts']>1
        version=store.summary_raw(sid)['skill']
        ai=SummaryAI([lambda inputs:{'records':[record([i]) for i in inputs],'ignored_entry_ids':[]},AIModelError('认证失败',category='authentication')])
        job=process(app,sid,ai);assert job['status']=='failed' and job['completed_parts']==1
        before=len(ai.calls)
        res=c.post(f'/api/knowledge/summaries/{sid}/action',json={'action':'retry','expected_config':CONFIG});assert res.status_code==200
        job=process(app,sid,ai);assert job['status']=='awaiting_review' and len(ai.calls)-before==job['total_parts']-1
        assert store.summary_raw(sid)['skill']==version
        assert store.summary_next_part(sid) is None
        assert start(app,c,doc)==sid


def test_stop_cancels_active_summary_request_and_recovery(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry()]);sid=start(app,c,doc);store=app.state.knowledge_store
        class Blocking:
            cancelled=False
            async def generate(self,*args,**kwargs):
                store.summary_action(sid,'stop')
                try:await asyncio.sleep(10)
                except asyncio.CancelledError:self.cancelled=True;raise
        ai=Blocking();job=process(app,sid,ai)
        assert ai.cancelled and job['status']=='stopped' and job['completed_parts']==0
        store.summary_action(sid,'start');assert process(app,sid)['status']=='awaiting_review'
        store.summary_update(sid,status='running');store.recover()
        assert store.summary_raw(sid)['status']=='queued'


def test_automatic_summary_queue_error_does_not_invalidate_extraction(app,monkeypatch):
    with TestClient(app) as c:
        doc=setup_document(app,c)
        def broken(*args):raise BidCheckError('汇总超限')
        monkeypatch.setattr(app.state.knowledge_store,'queue_summary',broken)
        items=extract(app,c,doc)
        run=app.state.knowledge_store.document(doc['id'])['run']
        assert run['status']=='awaiting_review' and run['completed_batches']==1
        assert run['diagnostic']['summary_error'] and items[0]['reviewable']


def test_auto_summary_bounded_long_text_and_manual_stop(app):
    with TestClient(app) as c:
        doc=setup_document(app,c,TEXT*2000);extract(app,c,doc)
        store=app.state.knowledge_store;summary=store.document(doc['id'])['summary']
        assert summary and summary['status']=='queued'
        with store.connect() as db:
            for row in db.execute('SELECT inputs FROM summary_parts WHERE summary_id=?',(summary['id'],)):
                assert len(row[0])<=19000
        # A manual interruption should not start a new AI summary.
        other=setup_document(app,c,TEXT+'追加资料')
        ai=KnowledgeAI(stop=lambda:store.action(other['id'],'stop'))
        extract(app,c,other,ai)
        assert store.document(other['id'])['summary'] is None


def test_human_summary_edits_rejections_survive_new_coverage_and_raw_edits(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='营业执照'),entry(kind='material',name='许可证')]);store=app.state.knowledge_store
        first=process(app,start(app,c,doc));kept,rejected=first['items']
        review(c,[kept],changes={'name':'人工材料名','value':'人工确认有此材料','notes':'人工备注'})
        review(c,[rejected],status='rejected')
        with store.connect() as db:
            db.execute("INSERT INTO batches(run_id,position,units,status) VALUES (?,?,?,'pending')",(doc['run']['id'],2,json.dumps([{'source_id':'S3','location':'第3页','text':'','visual':None}])))
        nextjob=process(app,start(app,c,doc));assert len(nextjob['items'])==1
        assert nextjob['items'][0]['data']['value']=='人工确认有此材料'
        version=store.revision(review(c,nextjob['items']))
        assert len(version['records'])==1 and version['records'][0]['data']['name']=='人工材料名'
        raw=next(e for e in store.entries(view='trace')['items'] if e['id']==nextjob['items'][0]['entry_ids'][0])
        confirm(c,[raw])
        c.post('/api/knowledge/review',json={'items':[{'id':raw['id'],'status':'accepted','changes':{'value':'人工再次修正原始记录'}}]})
        current=c.get('/api/knowledge/company').json()['revision']
        assert current['records'][0]['data']['value']=='人工再次修正原始记录'
        assert len(current['records'])==1
        result=c.post('/api/knowledge/summaries/review',json={'items':[{'id':nextjob['items'][0]['id'],'status':'accepted'}]})
        assert result.status_code==422 and '修改' in result.json()['detail']


def test_cross_part_alias_merge_does_not_merge_manufacturers(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='营业执照'),entry(kind='material',name='营业执照副本'),entry(kind='material',name='营业执照',subject_role='manufacturer',subject='厂家')])
        sid=start(app,c,doc);store=app.state.knowledge_store
        raw=store.summary_raw(sid)
        # Emulate packing into multiple requests to verify backend final merge.
        from ccgp_crawler.knowledge_summary_store import prompt_input,dumps
        with store.connect() as db:
            db.execute('DELETE FROM summary_parts WHERE summary_id=?',(sid,))
            for i,v in enumerate(raw['inputs']):db.execute('INSERT INTO summary_parts(summary_id,position,inputs) VALUES (?,?,?)',(sid,i,dumps([prompt_input(v)])))
        job=process(app,sid);assert job['status']=='awaiting_review' and len(job['items'])==2
        assert sorted(len(i['sources']) for i in job['items'])==[1,2]


def test_legacy_unknown_is_not_automatically_actual():
    inputs=[{'id':'one','data':entry(occurrence='third_party'),'text':'营业执照'}]
    result=validate_summary(SummaryOutput.model_validate({'records':[record(inputs,occurrence='actual')]}),inputs)
    assert result.records[0].occurrence=='unknown' and result.records[0].needs_review


def test_conflicting_facts_remain_parallel_in_summary_revision(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(value='企业声明具有能力',quote='企业声明具有能力'),entry(value='企业声明不具有能力',quote='企业声明不具有能力')])
        job=process(app,start(app,c,doc));v=app.state.knowledge_store.revision(review(c,job['items']))
        assert v['fact_count']==2 and all(r['conflict'] for r in v['records'])


def test_material_title_alone_never_becomes_capability():
    inputs=[{'id':'one','data':entry(kind='material',name='设备能力声明函'),'text':'设备能力声明函'}]
    result=validate_summary(SummaryOutput.model_validate({'records':[record(inputs,kind='fact',value='具备设备能力')]}),inputs)
    fact=result.records[0]
    assert fact.evidence_kind=='inference' and fact.occurrence=='unknown' and fact.needs_review
    assert '能力待核实' in fact.notes


def test_directory_and_template_are_saved_clues_not_candidates_or_calls(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='变更登记通知书',occurrence='mention',quote='9.2变更登记通知书\t14'),
                        entry(kind='material',name='设备能力声明函',occurrence='template',quote='声明人：____')])
        store=app.state.knowledge_store
        assert store.entries()['total']==0
        sid=start(app,c,doc);ai=SummaryAI();job=process(app,sid,ai)
        assert not ai.calls and job['status']=='reviewed' and not job['items']
        assert len(job['reference_clues'])==2
        with store.connect() as db:
            raw=db.execute('SELECT id,source,status FROM entries WHERE run_id=?',(doc['run']['id'],)).fetchall()
        assert len(raw)==2 and all(json.loads(r['source'])['reference_only'] for r in raw)
        result=c.post('/api/knowledge/review',json={'items':[{'id':raw[0]['id'],'status':'accepted'}]})
        assert result.status_code==422 and '定位线索' in result.json()['detail']
        assert not c.get('/api/knowledge/company').json()['revision']['records']


def test_legacy_retry_skips_clues_and_links_body_without_reparse(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='营业执照',occurrence='mention',quote='目录：营业执照'),
                        entry(kind='material',name='营业执照',quote='营业执照 统一社会信用代码 ABC')])
        store=app.state.knowledge_store;sid=start(app,c,doc)
        from ccgp_crawler.knowledge_summary_store import prompt_input,dumps
        raw=store.summary_raw(sid)
        # A previously queued part contains both body and TOC; resume without changing it.
        with store.connect() as db:
            db.execute('DELETE FROM summary_parts WHERE summary_id=?',(sid,))
            db.execute('INSERT INTO summary_parts(summary_id,position,inputs) VALUES (?,?,?)',(sid,0,dumps([prompt_input(v) for v in raw['inputs']])))
            db.execute("UPDATE knowledge_summaries SET status='failed',error='汇总不能将目录或模板升级为实际材料' WHERE id=?",(sid,))
        original_inputs=store.summary_raw(sid)['inputs'];store.summary_action(sid,'retry')
        ai=SummaryAI();job=process(app,sid,ai)
        assert job['status']=='awaiting_review' and len(ai.calls)==1
        assert len(ai.calls[0][0]['records'])==1
        assert ai.calls[0][0]['records'][0]['data']['occurrence']=='actual'
        assert len(job['items'])==1 and len(job['items'][0]['sources'])==2
        assert job['items'][0]['sources'][0]['occurrence']=='actual'
        assert store.summary_raw(sid)['inputs']==original_inputs
        assert doc['run']['completed_batches']==store.document(doc['id'])['run']['completed_batches']


def test_bad_directory_upgrade_is_ignored_without_discarding_valid_body():
    inputs=[{'id':'toc','data':entry(kind='material',occurrence='mention'),'text':'目录：营业执照'},
            {'id':'body','data':entry(kind='material'),'text':'营业执照 ABC'}]
    result=validate_summary(SummaryOutput.model_validate({'records':[
        record([inputs[0]],occurrence='actual',value='已提供营业执照'),
        record([inputs[1]],occurrence='actual',value='有营业执照')]}),inputs)
    assert len(result.records)==1 and result.records[0].evidence[0].entry_id=='body'
    assert result.ignored_entry_ids==['toc']
    # A second pass against full saved text is idempotent.
    assert validate_summary(result,inputs).ignored_entry_ids==['toc']


@pytest.mark.parametrize('occurrence',['mention','template'])
def test_reference_only_cannot_generate_ability_even_with_filled_label(occurrence):
    inputs=[{'id':'clue','data':entry(kind='material',occurrence=occurrence),'text':'设备能力声明函'}]
    result=validate_summary(SummaryOutput.model_validate({'records':[record(inputs,kind='fact',occurrence='actual',value='企业具备设备能力')]}),inputs)
    assert not result.records and result.ignored_entry_ids==['clue']


def test_legacy_directory_only_part_finishes_without_model_call(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',occurrence='mention',quote='目录：营业执照')]);sid=start(app,c,doc)
        store=app.state.knowledge_store
        from ccgp_crawler.knowledge_summary_store import prompt_input,dumps
        raw=store.summary_raw(sid)
        with store.connect() as db:
            db.execute('INSERT INTO summary_parts(summary_id,position,inputs) VALUES (?,?,?)',(sid,0,dumps([prompt_input(v) for v in raw['inputs']])))
        ai=SummaryAI();job=process(app,sid,ai)
        assert not ai.calls and job['completed_parts']==job['total_parts']==1
        assert not job['items'] and job['status']=='reviewed'


def test_filled_equipment_statement_without_keyword_is_valid():
    quote='本单位履行合同所必备的设备和专业技术能力。'
    inputs=[{'id':'one','data':entry(kind='material',name='设备能力声明函',quote=quote),'text':quote}]
    result=validate_summary(SummaryOutput.model_validate({'records':[record(inputs,kind='fact',name='设备与技术能力',value='企业声明具备履约设备和专业技术能力')]}),inputs)
    assert result.records[0].evidence_kind=='declaration'
    assert result.records[0].occurrence=='actual' and not result.records[0].needs_review


def test_title_quote_restores_existing_filled_statement():
    quote='本单位履行合同所必备的设备和专业技术能力。'
    inputs=[{'id':'one','data':entry(kind='material',name='设备能力声明函',quote=quote),'text':'设备能力声明函\n'+quote,'original_quote':quote}]
    result=validate_summary(SummaryOutput.model_validate({'records':[record(inputs,kind='fact',name='设备与技术能力',value='企业声明具备设备和专业技术能力',evidence=[{'entry_id':'one','quote':'设备能力声明函'}])]}),inputs)
    assert result.records[0].evidence[0].quote==quote
    assert result.records[0].evidence_kind=='declaration'
    assert validate_summary(result,inputs).records[0].evidence[0].quote==quote


def test_title_failure_keeps_other_material_and_does_not_support_bid(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='设备能力声明函',quote='设备能力声明函')])
        c.put(f"/api/knowledge/documents/{doc['id']}/ownership",json={'confirmed':True})
        def dual(inputs):
            return {'records':[record(inputs,kind='material',name='设备能力声明函',subject_role='supplier'),record(inputs,kind='fact',name='设备与技术能力',value='企业具备设备和专业技术能力',subject_role='supplier')],'ignored_entry_ids':[]}
        job=process(app,start(app,c,doc),SummaryAI([dual]))
        assert job['status']=='awaiting_review' and len(job['items'])==2
        version=app.state.knowledge_store.revision(review(c,job['items']))
        assert len(version['material_support'])==1 and version['material_support'][0]['kind']=='material'
        fact=next(i for i in version['records'] if i['data']['kind']=='fact')
        assert not fact['basic_support'] and fact['data']['needs_review']


def test_restoration_never_uses_invented_or_unrelated_quote():
    inputs=[{'id':'one','data':entry(kind='material',name='设备能力声明函'),'text':'设备能力声明函','original_quote':'本单位具备设备和技术能力'}]
    result=validate_summary(SummaryOutput.model_validate({'records':[record(inputs,kind='fact',name='设备能力',value='企业具备设备能力')]}),inputs)
    assert result.records[0].evidence_kind=='inference'
    inputs[0]['text']+='\n本公司具有独立承担民事责任的能力。'
    inputs[0]['original_quote']='本公司具有独立承担民事责任的能力。'
    result=validate_summary(SummaryOutput.model_validate({'records':[record(inputs,kind='fact',name='设备能力',value='企业具备设备能力',evidence=[{'entry_id':'one','quote':'设备能力声明函'}])]}),inputs)
    assert result.records[0].evidence_kind=='inference'


def separate_parts(store,sid):
    from ccgp_crawler.knowledge_summary_store import prompt_input,dumps
    with store.connect() as db:
        db.execute('DELETE FROM summary_parts WHERE summary_id=?',(sid,))
        db.executemany('INSERT INTO summary_parts(summary_id,position,inputs) VALUES (?,?,?)',
                       [(sid,i,dumps([prompt_input(v)])) for i,v in enumerate(store.summary_raw(sid)['inputs'])])


def test_bad_middle_group_continues_partial_review_and_retry_keeps_manual_snapshot(app):
    seed(app,count=1)
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='营业执照'),entry(kind='material',name='许可证'),entry(kind='material',name='设备能力声明函')])
        store=app.state.knowledge_store;sid=start(app,c,doc);separate_parts(store,sid)
        def invalid(inputs):return {'records':None}
        ai=SummaryAI([lambda inputs:{'records':[record(inputs)]},invalid])
        job=process(app,sid,ai)
        assert len(ai.calls)==3 and job['status']=='partial'
        assert (job['completed_parts'],job['failed_parts'],job['processed_parts'],job['pending_parts'])==(2,1,3,0)
        assert job['part_errors'][0]['part']==2 and job['part_errors'][0]['locations']==['正文段落 2']
        assert '格式无效' in job['part_errors'][0]['error']
        assert len(job['items'])==2 and store.summary_next_part(sid) is None
        rid=review(c,job['items'],changes={'notes':'人工核对保留'})
        version=store.revision(rid)
        assert store.summary_detail(sid)['status']=='partial'
        assert version['coverage'][0]['summary_gaps'][0]['part']==2
        assert any('汇总未完成' in v for v in version['profile']['limitations'])
        task_id=c.post('/api/bid-check/tasks',data={'knowledge_revision_id':rid,'expected_config':json.dumps(CONFIG)}).json()['id']
        before_task=app.state.bid_store.raw(task_id)['knowledge_context']
        reviewed={v['id']:v for v in store.summary_detail(sid)['items']}
        with store.connect() as db:outputs=[tuple(r) for r in db.execute("SELECT position,output FROM summary_parts WHERE summary_id=? AND status='done'",(sid,))]
        assert c.post(f'/api/knowledge/summaries/{sid}/action',json={'action':'retry','expected_config':CONFIG}).status_code==200
        retry=SummaryAI();result=process(app,sid,retry)
        assert len(retry.calls)==1 and result['status']=='awaiting_review'
        assert result['completed_parts']==3 and not result['part_errors']
        assert len(result['items'])==3
        for item in result['items']:
            if item['id'] in reviewed:assert item==reviewed[item['id']]
        assert set(reviewed)<={v['id'] for v in result['items']}
        with store.connect() as db:
            assert outputs==[tuple(r) for r in db.execute("SELECT position,output FROM summary_parts WHERE summary_id=? AND position IN (0,2)",(sid,))]
        assert store.revision(rid)==version and app.state.bid_store.raw(task_id)['knowledge_context']==before_task
        assert not store.company()['revision']['coverage'][0]['summary_gaps']
        final=store.revision(review(c,[v for v in result['items'] if v['status']=='pending']))
        assert not final['coverage'][0]['summary_gaps']


@pytest.mark.parametrize('error',[BidCheckError('局部未发现材料不能生成企业全局缺失结论'),
                                AIModelError('等待超时',category='timeout',retryable=True),
                                RuntimeError('PRIVATE UPSTREAM BODY')])
def test_local_failure_and_exhausted_retry_do_not_stop_later_parts(app,error):
    with TestClient(app) as c:
        doc=make(app,c,[entry(name='能力'+str(i)) for i in range(3)])
        store=app.state.knowledge_store;sid=start(app,c,doc);separate_parts(store,sid)
        outputs=[error]*3 if isinstance(error,AIModelError) else [error]
        ai=SummaryAI(outputs);job=process(app,sid,ai)
        assert job['status']=='partial' and job['completed_parts']==2 and job['failed_parts']==1
        assert len(ai.calls)==(5 if isinstance(error,AIModelError) else 3)
        assert 'PRIVATE' not in json.dumps(job)


def test_every_group_invalid_is_failed_once_without_busy_loop(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(name='能力'+str(i)) for i in range(3)])
        store=app.state.knowledge_store;sid=start(app,c,doc);separate_parts(store,sid)
        ai=SummaryAI([{'records':None}]*3);job=process(app,sid,ai)
        assert job['status']=='failed' and job['failed_parts']==3 and not job['items']
        assert len(ai.calls)==3 and job['processed_parts']==3 and not job['pending_parts']
        assert store.summary_work() is None


@pytest.mark.parametrize('category',['authentication','billing','rate_limit','model','compatibility'])
def test_global_provider_failure_pauses_remaining_groups(app,category):
    with TestClient(app) as c:
        doc=make(app,c,[entry(name='能力'+str(i)) for i in range(3)])
        store=app.state.knowledge_store;sid=start(app,c,doc);separate_parts(store,sid)
        ai=SummaryAI([AIModelError('上游需修复',category=category)]);job=process(app,sid,ai)
        assert job['status']=='failed' and job['failed_parts']==1 and job['pending_parts']==2 and len(ai.calls)==1


def test_stop_after_local_failure_keeps_gap_and_resumes_only_remaining(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(name='能力'+str(i)) for i in range(3)])
        store=app.state.knowledge_store;sid=start(app,c,doc);separate_parts(store,sid)
        def stop(inputs):
            store.summary_action(sid,'stop')
            return {'records':[record(inputs)]}
        ai=SummaryAI([{'records':None},stop]);job=process(app,sid,ai)
        assert job['status']=='stopped' and job['failed_parts']==1 and job['pending_parts']==2 and len(ai.calls)==2
        store.summary_action(sid,'start');retry=SummaryAI();result=process(app,sid,retry)
        assert result['status']=='awaiting_review' and len(retry.calls)==3


def test_retry_combines_new_evidence_without_rewriting_reviewed_material(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='营业执照',quote='营业执照 A'),entry(kind='material',name='营业执照',quote='营业执照 B')])
        store=app.state.knowledge_store;sid=start(app,c,doc);separate_parts(store,sid)
        job=process(app,sid,SummaryAI([lambda inputs:{'records':[record(inputs)]},{'records':None}]))
        rid=review(c,job['items'],changes={'value':'人工确认执照','notes':'原人工备注'})
        original=store.summary_detail(sid)['items'][0];snapshot=store.revision(rid)
        store.summary_action(sid,'retry');result=process(app,sid)
        assert len(result['items'])==2 and result['items'][0]==original
        new=result['items'][1]
        assert new['status']=='pending' and len(new['sources'])==2 and new['data']['value']=='人工确认执照'
        assert store.revision(rid)==snapshot
        version=store.revision(review(c,[new]))
        assert len(version['records'])==1 and len(version['records'][0]['sources'])==2


@pytest.mark.parametrize('text,quote',[
    ('经营范围：粮油（销售）。“本公司具备能力”','经营范围:粮油(销售)。"本公司具备能力"'),
    ('正文\n本单位具有\u200b民事责任能力。','本单位具有民事责任能力。'),
    ('证件编号ＡＢ１２３','证件编号AB123'),
])
def test_typography_repair_returns_exact_source_span(text,quote):
    match=original_quote_match(text,quote)
    assert match and contains_quote(text,match)


@pytest.mark.parametrize('quote',['收入9,289,865.07元','收入9.289.865.08元','收入9289865.07元','本单位不具备设备能力'])
def test_quote_repair_never_changes_financial_value_or_negation(quote):
    assert original_quote_match('收入9.289.865.07元；本单位具备设备能力',quote) is None


def test_quote_failure_keeps_original_material_and_valid_peers(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='营业执照',quote='营业执照 编号 A123'),entry(name='设备能力',quote='本单位具备设备和技术能力')])
        store=app.state.knowledge_store;sid=start(app,c,doc)
        def malformed(inputs):
            return {'records':[record([inputs[0]],name='凭空新资质',value='持有特级资质',certificates=[{'number':'MADEUP','entry_ids':[inputs[0]['id']]}],evidence=[{'entry_id':inputs[0]['id'],'quote':'伪造引用'}]),record([inputs[1]])]}
        ai=SummaryAI([malformed]);job=process(app,sid,ai)
        assert job['status']=='awaiting_review' and job['completed_parts']==1 and len(ai.calls)==1
        assert not job['failed_parts'] and len(job['items'])==2
        material=next(e for e in job['items'] if e['data']['kind']=='material')
        assert material['data']['name']=='营业执照' and material['data']['certificates']==[]
        assert material['data']['needs_review'] and '原始提取' in material['data']['notes']
        assert material['sources'][0]['quote']=='营业执照 编号 A123'
        assert not any('MADEUP' in json.dumps(e) or '特级' in e['data']['value'] for e in job['items'])
        assert next(e for e in job['items'] if e['data']['kind']=='fact')['data']['name']=='设备能力'
        assert all(e['status']=='pending' for e in job['items'])
        assert not c.get('/api/knowledge/company').json()['revision']['records']


def test_omitted_or_wrong_source_id_falls_back_without_losing_coverage(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='营业执照'),entry(name='设备能力')]);sid=start(app,c,doc)
        def wrong(inputs):return {'records':[record([inputs[0]],value='企业凭空声明',evidence=[{'entry_id':'NOT-AN-INPUT','quote':inputs[0]['text']}])]}
        job=process(app,sid,SummaryAI([wrong]))
        assert job['status']=='awaiting_review' and len(job['items'])==2
        assert all('原始提取' in e['data']['notes'] for e in job['items'])
        assert not any(e['data']['value']=='企业凭空声明' for e in job['items'])


def test_reconciled_quote_remains_strict_and_does_not_upgrade_third_party(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='厂家许可证',subject_role='manufacturer',quote='厂家许可证 编号 B12')]);sid=start(app,c,doc)
        def upgrade(inputs):return {'records':[record(inputs,subject_role='supplier',value='本企业持有许可证')]}
        job=process(app,sid,SummaryAI([upgrade]))
        assert job['status']=='awaiting_review'
        assert job['items'][0]['data']['subject_role']=='manufacturer'
        assert job['items'][0]['data']['value']!= '本企业持有许可证'


def test_normalized_reference_preserves_original_quotation(app):
    with TestClient(app) as c:
        quote='本单位具有设备（含车辆）能力：已提供。'
        doc=make(app,c,[entry(name='设备能力',quote=quote)]);sid=start(app,c,doc)
        def typography(inputs):return {'records':[record(inputs,evidence=[{'entry_id':inputs[0]['id'],'quote':'本单位具有设备(含车辆)能力:已提供。'}])]}
        job=process(app,sid,SummaryAI([typography]));source=job['items'][0]['sources'][0]
        assert source['quote']==quote and '原始提取' not in job['items'][0]['data']['notes']
