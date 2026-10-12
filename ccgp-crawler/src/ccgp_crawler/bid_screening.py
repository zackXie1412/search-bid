"""Protocol 2: coverage and evidence gates independent of model verdicts."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from .bid_models import StrictModel, ReviewIssue, contains_quote, text_key
from .bid_review import subject_only, visible_reason, requirement_issues
from .bid_simplify import clause_role, inapplicable_branch, positive_material_support, concise_requirement, fact_text


class EvidenceCitation(StrictModel):
    fact_id: str
    source_id: str
    quote: str = Field(min_length=1)


class ScreeningRequirement(StrictModel):
    condition_id: str = Field(min_length=1)
    clause_ids: list[str] = Field(min_length=1)
    category: Literal['general', 'specific', 'scoring']
    requirement: str = Field(min_length=1)
    mandatory: bool
    announcement_quote: str = Field(min_length=1)
    assessment: Literal['met', 'conflict', 'unknown']
    evidence: list[EvidenceCitation]
    conflict_type: Literal['none', 'explicit_absence', 'revoked', 'prohibited', 'numeric', 'other']
    reason: str
    review_issues: list[ReviewIssue] = Field(default_factory=list)


class ScreeningGroup(StrictModel):
    id: str
    operator: Literal['and', 'or', 'unknown']
    members: list[str] = Field(min_length=1)
    exception_quote: str = ''
    exception_handled: bool = False


class ScreeningOutput(StrictModel):
    requirements: list[ScreeningRequirement]
    groups: list[ScreeningGroup] = Field(default_factory=list)


def build_evidence_index(profile: dict, sources: list[dict], records=()) -> list[dict]:
    by_id = {s['source_id']: s for s in sources}
    result = []
    for i, fact in enumerate(profile['facts']):
        source = by_id.get(fact['source_id'], {})
        record = records[i] if i < len(records) else {}
        data = record.get('data', {})
        provenance = source.get('provenance') or [{'quote': fact['quote'], 'location': source.get('location', ''), 'filename': source.get('filename', '')}]
        result.append({'fact_id': fact['id'], 'statement': fact['statement'], 'company_name': profile['company_name'],
                       'data': data, 'owned': record.get('owned', not bool(records)),
                       'unsafe_for_conflict': bool(source.get('unsafe_for_conflict') or source.get('kind')=='image'),
                       'sources': [{**p, 'source_id': f"{fact['id']}:S{j+1}"} for j, p in enumerate(provenance)]})
    return result


# Broad semantic mapping is conservative: an unrelated genuine quote cannot support a condition.
MATERIAL_TOPICS = {
    '食品经营': ('食品经营',), '食品生产': ('食品生产',), '建筑施工': ('建筑施工', '建筑业企业',),
    '独立承担民事责任': ('营业执照', '独立承担民事责任', '法人资格'),
    '设备和专业技术': ('设备', '技术能力', '履约能力'),
    '设备及专业技术': ('设备', '技术能力', '履约能力'),
    '设备': ('设备',), '专业技术': ('专业技术', '技术能力'),
    '纳税': ('纳税', '税收'), '税收': ('税收', '纳税'), '社会保障': ('社会保障', '社保'),
    '社保': ('社保', '社会保障'), '财务': ('财务', '会计', '审计'), '商业信誉': ('信誉', '信用'),
    '信用': ('信用', '失信', '信誉'), '违法': ('违法', '犯罪'), '中小企业': ('中小企业', '小微企业'),
    '联合体': ('联合体',), '授权': ('授权',), '关联': ('关联',), '政府采购法': ('第二十二条', '资格声明'),
}


def relevant(requirement: str, evidence: str) -> bool:
    for topic, aliases in MATERIAL_TOPICS.items():
        if topic in requirement:
            return any(alias in evidence for alias in aliases)
    # Exact certificate names or a meaningful shared fragment, never just “许可证/能力”.
    targets = re.findall(r'[\u4e00-\u9fff]{2,15}(?:许可证|资质证书|资格证书)', requirement)
    for target in targets:
        target = re.sub(r'^(?:供应商|投标人|申请人)?(?:必须|须|应)?(?:具备|具有|提供|有效的|有效|的)*', '', target)
        if target in evidence and len(target) > 4: return True
    filtered = re.sub(r'供应商|投标人|申请人|资格|具备|具有|提供|有效|要求|必须|许可证|证书|能力|满足|应当|本项目', '', text_key(requirement))
    return any(filtered[i:i+4] in evidence for i in range(max(0, len(filtered)-3)) if re.fullmatch(r'[\u4e00-\u9fff]{4}', filtered[i:i+4]))


def explicit_negative(requirement: str, quote: str, conflict_type: str, read_at: str) -> bool:
    # Time must be explicit and current, not inferred from an undated historical record.
    current = bool(re.search(r'目前|当前|现仍|现未|现不|至今|现已', quote))
    if not current:
        date = read_at[:10]
        current = date in quote
        period = re.search(r'(\d{4}-\d{2}-\d{2}).{0,8}(?:至|到).{0,8}(\d{4}-\d{2}-\d{2})', quote)
        if period: current = period[1] <= date <= period[2]
    if not current or re.search(r'曾经|曾被|此前|当时|已恢复|解除|不再|未被|没有被', quote): return False
    patterns = {'explicit_absence': r'(?:不具备|不具有|没有|尚未取得|未取得|无)([^。；，]{2,80})',
                'revoked': r'([^。；，]{2,80})(?:被吊销|已吊销|被撤销|已撤销)',
                'prohibited': r'(?:禁止|不得)([^。；，]{2,80})(?:参与|参加)|(?:禁止|不得)(?:参与|参加)([^。；，]{2,80})'}
    for match in re.finditer(patterns.get(conflict_type, r'(?!)'), quote):
        target = ''.join(v for v in match.groups() if v)
        if conflict_type == 'prohibited':
            if re.search(r'采购|投标|招标', requirement) and re.search(r'采购|投标|招标', target + match[0]): return True
        elif relevant(requirement, target):
            # Missing a copy/declaration is not explicit absence of the underlying qualification.
            if re.search(r'材料|证明|复印|扫描|文件|声明函|承诺函|记录|目录', target): continue
            if re.search(r'设备|技术|履约', requirement):
                if not re.search(r'(?:设备|技术|履约).{0,12}能力', target): continue
            if re.search(r'许可证|资质证书|资格证书',requirement) and not re.search(r'许可证|资质证书|资格证书',target): continue
            return True
    return False


def detail_supported(requirement: str, quotes: list[str]) -> bool:
    # Personnel qualifications need the named credential, not a generic equipment declaration.
    credentials=re.findall(r'(?:注册[^，。；（）]{0,8}师|[^，。；（）]{2,10}资格证书|健康证明|健康证)',requirement)
    if any(credential not in ''.join(quotes) for credential in credentials): return False
    details = re.findall(r'(?<!同)(?:[一二三四五六七八九十]+|\d+(?:\.\d+)?)\s*(?:级|台|人|名|万元|元|项|年|套|辆|平方米)', requirement)
    joined = text_key(' '.join(quotes))
    if any(text_key(detail) not in joined for detail in details): return False
    # Named personnel qualifications and explicit licence scope must appear in the evidence.
    for match in re.finditer(r'(?:型号|经营范围|许可范围)[：:为]?([^，。；（）]{2,25})', requirement):
        if text_key(match[1]) not in joined: return False
    return True


def unresolved_positive(requirement, index, snapshot):
    for fact in index:
        d=fact.get('data',{})
        if not fact.get('owned') or d.get('occurrence','actual')!='actual' or d.get('subject_role','supplier') in {'manufacturer','customer','third_party'}: continue
        for source in fact['sources']:
            quote=source.get('quote','')
            if relevant(requirement,quote) and not re.search(r'不具备|不具有|没有|未取得|吊销|撤销|禁止|不得|过期|失效',quote):
                return True
    return False


def evaluate_screening(output: ScreeningOutput, snapshot: dict, evidence_index: list[dict]) -> dict:
    snapshot = {**snapshot, 'clauses': [dict(c) for c in snapshot['clauses']]}
    for clause in snapshot['clauses']:
        role = clause_role(clause['quote'],clause.get('context',''),snapshot['body'])
        if role != 'qualification':
            clause['mandatory']=False
            clause['reference_only']=True
            clause['display_role']=role
        if inapplicable_branch(clause['quote'],evidence_index):
            clause['mandatory']=False
            clause['not_applicable']=True
    clauses = {c['id']: c for c in snapshot['clauses']}
    facts = {f['fact_id']: f for f in evidence_index}
    by_clause, requirements, gaps = {}, [], []
    for original in output.requirements:
        r = original.model_dump()
        cited_clauses = [clauses[cid] for cid in r['clause_ids'] if cid in clauses]
        # One leaf per condition, exact leaf quote; a heading quote does not cover subclauses.
        coverage_ok = (len(cited_clauses)==1 and len(r['clause_ids'])==1 and r['condition_id']==cited_clauses[0]['id']
                       and text_key(r['announcement_quote'])==text_key(cited_clauses[0]['quote']))
        if not coverage_ok or r['condition_id'] in by_clause:
            continue
        clause = cited_clauses[0]
        by_clause[clause['id']] = r
        r['mandatory'] = clause['mandatory']
        if clause['mandatory'] and r['category']=='scoring':
            r['category']='specific'; r['assessment']='unknown'; gaps.append({'clause_id': clause['id'], 'text': '明确资格条款被归为评分项，尚未完成核对'})
        if not clause['mandatory'] and clause.get('category_hint')=='scoring': r['category']='scoring'
        r['reference_only']=clause.get('reference_only',False)
        r['not_applicable']=clause.get('not_applicable',False)
        valid, safe_negative = [], False
        for citation in r['evidence']:
            fact = facts.get(citation['fact_id'])
            source = next((s for s in fact['sources'] if s['source_id']==citation['source_id']), None) if fact else None
            if not source or not contains_quote(source.get('quote',''), citation['quote']): continue
            # Relation is checked on the actual quoted passage, not just the material name/statement.
            if not relevant(clause['quote'], citation['quote']): continue
            data = fact.get('data', {})
            allowed = fact.get('owned') and data.get('occurrence','actual')=='actual' and data.get('subject_role','supplier') not in {'manufacturer','customer','third_party'} and data.get('evidence_kind','proof') not in {'inference','unknown'}
            if not allowed: continue
            valid.append({**citation, 'filename': source.get('filename',''), 'location': source.get('location','')})
            unsafe = fact.get('unsafe_for_conflict') or source.get('ocr') or source.get('visual') or data.get('needs_review') or (data.get('subject') and data['subject']!=fact['company_name']) or data.get('applicable_date')
            if not unsafe and explicit_negative(clause['quote'], citation['quote'], r['conflict_type'], snapshot['read_at']): safe_negative = True
        identity = subject_only(r)
        r['evidence_validated'] = bool(valid)
        r['evidence'] = valid
        r['company_fact_ids'] = list(dict.fromkeys(v['fact_id'] for v in valid))
        r['company_quote'] = valid[0]['quote'] if valid else ''
        r['conflict_validated'] = bool(safe_negative and not identity and clause['mandatory'] and not clause['exception'] and not clause['relation_unclear'] and not clause['source_gap'] and not unresolved_positive(clause['quote'], evidence_index, snapshot))
        if r['assessment']=='met' and (not valid or not detail_supported(clause['quote'], [v['quote'] for v in valid]) or any(explicit_negative(clause['quote'], c['quote'], kind, snapshot['read_at']) for c in valid for kind in ('explicit_absence','revoked','prohibited'))):
            r['assessment']='unknown'; r['reason']='企业证明不足或引用与本条件不对应，需核对对应材料'
            if not identity: r['review_issues']=[]
        if r['assessment']=='conflict' and not r['conflict_validated']:
            r['assessment']='unknown'
            if not identity: r['reason']='未通过明确否定性证据校验，需核对对应资格；不据此筛除'; r['review_issues']=[]
        support = positive_material_support(clause['quote'],evidence_index) if clause['mandatory'] else []
        # Confirmed typed materials support general initial screening independently of model wording.
        # Additional grade/amount/personnel details still need matching facts, never inferred.
        support = [v for v in support if detail_supported(clause['quote'], [fact_text(facts[v['fact_id']])])]
        supported_policy_or = bool(support and re.search(r'中小企业声明函|监狱企业|残疾人福利',clause['quote']) and '或' in clause['quote'])
        if support and original.assessment!='conflict' and r['assessment']=='unknown' and not clause['source_gap'] and (not clause['relation_unclear'] or supported_policy_or):
            r.update(assessment='met',reason='已确认实际材料或已填写声明支持本项资格初筛',review_issues=[],
                     evidence=support,evidence_validated=True,company_fact_ids=list(dict.fromkeys(v['fact_id'] for v in support)),company_quote=support[0]['quote'])
            identity=False
        if (clause['source_gap'] or (clause['relation_unclear'] and not supported_policy_or) or (clause['exception'] and r['assessment']!='met')) and clause['mandatory']:
            r['assessment']='unknown'; r['conflict_validated']=False
            reason = '资格来源缺口：正文要求详见附件或招标文件，相关内容未读取' if clause['source_gap'] else '条件关系或例外适用性尚未可靠核对'
            gaps.append({'clause_id': clause['id'], 'text': reason}); r['reason']=reason; r['review_issues']=[]; identity=False
        if r.get('reference_only'): r.update(reason='公告说明或材料提交方式，仅供参考',review_issues=[],assessment='unknown')
        if r.get('not_applicable'): r.update(reason='已确认企业为独立法人，本项分支机构投标条件不适用',review_issues=[],assessment='unknown')
        r['review_issues'] = [i for i in requirement_issues(r) if i['kind']=='qualification']
        r['reason'] = visible_reason(r) or '本项未作资格确认'
        r['identity_only'] = identity
        requirements.append(r)
    for cid, clause in clauses.items():
        if cid in by_clause: continue
        if not clause['mandatory']: continue
        reason = '资格来源缺口：正文要求详见附件或招标文件，相关内容未读取' if clause['source_gap'] else '该资格条款尚未完成核对'
        gaps.append({'clause_id': cid, 'text': reason})
        requirements.append({'condition_id':cid,'clause_ids':[cid],'category':'specific','requirement':clause['quote'], 'mandatory':True,
                             'announcement_quote':clause['quote'],'assessment':'unknown','evidence':[],'company_fact_ids':[], 'company_quote':'',
                             'reason':reason,'review_issues':[], 'conflict_type':'none','conflict_validated':False, 'evidence_validated':False})
    if not clauses:
        gaps.append({'clause_id':'', 'text':'未能可靠识别资格条款，需查看公告或招标文件'})
    reqs = {r['condition_id']:r for r in requirements}
    groups, pending = [], []
    model_groups = {g.id:g for g in output.groups}
    indexed_groups = {g['id']:g for g in snapshot['groups']}
    evaluated = {}

    def leaf_ids(node):
        if node in indexed_groups:
            return {leaf for member in indexed_groups[node]['members'] for leaf in leaf_ids(member)}
        return {node} if node in clauses else set()

    def state(node):
        if node in evaluated: return evaluated[node]
        if node in reqs:
            r=reqs[node]
            needs=bool(r['mandatory'] and r['assessment']=='unknown' and not r.get('identity_only'))
            r['review_required']=needs
            value=(r['assessment'] if r['mandatory'] else 'met', needs, concise_requirement(r['requirement'])+'：'+r['reason'])
        elif node in indexed_groups:
            group=indexed_groups[node]; candidate=model_groups.get(node)
            valid_relation=bool(candidate and candidate.operator==group['operator'] and group['operator']!='unknown'
                and set(candidate.members)==set(group['members']) and len(candidate.members)==len(group['members'])
                and not candidate.exception_quote
                and all(member in by_clause or member in indexed_groups for member in group['members']))
            children=[state(member) for member in group['members']]
            states=[child[0] for child in children]
            only_references=all(not any(clauses[leaf]['mandatory'] for leaf in leaf_ids(member)) for member in group['members'])
            # The index supplies explicit alternatives; positive support of a known OR branch
            # does not depend on the model restating the group or citing every unused branch.
            positive_or=group['operator']=='or' and 'met' in states and not only_references and candidate is not None and candidate.operator=='or'
            if only_references or not group.get('mandatory', True): assessment='met'
            elif positive_or: assessment='met'; valid_relation=True
            elif not valid_relation: assessment='unknown'
            elif group['exception']: assessment='unknown'
            elif group['operator']=='or': assessment='met' if 'met' in states else 'conflict' if all(v=='conflict' for v in states) else 'unknown'
            else: assessment='conflict' if 'conflict' in states else 'met' if all(v=='met' for v in states) else 'unknown'
            review=bool(group.get('mandatory',True) and assessment=='unknown' and (not valid_relation or any(c[1] for c in children)))
            groups.append({**group,'assessment':assessment,'validated':valid_relation})
            if not valid_relation and group.get('mandatory',True) and not only_references: gaps.append({'clause_id':node,'text':'条件组缺成员、例外或条件关系未完成核对'})
            reading_gap=any('资格来源缺口' in child[2] for child in children)
            reason='资格来源缺口：相关附件或招标文件未读取' if reading_gap else '条件关系尚未完成核对' if not valid_relation else '对应证明材料尚需核对'
            value=(assessment,review,concise_requirement(group['context'])+'：'+reason)
        else: value=('unknown',True,'该资格条件尚未完成核对')
        evaluated[node]=value
        return value

    root_states=[state(node) for node in snapshot.get('roots', list(reqs))]
    conflict=any(value[0]=='conflict' for value in root_states)
    pending.extend(value[2] for value in root_states if value[1])
    for g in output.groups:
        if g.id not in indexed_groups:
            gaps.append({'clause_id':g.id,'text':'模型条件关系无法与公告条款索引对应，需核对'})
            pending.append('模型条件关系无法与公告条款索引对应，需核对')
    if not clauses: pending.append('未能可靠识别资格条款，需查看公告或招标文件')
    def affected_leaves(node):
        if node in indexed_groups:
            return {leaf for member in indexed_groups[node]['members'] for leaf in affected_leaves(member)}
        return {node}
    missing_coverage=set()
    for gap in gaps: missing_coverage.update(affected_leaves(gap['clause_id']))
    pending = list(dict.fromkeys(pending))
    verdict = 'exclude' if conflict else 'needs_review' if pending else 'retain'
    return {'protocol_version':2,'review_scope_version':3,'verdict':verdict,
            'summary':'存在通过校验的明确强制资格冲突' if conflict else f'未发现明确硬性资格冲突，建议关注；有 {len(pending)} 项资格需核实。' if pending else '未发现明确硬性资格冲突，建议关注。',
            'presentation_version':1,'requirements':requirements,'groups':groups,'pending_checks':pending,'coverage_gaps':gaps,
            'coverage':{'identified':sum(c['mandatory'] for c in clauses.values()),'checked':sum(c['mandatory'] and cid not in missing_coverage for cid,c in clauses.items()),'scope':'仅核对已识别条款，非完整法律审查'},
            'reading':{'scope':snapshot['scope'],'read_at':snapshot['read_at'],'sha256':snapshot['sha256'],'attachments':snapshot['attachments']}}


def screening_display(decision: dict, snapshot: dict | None, evidence_index: list[dict] | None) -> dict:
    """Derive current concise view from frozen inputs without overwriting saved conclusions."""
    if decision.get('protocol_version') != 2 or not snapshot or not evidence_index:
        return decision
    from pydantic import ValidationError
    try:
        requirements=[]
        for original in decision.get('requirements',[]):
            r={key:value for key,value in original.items() if key in ScreeningRequirement.model_fields}
            r['evidence']=[{key:value for key,value in e.items() if key in EvidenceCitation.model_fields} for e in r.get('evidence',[])]
            requirements.append(r)
        groups=[{key:value for key,value in group.items() if key in ScreeningGroup.model_fields} for group in decision.get('groups',[])]
        output=ScreeningOutput.model_validate({'requirements':requirements,'groups':groups})
        return evaluate_screening(output,snapshot,evidence_index)
    except (ValidationError, KeyError, TypeError, ValueError):
        # Older incomplete snapshots retain their already validated result.
        return decision
