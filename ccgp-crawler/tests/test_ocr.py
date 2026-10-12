"""Paddle official protocol and knowledge OCR without cloud calls."""
import asyncio
import io
import json
import stat
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from ccgp_crawler.ocr_config import OCRConfigStore, OCRConfigUpdate, OCRConfig
from ccgp_crawler.ocr_client import PaddleOCRClient, OCRError
from ccgp_crawler.bid_service import TaskInterrupted
from ccgp_crawler.knowledge_models import ExtractionOutput, validate_extraction
from ccgp_crawler.bid_models import BidCheckError
from test_bid_check import app
from test_knowledge import CONFIG, entry

TOKEN='ocr-secret-do-not-return'
SNAPSHOT={'enabled':True,'provider':'paddle_aistudio','model':'PP-OCRv5'}


def result(text='营业执照\n测试公司'):
    return json.dumps({'result':{'ocrResults':[{'prunedResult':{'rec_texts':text.split('\n'),'rec_scores':[.98,.94]}}]}},ensure_ascii=False)


def image():
    stream=io.BytesIO();Image.new('RGB',(80,80),'white').save(stream,'PNG');return stream.getvalue()


def test_configuration_persists_without_secret_echo_and_test_uses_draft(app):
    app.state.ocr_client=PaddleOCRClient(transport=httpx.MockTransport(lambda req:httpx.Response(401,json={'message':TOKEN})))
    with TestClient(app) as client:
        default=client.get('/api/ocr/config').json();assert not default['enabled'] and not default['access_token_configured']
        assert not app.state.ocr_config_store.path.exists()
        saved=client.put('/api/ocr/config',json={**SNAPSHOT,'access_token':TOKEN})
        assert saved.status_code==200 and TOKEN not in saved.text
        store=OCRConfigStore(app.state.ocr_config_store.path.parent)
        assert store.get().access_token.get_secret_value()==TOKEN
        assert stat.S_IMODE(store.path.stat().st_mode)==0o600
        for token in [None,'']:
            assert client.put('/api/ocr/config',json={**SNAPSHOT,'access_token':token}).json()['access_token_configured']
            assert store.get().access_token.get_secret_value()==TOKEN
        tested=client.post('/api/ocr/test',json={**SNAPSHOT,'access_token':'draft-token'})
        assert tested.json()['ok'] is False and '认证失败' in tested.json()['message']
        assert store.get().access_token.get_secret_value()==TOKEN
        assert TOKEN not in tested.text
        assert client.put('/api/ocr/config',json={**SNAPSHOT,'access_token':'replacement'}).status_code==200
        assert store.get().access_token.get_secret_value()=='replacement'
        assert client.put('/api/ocr/config',json={**SNAPSHOT,'clear_access_token':True}).json()['access_token_configured'] is False
        invalid=client.put('/api/ocr/config',json={**SNAPSHOT,'access_token':TOKEN,'clear_access_token':True})
        assert invalid.status_code==422 and TOKEN not in invalid.text
        store.path.write_text('{broken')
        assert client.get('/api/ocr/config').status_code==503


def test_official_multipart_poll_and_unsigned_result_download():
    calls=[];polls=0;saved=[]
    def handler(req):
        nonlocal polls
        calls.append(req)
        if req.method=='POST':
            assert req.headers['authorization']=='Bearer '+TOKEN
            assert b'PP-OCRv5' in req.content and b'name="file"' in req.content
            assert b'visualize' in req.content and b'false' in req.content
            return httpx.Response(200,json={'code':0,'data':{'jobId':'job1'}})
        if req.url.host=='paddleocr.aistudio-app.com':
            polls+=1
            return httpx.Response(200,json={'data':{'state':'running'}} if polls==1 else {'data':{'state':'done','resultUrl':{'jsonUrl':'https://result.example/output.jsonl'}}})
        assert 'authorization' not in req.headers
        return httpx.Response(200,text=result())
    client=PaddleOCRClient(transport=httpx.MockTransport(handler),poll_interval=0)
    value=asyncio.run(client.recognize(OCRConfig(**SNAPSHOT,access_token=TOKEN),image(),save_job=saved.append))
    assert saved==['job1'] and value['text']=='营业执照\n测试公司' and len(value['scores'])==2
    assert len(calls)==4


