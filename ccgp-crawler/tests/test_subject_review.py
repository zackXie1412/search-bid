import json
import pytest
from ccgp_crawler.bid_review import decision_view, profile_warnings
from ccgp_crawler.bid_models import BidDecision,CompanyProfile,validate_decision
from test_bid_check import decision,PROFILE,ANNOUNCEMENT,app,seed,create
from fastapi.testclient import TestClient

@pytest.mark.parametrize('reason',[
 '主体名称与本次投标人名称不一致，无法确认是否符合',
 '材料归属尚未确认，不能直接认定具有独立法人资格',
 '资料中有营业执照，但公司名称与本企业名称不同，因此无法确认资格',
])
def test_identity_only_does_not_trigger_review(reason):
 d=decision('needs_review','unknown');d['requirements'][0]['reason']=reason
 v=decision_view(d)
 assert v['verdict']=='retain' and not v['pending_checks']
 assert v['requirements'][0]['assessment']=='unknown'
 assert not v['requirements'][0]['review_required']
 assert '不一致' not in v['summary']+v['requirements'][0]['reason']

@pytest.mark.parametrize('reason,kept',[
 ('主体名称不一致，许可证有效期未明确，无法确认当前持证状态','有效期未明确'),
 ('信用查询截图文字不清，且查询主体与投标主体名称不一致，无法确认当前全面符合','截图文字不清'),
 ('主体关系未确认；未提供有效许可证扫描件','未提供有效许可证'),
])
def test_mixed_reasons_keep_independent_doubts(reason,kept):
 d=decision('needs_review','unknown');d['requirements'][0]['reason']=reason
 v=decision_view(d)
 assert v['verdict']=='needs_review' and kept in v['pending_checks'][0]
 assert not any(t in v['pending_checks'][0] for t in ['名称不一致','主体关系','查询主体'])

@pytest.mark.parametrize('reason',[
 '未见独立法人资格证明', '分公司未提供总公司授权书',
 '未提供非联合体声明','未提供不存在关联供应商控股关系的声明',
 '重大税收违法失信主体查询截图文字不清', '公司证据不足，需进一步核实',
])
def test_real_qualification_conditions_are_not_identity(reason):
 d=decision('needs_review','unknown');d['requirements'][0]['reason']=reason
 assert decision_view(d)['verdict']=='needs_review'
 assert reason.replace('，',',').split(',')[0] in decision_view(d)['pending_checks'][0]

def test_structured_issues_and_conflict_validation():
 d=decision('exclude','conflict');r=d['requirements'][0]
 r['review_issues']=[{'kind':'subject_identity','text':'公司名称不符'}]
 result=validate_decision(BidDecision.model_validate(d),CompanyProfile.model_validate(PROFILE),ANNOUNCEMENT)
 assert result.verdict=='retain' and result.requirements[0].assessment=='unknown'
 r['review_issues'].append({'kind':'qualification','text':'许可证已过期'})
 assert decision_view(d)['verdict']=='exclude'
 # Unsupported real conflict still becomes unknown and cannot be concealed by unrelated identity issues.
 r['company_quote']='编造原文'
 result=validate_decision(BidDecision.model_validate(d),CompanyProfile.model_validate(PROFILE),ANNOUNCEMENT)
 assert result.verdict=='needs_review' and result.requirements[0].assessment=='unknown'

def test_history_counts_pagination_and_storage_immutable(app):
 seed(app,count=3)
 with TestClient(app) as client:
  tid=create(client).json()['id'];store=app.state.bid_store
  identity=decision('needs_review','unknown');identity['requirements'][0]['reason']='主体名称不一致，无法确认资格'
  for i,(status,d) in enumerate([('needs_review',identity),('exclude',identity),('exclude',decision())]):
   store.item_update(tid,i,status,decision=d)
  with store.connect() as db:before=[tuple(r) for r in db.execute('select * from items where task_id=?',(tid,))]
  v=store.detail(tid,page_size=1)
  assert v['retained']==2 and v['needs_review']==0 and v['excluded']==1 and v['has_next']
  assert v['items'][0]['decision']['verdict']=='retain'
  assert store.detail(tid,page=2,page_size=1)['items'][0]['notice']['id']!=v['items'][0]['notice']['id']
  listed=store.list_tasks()[0]
  assert listed['needs_review']==0 and listed['retained']==2 and listed['items']==[]
  with store.connect() as db:assert before==[tuple(r) for r in db.execute('select * from items where task_id=?',(tid,))]

def test_profile_identity_warning_removed_but_ocr_remains():
 p={**PROFILE,'limitations':['主体名称不一致，无法确认资格']}
 assert profile_warnings(p,[])==[]
 warnings=profile_warnings(p,[{'kind':'image'}])
 assert len(warnings)==1 and 'OCR' in warnings[0] and '主体' not in warnings[0]
