"""Derived review views preserve extraction, audits and historic snapshots."""
import json
import pytest
from fastapi.testclient import TestClient
from test_bid_check import app
from test_knowledge import entry,confirm
from test_knowledge_summary import make,start,process,review,SummaryAI,record
from ccgp_crawler.knowledge_views import reference_entry,note_view


def trace(store):return store.entries(view='trace')['items']


def test_summary_links_multi_withdraw_and_source_changes(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='设备能力声明函')]);store=app.state.knowledge_store
        original=trace(store)[0];run=doc['run']['id']
        def dual(inputs):return {'records':[record(inputs),record(inputs,kind='fact',name='设备能力')],'ignored_entry_ids':[]}
        job=process(app,start(app,c,doc),SummaryAI([dual]))
        review(c,job['items'])
        detail=c.get(f"/api/knowledge/summaries/{job['id']}")
        assert detail.status_code==200 and len(detail.json()['items'])==2
        assert c.get('/api/knowledge/summaries/missing').status_code==404
        raw=trace(store)[0]
        assert raw['status']=='pending' and raw['display_status']=='summary_confirmed'
        assert len(raw['summary_links'])==2 and not raw['reviewable']
        assert store.entries()['total']==0 and store.document(doc['id'])['run']['pending_entries']==0
        with store.connect() as db:assert db.execute('SELECT count(*) FROM review_events').fetchone()[0]==0
        review(c,[job['items'][0]],'pending')
        assert len(trace(store)[0]['summary_links'])==1 and store.entries()['total']==0
        review(c,[job['items'][1]],'pending')
        assert trace(store)[0]['reviewable'] and store.entries(status='pending')['total']==1
        assert store.document(doc['id'])['run']['pending_entries']==1
        review(c,job['items'])
        c.post('/api/knowledge/review',json={'items':[{'id':original['id'],'status':'pending','changes':{'notes':'人工修正原始来源'}}]})
        assert not trace(store)[0]['summary_links'] and store.entries()['total']==1
        assert trace(store)[0]['original']==original['original']
        assert c.post('/api/knowledge/summaries/review',json={'items':[{'id':job['items'][0]['id'],'status':'accepted'}]}).status_code==422
        # Extraction failures remain failures regardless of reviewed evidence.
        store.update_run(run,status='failed')
        assert store.document(doc['id'])['run']['status']=='failed'


def test_pending_new_summary_does_not_replace_confirmed_and_paginate_after_classification(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(name=f'能力{i}') for i in range(33)]);store=app.state.knowledge_store
        first=process(app,start(app,c,doc));review(c,[first['items'][0]])
        before=trace(store)[0]['id']
        with store.connect() as db:
            db.execute("INSERT INTO batches(run_id,position,units,status) VALUES (?,33,'[]','failed')",(doc['run']['id'],))
        next_id=start(app,c,doc);process(app,next_id)
        assert store.entries(status='pending')['total']==32
        assert len(store.entries(status='pending')['items'])==30
        assert len(store.entries(status='pending',page=2)['items'])==2
        linked=[e for e in store.entries(view='trace',status='accepted')['items'] if e['summary_links']]
        assert len(linked)==1 and linked[0]['summary_links'][0]['summary_id']==first['id']
        assert store.document(doc['id'])['run']['pending_entries']==32
        assert c.get('/api/knowledge/entries?view=invalid').status_code==422


@pytest.mark.parametrize('occurrence,quote,notes,reference',[
    ('mention','目录：营业执照','',True),
    ('template','企业名称：____','空白声明函',True),
    ('third_party','9.1 营业执照扫描件 13','在目录中提及，未在正文段落中提取到具体扫描件内容。',True),
    ('third_party','营业执照 统一社会信用代码 A001','在目录中提及，本批未见正文',False),
    ('third_party','我方声明具备履约能力','旧公司名称不符',False),
    ('actual','营业执照','本批未见正文',False),
    ('unknown','资格声明 已填写','归属未知',False),
])
def test_legacy_reference_classification_without_new_marker(app,occurrence,quote,notes,reference):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='营业执照',occurrence=occurrence,quote=quote,notes=notes)])
        store=app.state.knowledge_store
        with store.connect() as db:
            r=db.execute('SELECT id,source FROM entries').fetchone();source=json.loads(r['source']);source.pop('reference_only',None)
            db.execute('UPDATE entries SET source=? WHERE id=?',(json.dumps(source),r['id']))
        raw=trace(store)[0]
        assert raw['reference_only']==reference
        assert store.entries()['total']==int(not reference)
        assert store.document(doc['id'])['run']['pending_entries']==int(not reference)
        response=c.post('/api/knowledge/review',json={'items':[{'id':raw['id'],'status':'accepted'}]})
        assert response.status_code==(422 if reference else 200)
        if reference:
            assert c.post('/api/knowledge/review',json={'items':[{'id':raw['id'],'status':'pending','changes':{'notes':'','value':'有营业执照'}}]}).status_code==422
            assert trace(store)[0]['data']==raw['data']