def test_transient_poll_retry_keeps_job_and_limits_attempts():
    submits=0;polls=0
    def handler(req):
        nonlocal submits,polls
        if req.method=='POST':submits+=1;return httpx.Response(200,json={'data':{'jobId':'job1'}})
        polls+=1
        return httpx.Response(503,json={})
    client=PaddleOCRClient(transport=httpx.MockTransport(handler),retry_delays=(0,0))
    with pytest.raises(OCRError,match='暂时不可用'):asyncio.run(client.recognize(OCRConfig(access_token=TOKEN),image()))
    assert submits==1 and polls==3


@pytest.mark.parametrize('status,body,category',[(401,{},'authentication'),(429,{},'rate_limit'),(429,{'message':'quota exhausted'},'quota'),(200,{'code':1,'message':'配额不足'},'quota')])
def test_errors_are_safe(status,body,category):
    client=PaddleOCRClient(transport=httpx.MockTransport(lambda req:httpx.Response(status,json={**body,'secret':TOKEN})),retry_delays=())
    with pytest.raises(OCRError) as error:asyncio.run(client.recognize(OCRConfig(access_token=TOKEN),image()))
    assert error.value.category==category and TOKEN not in str(error.value)


def test_timeout_cancels_current_request_and_stop_preserves_submitted_job():
    async def slow(req):await asyncio.sleep(5);return httpx.Response(200,json={})
    client=PaddleOCRClient(transport=httpx.MockTransport(slow),timeout=.02)
    with pytest.raises(OCRError,match='超时'):asyncio.run(client.recognize(OCRConfig(access_token=TOKEN),image()))
    saved=[]
    def check():
        if saved:raise TaskInterrupted()
    client=PaddleOCRClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'data':{'jobId':'preserved'}})))
    with pytest.raises(TaskInterrupted):asyncio.run(client.recognize(OCRConfig(access_token=TOKEN),image(),save_job=saved.append,check=check))
    assert saved==['preserved']


class OCRFake:
    def __init__(self,values=None,stop=None):self.calls=[];self.values=list(values or ['营业执照\n测试公司']);self.stop=stop
    async def recognize(self,config,data,**kwargs):
        self.calls.append((data,kwargs['job_id']))
        kwargs['save_job']('job'+str(len(self.calls)))
        if self.stop:self.stop();kwargs['check']()
        value=self.values.pop(0)
        if isinstance(value,Exception):raise value
        return {'text':value,'scores':[.96]}


class TextAI:
    def __init__(self,fail=False):self.calls=[];self.fail=fail
    async def generate(self,config,input_text,**kwargs):
        self.calls.append((json.loads(input_text),kwargs))
        if self.fail:raise BidCheckError('临时模型失败')
        sources=[s for s in self.calls[-1][0]['sources'] if not s['context_only']]
        entries=[entry(source_id=s['source_id'],quote=s['text'][:100],kind='material',name='营业执照') for s in sources]
        return json.dumps({'entries':entries,'limitations':[]},ensure_ascii=False)


def setup(app,tmp_path,count=1):
    app.state.knowledge_manager.interval=0
    store=app.state.knowledge_store;store.save_company('测试公司')
    path=tmp_path/'original.png';path.write_bytes(image())
    doc_id,_=store.add_document('doc','材料.png','hash',path.stat().st_size,path)
    run=store.document(doc_id)['run'];units=[{'source_id':'S'+str(i),'location':f'图片位置{i}','text':'','visual':{'kind':'image','path':str(path)}} for i in range(count)]
    store.set_batches(run['id'],count,[units]);return store,doc_id,run['id']


def start(app,doc_id,ocr=True):
    app.state.ocr_config_store.save(OCRConfigUpdate(enabled=ocr,access_token=TOKEN))
    with TestClient(app) as client:
        response=client.post(f'/api/knowledge/documents/{doc_id}/action',json={'action':'start','expected_config':CONFIG,'expected_ocr':{**SNAPSHOT,'enabled':ocr}})
    assert response.status_code==200,response.text


