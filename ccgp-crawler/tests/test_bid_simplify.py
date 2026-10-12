"""Concise permissive screening without bypassing evidence or rewriting task history."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from ccgp_crawler.bid_notice import notice_snapshot
from ccgp_crawler.bid_screening import ScreeningOutput, evaluate_screening, screening_display
from test_bid_screening import fact, response, cite, evaluate
from test_bid_check import app, seed, create


def material(name, value, **data):
    quote=data.pop('quote',value)
    item=fact(quote,unsafe_for_conflict=True,data={'name':name,'value':value,'kind':'material','occurrence':'actual','subject_role':'supplier','evidence_kind':'declaration',**data})
    return item


def test_structure_definitions_attachments_and_procedure_are_not_review_tasks():
    text='''二、申请人的资格要求：
投标供应商应具备政府采购法第二十二条规定的条件，提供下列材料：
1）具备独立承担民事责任的能力。
2）具备设备和专业技术能力。
采购包1落实政府采购政策需满足的资格要求如下:
采购包1特定资格要求如下:
(
重大违法记录，是指供应商因违法经营受到刑事处罚或者责令停产停业、吊销许可证或者执照、较大数额罚款等行政处罚。
根据财库〔2022〕3号文，较大数额罚款认定为200万元以上的罚款。
如国家另有规定，则适用其规定。
提供许可证或备案证明材料复印件并加盖公章。
附件1：投标人资格声明函.docx'''
    snap=notice_snapshot(text)
    substantive=[c for c in snap['clauses'] if c['mandatory']]
    assert len(substantive)==2
    result=evaluate(text)
    assert len(result['pending_checks'])==2
    assert all(not any(w in p for w in ['200万元','序号','附件1','如下']) for p in result['pending_checks'])
    assert len(result['summary'])<70 and '具备设备和专业技术能力' not in result['summary']


def test_branch_authorization_only_exits_when_confirmed_independent_company():
    text='分支机构投标的，须提供总公司和分公司营业执照副本复印件，总公司授权。'
    corporate=material('企业类型','有限责任公司（自然人投资或控股）',kind='fact')
    result=evaluate(text,evidence=[corporate])
    assert result['verdict']=='retain' and result['requirements'][0]['not_applicable']
    for evidence in [[],[material('企业类型','分公司',kind='fact')],[material('企业类型','有限责任公司',occurrence='mention')]]:
        assert evaluate(text,evidence=evidence)['verdict']=='needs_review'


@pytest.mark.parametrize('text,name,value',[
 ('供应商应具备独立承担民事责任的能力。','营业执照','已确认营业执照，企业注册登记。'),
 ('供应商须具备设备和专业技术能力。','设备能力声明函','本单位具备履行合同所需的设备和专业技术能力。'),
 ('供应商有依法缴纳税收和社会保障资金的良好记录。','税收社保声明','有依法缴纳税收和社会保障资金的良好记录。'),
 ('供应商具有良好的商业信誉和健全的财务会计制度。','财务审计报告','2020年度财务审计报告。'),
 ('供应商参加采购活动前3年内没有重大违法记录。','无重大违法记录声明','参加采购活动前3年内没有重大违法记录。'),
 ('供应商不得被列入信用中国失信被执行人名单。','信用记录查询结果','政府采购查询结果显示没有相关记录。'),
 ('供应商不接受联合体投标。','非联合体投标声明','投标本项目为非联合体投标。'),
 ('单位负责人为同一人或者存在直接控股、管理关系的供应商不得同时参加本项目。','关联关系承诺','本单位不存在直接控股、管理关系，不参加关联供应商共同投标。'),
])
def test_confirmed_material_metadata_supports_general_condition_without_identical_words(text,name,value):
    item=material(name,value,applicable_date='2020-01-01',needs_review=True,quote='本企业已填写并提交此声明。')
    item['sources'][0]['ocr']=True
    result=evaluate(text,evidence=[item])
    assert result['verdict']=='retain' and not result['pending_checks']
    assert result['requirements'][0]['assessment']=='met'
    assert result['requirements'][0]['company_quote']=='本企业已填写并提交此声明。'
    assert not result['requirements'][0]['conflict_validated']


@pytest.mark.parametrize('change',[{'occurrence':'mention'},{'occurrence':'template'},{'subject_role':'manufacturer'},{'subject_role':'customer'},{'subject_role':'unknown'},{'evidence_kind':'inference'}])
def test_actual_own_company_material_is_required_for_automatic_positive_mapping(change):
    item=material('设备能力声明函','具备设备及专业技术能力。',**change)
    assert evaluate('供应商须具备设备和专业技术能力。',evidence=[item])['verdict']=='needs_review'


def test_material_presence_does_not_prove_extra_details_or_personnel_credentials():
    item=material('设备能力声明函','具备履约设备和技术能力。')
    for text in ['供应商须至少配备10台设备。','供应商须配备3名注册营养师并具有设备技术能力。','供应商须具有指定型号ABC的设备。']:
        assert evaluate(text,evidence=[item])['verdict']=='needs_review'
    permit=material('食品经营许可证','企业已持有食品经营许可证。')
    assert evaluate('供应商须具备食品经营许可证，许可范围为热食类食品制售。',evidence=[permit])['verdict']=='needs_review'


def test_or_support_does_not_request_other_permits_or_optional_merged_certificate_path():
    text='''申请人的资格要求：供应商须有食品经营许可证或食品生产许可证。
如已实施食品药品经营许可多证合一改革的，可提供食品药品经营许可证。
如投标人的许可证书与营业执照合并办理的，提供有效备案信息截图。
如国家另有规定，则适用其规定。'''
    result=evaluate(text,evidence=[material('食品经营许可证','企业已取得食品经营许可证，编号未识别。')])
    assert result['verdict']=='retain' and not result['pending_checks']
    assert result['groups'][0]['assessment']=='met'
    assert evaluate('供应商须有食品经营许可证。',evidence=[material('食品安全责任保险承诺','如中标将提供食品经营许可证。')])['verdict']=='needs_review'


def test_small_company_policy_alternative_and_explicit_size_industry_details():
    small=material('中小企业声明函','属于小型企业，所属行业：批发业。')
    text='本项目面向小微企业采购，供应商应为小微企业、监狱企业、残疾人福利性单位。'
    assert evaluate(text,evidence=[small])['verdict']=='retain'
    assert evaluate('提供《中小企业声明函》或监狱企业证明或残疾人福利性单位声明函。',evidence=[small])['verdict']=='retain'
    assert evaluate('本项目仅限微型企业采购。',evidence=[small])['verdict']=='needs_review'
    assert evaluate('本项目面向小微企业，所属行业为：工业。',evidence=[small])['verdict']=='needs_review'


def test_ordinary_attachment_reference_vs_real_qualification_source_gap():
    text='供应商须具备设备和专业技术能力。\n附件1：投标人资格声明函.docx'
    item=material('设备能力声明函','具备设备和专业技术能力。')
    assert evaluate(text,evidence=[item])['verdict']=='retain'
    assert evaluate('供应商资格要求详见未读取的招标文件。',evidence=[item])['verdict']=='needs_review'


def test_positive_mapping_does_not_override_conflict_candidate_or_unsafe_negative_gate():
    text='供应商须具备食品经营许可证。'
    negative='本企业目前没有食品经营许可证。'
    raw=response(notice_snapshot(text),'conflict',[cite(negative)])
    evidence=[material('食品经营许可证','已持有食品经营许可证。')]
    result=evaluate(text,raw,evidence)
    assert result['verdict']=='needs_review'  # disputed conflict is not silently positive
    raw=response(notice_snapshot(text),'conflict',[cite(negative)])
    assert evaluate(text,raw,[fact(negative)])['verdict']=='exclude'


def test_saved_verbose_result_derives_concise_view_stats_and_page_without_rewriting(app):
    seed(app,count=2)
    text='申请人的资格要求：供应商须有设备和专业技术能力。\n附件1：投标人资格声明函.docx\n采购包1特定资格要求如下:'
    snapshot=notice_snapshot(text)
    # Simulate original protocol-2 output treating every fragment as mandatory.
    for c in snapshot['clauses']:c['mandatory']=True
    evidence=[material('设备能力声明函','本企业具备设备和专业技术能力。')]
    raw=response(snapshot)
    old=evaluate_screening(ScreeningOutput.model_validate(raw),snapshot,[])
    old['pending_checks']=['设备能力未核实','附件1未读取','具体子项分别评估']
    old['summary']='；'.join(old['pending_checks'])
    old['verdict']='needs_review'
    with TestClient(app) as client:
        task_id=create(client).json()['id']
        app.state.bid_store.update(task_id,evidence_index=json.dumps(evidence))
        for i in range(2):
            app.state.bid_store.save_notice_snapshot(task_id,i,snapshot)
            app.state.bid_store.item_update(task_id,i,'needs_review',decision=old)
        with app.state.bid_store.connect() as db:before=db.execute('SELECT * FROM items WHERE task_id=?',(task_id,)).fetchall()
        detail=client.get(f'/api/bid-check/tasks/{task_id}?page_size=1').json()
        listing=client.get('/api/bid-check/tasks').json()['items'][0]
        assert detail['needs_review']==listing['needs_review']==0 and detail['retained']==2 and detail['has_next']
        assert detail['items'][0]['decision']['verdict']=='retain' and not detail['items'][0]['decision']['pending_checks']
        with app.state.bid_store.connect() as db:after=db.execute('SELECT * FROM items WHERE task_id=?',(task_id,)).fetchall()
        assert [tuple(r) for r in before]==[tuple(r) for r in after]


def test_empty_old_snapshot_is_not_reinterpreted():
    decision={'protocol_version':2,'verdict':'needs_review','requirements':[]}
    assert screening_display(decision,None,[]) is decision

@pytest.mark.parametrize('name,value',[('设备能力声明函','本企业目前没有设备和专业技术能力。'),('食品经营许可证','目前未持有食品经营许可证。'),('中小企业声明函','企业不属于小微企业。'),('税收缴纳证明','本企业仍有欠税未缴。')])
def test_negative_fact_with_positive_material_title_cannot_become_support(name,value):
    req={'设备能力声明函':'供应商须具备设备和专业技术能力。','食品经营许可证':'供应商须有食品经营许可证。','中小企业声明函':'仅限小微企业参加。','税收缴纳证明':'供应商須依法缴纳税收。'}[name]
    assert evaluate(req,evidence=[material(name,value)])['verdict']=='needs_review'

@pytest.mark.parametrize('text',[
 '投标人必须对项目全部内容进行投标报价，不得缺漏。',
 '投标人应为已获取本次采购文件的投标人。',
 '首次获取招标文件须完成平台注册并上传扫描件。',
 '供应商加密投标文件与开标解密必须使用同一种方式，否则解密失败导致投标无效。',
 '使用电子营业执照功能时，请确保已安装电子营业执照小程序或APP。',
 '信用信息查询记录应当作为项目档案材料一并保存。',
 '采购代理机构将通过国家企业信用信息公示系统查询企业信用信息。',
 '供应商如认为权益受损，应以书面形式提出质疑。',
 '供应商提供虚假资料被查实的，可能面临取消中标资格和三年内禁止参与政府采购的风险。',
])
def test_tender_operations_and_hypothetical_penalties_do_not_become_company_doubts(text):
    snap=notice_snapshot('二、申请人的资格要求：\n'+text)
    for c in snap['clauses']:c['mandatory']=True
    result=evaluate_screening(ScreeningOutput.model_validate(response(snap)),snap,[])
    assert not result['pending_checks'] and result['verdict']=='retain'
