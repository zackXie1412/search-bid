"""Protocol 2, real store/HTTP with simulated models and notice transport only."""
import asyncio
import copy
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ccgp_crawler.bid_models import BidCheckError
from ccgp_crawler.bid_notice import notice_snapshot, safe_attachment_url
from ccgp_crawler.bid_screening import ScreeningOutput, build_evidence_index, evaluate_screening
from ccgp_crawler.bid_service import NoticeReader
from ccgp_crawler.bid_skills import BidSkillRegistry, screening_snapshot
from ccgp_crawler.ai_client import AIModelError
from test_bid_check import app, seed, create, PROFILE, FakeAI


def fact(quote, **changes):
    return {'fact_id':'K1','statement':quote,'company_name':'测试公司','owned':True,'data':{},'unsafe_for_conflict':False,
            'sources':[{'source_id':'K1:S1','quote':quote,'filename':'历史资料.txt','location':'段落 1'}],**changes}


def response(snapshot, assessment='unknown', evidence=(), **changes):
    return {'requirements':[{'condition_id':c['id'],'clause_ids':[c['id']],'category':'scoring' if not c['mandatory'] else 'specific',
            'requirement':c['quote'],'mandatory':c['mandatory'],'announcement_quote':c['quote'], 'assessment':assessment,
            'evidence':list(evidence), 'conflict_type':'explicit_absence' if assessment=='conflict' else 'none',
            'reason':'需核对对应证明','review_issues':[], **changes} for c in snapshot['clauses']],
            'groups':[{'id':g['id'],'operator':g['operator'],'members':g['members'], 'exception_quote':'','exception_handled':False} for g in snapshot['groups']]}


def evaluate(text, result=None, evidence=()):
    snapshot=notice_snapshot(text)
    return evaluate_screening(ScreeningOutput.model_validate(result or response(snapshot)), snapshot, list(evidence))


def cite(quote, source_id='K1:S1', fact_id='K1'):
    return {'fact_id':fact_id,'source_id':source_id,'quote':quote}


def test_index_all_subclauses_outside_section_scoring_brackets_and_stable_hash():
    text='二、申请人的资格要求：\n1.具备独立承担民事责任能力。\n2.须提供设备能力声明。\n三、获取招标文件\n供应商不得组成联合体。\n本地业绩评分加分。'
    snapshot=notice_snapshot(text)
    assert len(snapshot['clauses'])==4 and sum(c['mandatory'] for c in snapshot['clauses'])==3
    assert snapshot['sha256']==hashlib.sha256(text.encode()).hexdigest()
    assert notice_snapshot(text)['clauses']==snapshot['clauses']
    nested=notice_snapshot('申请人资格要求：须具有食品经营许可证且（食品生产许可证或营业执照）。')
    assert len(nested['clauses'])==3 and {g['operator'] for g in nested['groups']}=={'and','or'}
    assert '且' in nested['clauses'][0]['context']


def test_missing_child_and_header_quote_cannot_cover_whole_section():
    text='二、申请人的资格要求：具备食品经营许可证。须有设备能力声明。'
    snap=notice_snapshot(text); raw=response(snap)
    raw['requirements']=raw['requirements'][:1]
    raw['requirements'][0]['announcement_quote']='二、申请人的资格要求'
    result=evaluate(text, raw)
    assert result['verdict']=='needs_review' and len(result['coverage_gaps'])==2
    assert result['coverage']['checked']==0
    raw=response(snap);raw['requirements'][1]['category']='scoring'
    assert evaluate(text,raw)['requirements'][1]['assessment']=='unknown'


def test_attachment_scope_normal_vs_explicit_gap_safe_links_and_no_download():
    url='https://www.ccgp.gov.cn/cggg/dfgg/gkzb/202610/a.htm'
    html='<div class="vF_detail_content"><p>二、申请人的资格要求：供应商须具备食品经营许可证。</p>'+('<p>公告说明普通采购事项。</p>'*10)+'<a href="/files/a.pdf">采购文件附件</a><a href="javascript:alert(1)">附件2</a><a>附件3</a></div>'
    requests=[]
    def upstream(req):requests.append(str(req.url));return httpx.Response(200,text=html)
    reader=NoticeReader(transport=httpx.MockTransport(upstream))
    snap=asyncio.run(reader.read_snapshot(url))
    assert len(requests)==1 and len(snap['attachments'])==3
    assert snap['attachments'][0]['url']=='https://www.ccgp.gov.cn/files/a.pdf'
    assert snap['attachments'][1]['url'] is None and snap['attachments'][2]['url'] is None
    quote='本企业提供食品经营许可证。'; raw=response(snap,'met',[cite(quote)])
    assert evaluate_screening(ScreeningOutput.model_validate(raw),snap,[fact(quote)])['verdict']=='retain'
    assert evaluate('二、申请人的资格要求：本项目资格要求详见招标文件。')['coverage_gaps']
    for bad in ['javascript:alert(1)','data:text/plain,x','http://127.0.0.1/x','https://user:pass@abc.com/x','http://localhost/x']:
        assert safe_attachment_url(bad,url) is None