def test_knowledge_ocr_text_only_partial_failure_confirmation_and_provenance(app,tmp_path):
    store,doc_id,run_id=setup(app,tmp_path,3)
    app.state.ocr_client=OCRFake(['营业执照\n测试公司','',OCRError('图像识别失败')]);app.state.ai_client=TextAI()
    start(app,doc_id);asyncio.run(app.state.knowledge_manager.process_run(run_id))
    run=store.document(doc_id)['run'];assert run['status']=='awaiting_review'
    assert run['ocr']['total']==3 and run['ocr']['processed']==3 and run['ocr']['failed']==2
    assert len(app.state.ai_client.calls)==1 and all(not kwargs['images'] for _,kwargs in app.state.ai_client.calls)
    found=store.entries()['items'][0];assert found['source']['ocr'] and found['data']['needs_review']
    assert found['source']['location']=='图片位置0 · OCR识别' and '营业执照' in found['source']['ocr_text']
    store.review([{'id':found['id'],'status':'accepted','changes':{'needs_review':False}}])
    revision=store.company()['revision'];assert revision['profile']['facts'] and revision['unsafe_fact_ids']
    assert any('OCR 待核实' in message for message in revision['profile']['limitations'])


def test_ocr_restore_same_job_and_completed_results_not_rerun(app,tmp_path):
    store,doc_id,run_id=setup(app,tmp_path)
    stopped=OCRFake(stop=lambda:store.action(doc_id,'stop'));app.state.ocr_client=stopped;app.state.ai_client=TextAI(fail=True)
    start(app,doc_id);asyncio.run(app.state.knowledge_manager.process_run(run_id))
    assert store.document(doc_id)['run']['status']=='stopped'
    app.state.ocr_client=OCRFake();start(app,doc_id);asyncio.run(app.state.knowledge_manager.process_run(run_id))
    assert app.state.ocr_client.calls[0][1]=='job1' and app.state.ocr_client.calls[0][0] is None
    assert store.document(doc_id)['run']['status']=='failed'
    app.state.ai_client=TextAI();start(app,doc_id);asyncio.run(app.state.knowledge_manager.process_run(run_id))
    assert len(app.state.ocr_client.calls)==1 and store.document(doc_id)['run']['status']=='awaiting_review'


def test_auth_quota_pause_and_long_ocr_text_rebatch_without_truncation(app,tmp_path):
    store,doc_id,run_id=setup(app,tmp_path)
    app.state.ai_client=TextAI();app.state.ocr_client=OCRFake([OCRError('配额不足',fatal=True)])
    start(app,doc_id);asyncio.run(app.state.knowledge_manager.process_run(run_id))
    assert store.document(doc_id)['run']['status']=='stopped' and not app.state.ai_client.calls
    text='营业执照'*12000;app.state.ocr_client=OCRFake([text]);start(app,doc_id)
    asyncio.run(app.state.knowledge_manager.process_run(run_id))
    assert ''.join(s['text'] for data,_ in app.state.ai_client.calls for s in data['sources'])==text
    assert all(len(data['sources'])<=6 and sum(len(s['text']) for s in data['sources'])<=20000 for data,_ in app.state.ai_client.calls)


def test_unconfigured_ocr_blocks_only_image_start_and_quote_must_match(app,tmp_path):
    store,doc_id,_=setup(app,tmp_path)
    app.state.ocr_config_store.save(OCRConfigUpdate(enabled=True))
    with TestClient(app) as client:
        response=client.post(f'/api/knowledge/documents/{doc_id}/action',json={'action':'start','expected_config':CONFIG})
        assert response.status_code==422 and 'Access Token' in response.text
    output=ExtractionOutput.model_validate({'entries':[entry(quote='不存在的文字')],'limitations':[]})
    with pytest.raises(BidCheckError,match='引用与原文不符'):validate_extraction(output,[{'source_id':'S1','text':'营业执照','ocr':True,'visual':None}],'测试公司')