def test_reference_batch_review_atomic_and_idempotent_version_migration(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(name='有效能力'),entry(name='目录营业执照',occurrence='mention')]);store=app.state.knowledge_store
        raw=trace(store)
        result=c.post('/api/knowledge/review',json={'items':[{'id':i['id'],'status':'accepted'} for i in raw]})
        assert result.status_code==422 and all(i['status']=='pending' for i in trace(store))
        with store.connect() as db:
            assert db.execute('SELECT count(*) FROM review_events').fetchone()[0]==0
            # Emulate an immutable legacy accepted clue and its older version.
            db.execute("UPDATE entries SET status='accepted' WHERE id=?",(raw[1]['id'],))
            db.execute('UPDATE documents SET adopted_run_id=? WHERE id=?',(doc['run']['id'],doc['id']))
            old={'company_name':'测试公司','profile':{'facts':[{'id':'legacy-fact'}]},'records':[{'data':raw[1]['data'],'entry_ids':[raw[1]['id']]}]}
            db.execute("INSERT INTO revisions VALUES ('legacy','default',?,'then')",(json.dumps(old),))
        before=trace(store)
        store.initialize();new=store.company()['revision']
        assert new['id']!='legacy' and new['material_count']==0 and new['fact_count']==0
        assert store.revision('legacy')==old and trace(store)==before
        store.initialize();assert store.company()['revision']['id']==new['id']


def test_current_state_local_notes_and_independent_uncertainty(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='营业执照',occurrence='mention',quote='目录：营业执照',notes='本批未见正文，因此无法确认实际持有；需结合其他来源核实；文字不清。'),entry(kind='material',name='营业执照',quote='营业执照 编号 A001'),entry(kind='material',name='营业执照',subject_role='manufacturer',subject='其他厂家',notes='文字不清')]);store=app.state.knowledge_store
        def actual(inputs):
            supplier=next(v for v in inputs if v['data'].get('subject_role')!='manufacturer')
            return {'records':[record([supplier],occurrence='actual',notes='本批未见正文；编号文字不清')],'ignored_entry_ids':[v['id'] for v in inputs if v!=supplier]}
        job=process(app,start(app,c,doc),SummaryAI([actual]));review(c,job['items'])
        items=trace(store);clue=next(v for v in items if v['reference_only'])
        assert clue['current_material_status']=='已发现实际营业执照'
        assert clue['current_notes']=='文字不清' and clue['local_observations']
        other=next(v for v in items if v['data'].get('subject_role')=='manufacturer')
        assert not other['summary_links'] and other['current_notes']=='文字不清'
        assert store.summary_detail(job['id'])['items'][0]['current_notes']=='编号文字不清'
        assert store.company()['revision']['records'][0]['current_notes']=='编号文字不清'
        assert '本批未见' not in store.company()['revision']['profile']['facts'][0]['statement']
        split=note_view('本批未找到扫描件，因此无法确认实际持有；许可证有效期不清；文字不清')
        assert len(split['local_observations'])==2 and split['current_notes']=='许可证有效期不清；文字不清'


def test_mixed_single_clause_notes_keep_independent_issue():
    assert note_view('本批未见正文但材料类型无法确定')['current_notes']=='材料类型无法确定'
    assert note_view('本批未找到扫描件且有效期文字不清')['current_notes']=='有效期文字不清'


def test_multiple_material_summaries_keep_independent_source_links(app):
    with TestClient(app) as c:
        doc=make(app,c,[entry(kind='material',name='复合证明')]);store=app.state.knowledge_store
        first=process(app,start(app,c,doc));review(c,first['items'])
        with store.connect() as db:
            db.execute("INSERT INTO batches(run_id,position,units,status) VALUES (?,1,'[]','failed')",(doc['run']['id'],))
        def other(inputs):return {'records':[record(inputs,name='另一材料')],'ignored_entry_ids':[]}
        second=process(app,start(app,c,doc),SummaryAI([other]));review(c,second['items'])
        assert len(trace(store)[0]['summary_links'])==2
        review(c,second['items'],'pending')
        assert len(trace(store)[0]['summary_links'])==1
        assert trace(store)[0]['display_status']=='summary_confirmed'