@pytest.mark.parametrize('text,quote',[
    ('供应商应具备履约设备和专业技术能力。','本企业声明具备履约设备和专业技术能力。'),
    ('供应商须具备食品经营许可证。','食品经营许可证，持证企业：测试公司。'),
    ('供应商应具备独立承担民事责任的能力。','测试公司营业执照，法人登记。'),
])
def test_general_materials_history_ocr_can_support_positive(text,quote):
    snap=notice_snapshot(text);raw=response(snap,'met',[cite(quote)])
    item=fact(quote,unsafe_for_conflict=True,data={'applicable_date':'2020-01-01','occurrence':'actual','subject_role':'supplier','evidence_kind':'declaration'})
    item['sources'][0]['ocr']=True
    assert evaluate(text,raw,[item])['verdict']=='retain'


@pytest.mark.parametrize('data', [{'occurrence':'mention'},{'occurrence':'template'},{'subject_role':'manufacturer'},{'subject_role':'customer'},{'evidence_kind':'inference'}])
def test_reference_or_third_party_cannot_be_positive(data):
    text='供应商须具备食品经营许可证。';quote='食品经营许可证。';raw=response(notice_snapshot(text),'met',[cite(quote)])
    assert evaluate(text,raw,[fact(quote,data=data)])['verdict']=='needs_review'


def test_multisource_exact_quotes_and_mismatched_or_invented_quotes():
    profile={'company_name':'测试公司','facts':[{'id':'K1','statement':'食品经营许可','source_id':'K1','quote':'第一页'}]}
    sources=[{'source_id':'K1','provenance':[{'quote':'第一页'},{'quote':'第二页：食品经营许可证。'}]}]
    evidence=build_evidence_index(profile,sources)
    text='供应商须具备食品经营许可证。';raw=response(notice_snapshot(text),'met',[cite('食品经营许可证。','K1:S2')])
    assert evaluate(text,raw,evidence)['verdict']=='retain'
    for quote in ['第一页第二页：食品经营许可证。','伪造食品经营许可证。','营业执照']:
        raw=response(notice_snapshot(text),'met',[cite(quote,'K1:S2')])
        assert evaluate(text,raw,evidence)['verdict']=='needs_review'
    raw=response(notice_snapshot(text),'met',[cite('营业执照')])
    assert evaluate(text,raw,[fact('营业执照')])['verdict']=='needs_review'


@pytest.mark.parametrize('quote,conflict_type,expected',[
    ('测试公司目前没有建筑施工许可证。','explicit_absence','exclude'),
    ('测试公司目前建筑施工许可证已吊销。','revoked','exclude'),
    ('测试公司目前没有食品经营许可证。','explicit_absence','needs_review'),
    ('测试公司曾经没有建筑施工许可证。','explicit_absence','needs_review'),
    ('测试公司目前没有违法记录，建筑施工许可证有效。','explicit_absence','needs_review'),
    ('建筑施工许可证于2020年过期。','other','needs_review'),
    ('测试公司建筑施工许可证为三级。','numeric','needs_review'),
    ('测试公司此前建筑施工许可证被吊销，目前已恢复。','revoked','needs_review'),
])
def test_negative_gate_target_time_and_type(quote,conflict_type,expected):
    text='供应商必须具备建筑施工许可证。';raw=response(notice_snapshot(text),'conflict',[cite(quote)],conflict_type=conflict_type)
    assert evaluate(text,raw,[fact(quote)])['verdict']==expected