def test_ocr_enabled_text_only_document_needs_no_token(app,tmp_path):
    store=app.state.knowledge_store;store.save_company('测试公司');app.state.knowledge_manager.interval=0
    path=tmp_path/'original.txt';path.write_text('测试公司营业执照')
    doc_id,_=store.add_document('doc','材料.txt','hash',path.stat().st_size,path)
    run=store.document(doc_id)['run'];store.set_batches(run['id'],1,[[{'source_id':'S1','location':'正文','text':'测试公司营业执照','visual':None}]])
    app.state.ocr_config_store.save(OCRConfigUpdate(enabled=True));app.state.ai_client=TextAI();app.state.ocr_client=OCRFake()
    with TestClient(app) as client:
        response=client.post(f'/api/knowledge/documents/{doc_id}/action',json={'action':'start','expected_config':CONFIG})
        assert response.status_code==200
    asyncio.run(app.state.knowledge_manager.process_run(run['id']))
    assert not app.state.ocr_client.calls and store.document(doc_id)['run']['status']=='awaiting_review'


def test_new_generation_does_not_reuse_ocr_and_disabled_ocr_sends_original_image(app,tmp_path):
    store,doc_id,run_id=setup(app,tmp_path);app.state.ocr_client=OCRFake();app.state.ai_client=TextAI()
    start(app,doc_id);asyncio.run(app.state.knowledge_manager.process_run(run_id))
    new=store.action(doc_id,'reextract')['run']
    asyncio.run(app.state.knowledge_manager.process_run(new['id']))
    app.state.ocr_client.values=['营业执照 测试公司'];start(app,doc_id)
    asyncio.run(app.state.knowledge_manager.process_run(new['id']))
    assert len(app.state.ocr_client.calls)==2 and app.state.ocr_client.calls[1][1]==''
    next_run=store.action(doc_id,'reextract')['run'];asyncio.run(app.state.knowledge_manager.process_run(next_run['id']))
    start(app,doc_id,ocr=False);asyncio.run(app.state.knowledge_manager.process_run(next_run['id']))
    assert app.state.ai_client.calls[-1][1]['images'] and len(app.state.ocr_client.calls)==2


def test_pdf_direct_text_remains_evidence_even_when_image_ocr_is_empty(app,tmp_path):
    import pymupdf
    store=app.state.knowledge_store;store.save_company('测试公司');app.state.knowledge_manager.interval=0
    path=tmp_path/'original.pdf'
    with pymupdf.open() as pdf:
        page=pdf.new_page();page.insert_text((20,20),'Business license');pdf.save(path)
    doc_id,_=store.add_document('doc','材料.pdf','hash',path.stat().st_size,path);run=store.document(doc_id)['run']
    store.set_batches(run['id'],1,[[{'source_id':'S1','location':'第1页','text':'Business license','visual':{'kind':'pdf','path':str(path),'page':0}}]])
    app.state.ocr_client=OCRFake(['']);app.state.ai_client=TextAI();start(app,doc_id)
    asyncio.run(app.state.knowledge_manager.process_run(run['id']))
    assert store.document(doc_id)['run']['ocr']['failed']==1
    assert store.entries()['items'][0]['source']['quote']=='Business license'
    assert not app.state.ai_client.calls[0][1]['images']


def test_expanded_ocr_batches_retain_original_document_order(app,tmp_path):
    store,doc_id,run_id=setup(app,tmp_path)
    original=store.next_batch(run_id)['units'][0]
    store.set_batches(run_id,3,[[original],[{'source_id':'S2','location':'后续正文','text':'之后文字','visual':None}],[{**original,'source_id':'S3','location':'最后图片'}]])
    long_text='营业执照'*8000
    app.state.ocr_client=OCRFake([long_text,'最后证件']);app.state.ai_client=TextAI();start(app,doc_id)
    asyncio.run(app.state.knowledge_manager.process_run(run_id))
    values=[s['text'] for data,_ in app.state.ai_client.calls for s in data['sources']]
    assert ''.join(values)==long_text+'之后文字'+'最后证件'
    assert store.document(doc_id)['run']['status']=='awaiting_review'