@pytest.mark.parametrize('changes',[{'unsafe_for_conflict':True},{'owned':False},{'data':{'subject':'其他公司'}},{'data':{'applicable_date':'2020-01-01'}},{'sources':[{'source_id':'K1:S1','quote':'测试公司目前没有建筑施工许可证。','ocr':True}]}])
def test_protected_evidence_never_excludes(changes):
    text='供应商须具备建筑施工许可证。';quote='测试公司目前没有建筑施工许可证。'
    raw=response(notice_snapshot(text),'conflict',[cite(quote)])
    assert evaluate(text,raw,[fact(quote,**changes)])['verdict']=='needs_review'


def test_current_prohibition_vs_old_penalty():
    text='供应商不得处于禁止参加政府采购活动期间。';quote='测试公司目前禁止参加政府采购活动。'
    raw=response(notice_snapshot(text),'conflict',[cite(quote)],conflict_type='prohibited')
    assert evaluate(text,raw,[fact(quote)])['verdict']=='exclude'
    assert evaluate(text,raw,[fact(quote.replace('目前','2020年'))])['verdict']=='needs_review'


def test_numeric_met_without_detail_stays_pending():
    text='供应商须配备至少10台设备。';quote='企业具备设备能力声明。'
    raw=response(notice_snapshot(text),'met',[cite(quote)])
    assert evaluate(text,raw,[fact(quote)])['verdict']=='needs_review'


def test_or_supported_unknown_and_all_conflicts_and_nested():
    text='供应商须具备食品经营许可证或食品生产许可证。';snap=notice_snapshot(text)
    quote='测试公司食品经营许可证。';raw=response(snap)
    raw['requirements'][0].update(assessment='met',evidence=[cite(quote)])
    assert evaluate(text,raw,[fact(quote)])['verdict']=='retain'
    raw['groups'][0]['operator']='and'
    assert evaluate(text,raw,[fact(quote)])['verdict']=='needs_review'
    raw=response(snap,'conflict');qs=['测试公司目前没有食品经营许可证。','测试公司目前没有食品生产许可证。']
    for i,r in enumerate(raw['requirements']):r['evidence']=[cite(qs[i],fact_id=f'K{i+1}',source_id=f'K{i+1}:S1')]
    evidence=[fact(qs[0]),fact(qs[1],fact_id='K2',sources=[{'source_id':'K2:S1','quote':qs[1]}])]
    assert evaluate(text,raw,evidence)['verdict']=='exclude'
    raw['requirements'][1]['assessment']='unknown'
    assert evaluate(text,raw,evidence)['verdict']=='needs_review'
    nested='申请人资格要求：须具有食品经营许可证且（食品生产许可证或营业执照）。';snap=notice_snapshot(nested)
    raw=response(snap,'met');quotes=['食品经营许可证','食品生产许可证','营业执照']
    for i,r in enumerate(raw['requirements']):r['evidence']=[cite(quotes[i],fact_id=f'K{i+1}',source_id=f'K{i+1}:S1')]
    evidence=[fact(q,fact_id=f'K{i+1}',sources=[{'source_id':f'K{i+1}:S1','quote':q}]) for i,q in enumerate(quotes)]
    assert evaluate(nested,raw,evidence)['verdict']=='retain'
    raw['groups']=[raw['groups'][-1]]
    assert evaluate(nested,raw,evidence)['verdict']=='needs_review'


def test_exception_scoring_identity_and_independent_conflict():
    text='供应商须有食品经营许可证，小微企业除外。'
    quote='测试公司目前没有食品经营许可证。';raw=response(notice_snapshot(text),'conflict',[cite(quote)])
    assert evaluate(text,raw,[fact(quote)])['verdict']=='needs_review'
    text='供应商须提供营业执照。';raw=response(notice_snapshot(text),reason='公司名称不一致',review_issues=[{'kind':'subject_identity','text':'公司名称不一致'}])
    result=evaluate(text,raw)
    assert result['verdict']=='retain' and not result['pending_checks'] and '名称不一致' not in result['summary']
    text='业绩评分加分。';raw=response(notice_snapshot(text),'unknown')
    assert evaluate(text,raw)['verdict']=='retain'
    text='供应商須具备食品经营许可证。供应商必须具备建筑施工许可证。';snap=notice_snapshot(text)
    raw=response(snap,'conflict',[cite('测试公司目前没有建筑施工许可证。')]);raw['requirements']=raw['requirements'][1:]
    assert evaluate(text,raw,[fact('测试公司目前没有建筑施工许可证。')])['verdict']=='exclude'


class NewAI:
    def __init__(self, fail=False): self.calls=[];self.fail=fail
    async def generate(self,config,input_text,**kwargs):
        data=json.loads(input_text);self.calls.append((data,kwargs))
        if 'sources' in data: return json.dumps(PROFILE,ensure_ascii=False)
        if self.fail:raise BidCheckError('模拟模型输出失败')
        snap={'clauses':data['clause_index'],'groups':data['relation_index']}
        return json.dumps(response(snap),ensure_ascii=False)


class Reader:
    def __init__(self):self.calls=0
    async def read_snapshot(self,url):
        self.calls+=1
        return notice_snapshot('供应商必须具备建筑施工许可证。')


def test_skill_frozen_new_task_and_notice_retry_snapshot_no_prompt_in_lists(app,tmp_path):
    seed(app,count=1)
    path=tmp_path/'skills'/'qualification-screening'/'SKILL.md';path.parent.mkdir(parents=True);path.write_text('初筛版本一')
    app.state.bid_skills=BidSkillRegistry(path.parent.parent)
    with TestClient(app) as client:
        task_id=create(client).json()['id'];snapshot=app.state.bid_store.raw(task_id)['rules_snapshot']
        path.write_text('初筛版本二')
        task2=create(client).json()['id']
        assert app.state.bid_store.raw(task2)['rules_snapshot']['skill_sha256']!=snapshot['skill_sha256']
        client.post(f'/api/bid-check/tasks/{task_id}/start')
        reader=Reader();ai=NewAI(fail=True);app.state.bid_manager.reader=reader;app.state.ai_client=ai
        asyncio.run(app.state.bid_manager.process_task(task_id))
        assert reader.calls==1 and app.state.bid_store.detail(task_id)['failed']==1
        client.post(f'/api/bid-check/tasks/{task_id}/retry');ai.fail=False
        asyncio.run(app.state.bid_manager.process_task(task_id))
        assert reader.calls==1 and app.state.bid_store.detail(task_id)['retained']==1
        for data,kwargs in ai.calls:
            if 'clause_index' in data: assert '初筛版本一' in kwargs['instructions'] and '初筛版本二' not in kwargs['instructions']
        public=client.get('/api/bid-check/tasks').text
        assert '初筛版本一' not in public and 'skill_instructions' not in public and 'paragraphs' not in public
        assert client.get(f'/api/bid-check/tasks/{task_id}').json()['legacy_screening'] is False
        assert app.state.bid_store.raw(task_id)['rules_snapshot']==snapshot


@pytest.mark.parametrize('content',[None,'','  '])
def test_missing_default_skill_blocks_before_upload_or_task(app,tmp_path,content):
    seed(app,count=1);root=tmp_path/'skills';root.mkdir()
    if content is not None:
        (root/'qualification-screening').mkdir();(root/'qualification-screening'/'SKILL.md').write_text(content)
    app.state.bid_skills=BidSkillRegistry(root)
    with TestClient(app) as client:
        assert create(client).status_code==503
        assert client.get('/api/bid-check/rules').status_code==503
        assert app.state.bid_store.list_tasks()==[]


def test_legacy_task_continues_fixed_prompt_without_new_coverage(app):
    seed(app,count=1)
    with TestClient(app) as client:
        task_id=create(client).json()['id']
        with app.state.bid_store.connect() as db:db.execute('UPDATE tasks SET rules_snapshot=NULL WHERE id=?',(task_id,))
        from test_bid_check import FakeReader
        app.state.bid_manager.reader=FakeReader();ai=FakeAI();app.state.ai_client=ai
        client.post(f'/api/bid-check/tasks/{task_id}/start');asyncio.run(app.state.bid_manager.process_task(task_id))
        result=app.state.bid_store.detail(task_id)
        assert result['legacy_screening'] and 'coverage' not in result['items'][0]['decision']
        assert 'clause_index' not in ai.calls[-1][0]


def test_new_stop_preserves_snapshot_and_resume_only_pending(app):
    seed(app,count=2)
    with TestClient(app) as client:
        task_id=create(client).json()['id'];reader=Reader();app.state.bid_manager.reader=reader
        class StopAI(NewAI):
            async def generate(self,*args,**kwargs):
                result=await super().generate(*args,**kwargs)
                if 'clause_index' in self.calls[-1][0]: app.state.bid_store.request_stop(task_id)
                return result
        app.state.ai_client=StopAI();client.post(f'/api/bid-check/tasks/{task_id}/start')
        asyncio.run(app.state.bid_manager.process_task(task_id))
        assert app.state.bid_store.detail(task_id)['status']=='stopped'
        ai=NewAI();app.state.ai_client=ai;client.post(f'/api/bid-check/tasks/{task_id}/start')
        asyncio.run(app.state.bid_manager.process_task(task_id))
        assert reader.calls==2 and len(ai.calls)==2 and app.state.bid_store.detail(task_id)['retained']==2


def test_missing_copy_or_equipment_proof_is_not_qualification_absence():
    for text,quote in [('供应商须具备建筑施工许可证。','测试公司目前没有建筑施工许可证复印件。'),('供应商须具备履约设备和专业技术能力。','测试公司目前没有设备。')]:
        raw=response(notice_snapshot(text),'conflict',[cite(quote)])
        assert evaluate(text,raw,[fact(quote)])['verdict']=='needs_review'


def test_negative_evidence_cannot_ignore_another_positive_source():
    text='供应商须具备建筑施工许可证。';negative='测试公司目前没有建筑施工许可证。'
    raw=response(notice_snapshot(text),'conflict',[cite(negative)])
    positive=fact('测试公司建筑施工许可证。',fact_id='K2',sources=[{'source_id':'K2:S1','quote':'测试公司建筑施工许可证。'}])
    assert evaluate(text,raw,[fact(negative),positive])['verdict']=='needs_review'


def test_html_spans_do_not_split_a_qualification_into_fake_paragraphs():
    html='<div class="vF_detail_content"><p>申请人资格要求：<span>供应商须具备</span><b>食品经营许可证</b>。</p>'+('<p>公告说明普通采购事项。</p>'*10)+'</div>'
    reader=NoticeReader(transport=httpx.MockTransport(lambda req:httpx.Response(200,text=html)))
    snap=asyncio.run(reader.read_snapshot('https://www.ccgp.gov.cn/cggg/dfgg/gkzb/202610/a.htm'))
    assert len(snap['clauses'])==1 and '供应商须具备食品经营许可证' in snap['clauses'][0]['quote']


def test_inline_numbered_subclauses_are_independent():
    text='二、申请人的资格要求：1、须具备食品经营许可证；2、須具备独立民事责任能力。'
    snap=notice_snapshot(text)
    assert len(snap['clauses'])==2
    raw=response(snap);raw['requirements']=raw['requirements'][:1]
    result=evaluate(text,raw)
    assert result['coverage']['checked']==1 and result['coverage_gaps'][0]['clause_id']==snap['clauses'][1]['id']


def test_no_qualifications_never_claims_complete_review():
    result=evaluate('公告受限，正文无法获取。')
    assert result['verdict']=='needs_review' and result['coverage']['identified']==0


def test_new_groups_list_counts_and_pagination_share_decision(app):
    seed(app,count=3)
    with TestClient(app) as client:
        task_id=create(client).json()['id']
        for i,text in enumerate(['供应商须有食品经营许可证或食品生产许可证。','供应商须有设备能力。','供应商须有建筑施工许可证。']):
            snap=notice_snapshot(text);quote='本企业食品经营许可证。' if i==0 else '本企业目前没有建筑施工许可证。'
            raw=response(snap,'conflict' if i==2 else 'unknown')
            if i==0:raw['requirements'][0].update(assessment='met',evidence=[cite(quote)])
            if i==2:raw['requirements'][0]['evidence']=[cite(quote)]
            decision=evaluate(text,raw,[fact(quote)])
            app.state.bid_store.item_update(task_id,i,decision['verdict'],decision=decision)
        before=app.state.bid_store.raw(task_id)
        detail=client.get(f'/api/bid-check/tasks/{task_id}?page_size=1').json()
        listing=client.get('/api/bid-check/tasks').json()['items'][0]
        assert detail['retained']==listing['retained']==2 and detail['needs_review']==listing['needs_review']==1
        assert detail['excluded']==listing['excluded']==1 and detail['has_next']
        assert detail['items'][0]['decision']['verdict']=='retain'
        assert client.get(f'/api/bid-check/tasks/{task_id}?page_size=1&page=2').json()['items'][0]['decision']['verdict']=='needs_review'
        assert app.state.bid_store.raw(task_id)==before
