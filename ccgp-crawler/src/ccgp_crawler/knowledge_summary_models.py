import re
import unicodedata
from typing import Literal
from pydantic import Field
from .bid_models import StrictModel,BidCheckError,contains_quote
from .knowledge_models import Category,local_absence,reference_only

class SummaryEvidence(StrictModel):
    entry_id:str
    quote:str=Field(min_length=1,max_length=4000)
class Certificate(StrictModel):
    number:str=''
    date:str=''
    entry_ids:list[str]=Field(min_length=1)
class SummaryRecord(StrictModel):
    kind:Literal['material','fact']
    category:Category
    name:str=Field(min_length=1,max_length=200)
    value:str=Field(min_length=1,max_length=3000)
    subject:str=''
    subject_role:Literal['supplier','manufacturer','customer','third_party','unknown']='unknown'
    occurrence:Literal['actual','mention','template','unknown']='unknown'
    evidence_kind:Literal['proof','declaration','inference','unknown']='unknown'
    applicable_date:str=''
    industry:str=''
    notes:str=''
    needs_review:bool=False
    evidence:list[SummaryEvidence]=Field(min_length=1)
    certificates:list[Certificate]=Field(default_factory=list)
class SummaryOutput(StrictModel):
    records:list[SummaryRecord]
    ignored_entry_ids:list[str]=Field(default_factory=list)

_QUOTE_TYPOGRAPHY=str.maketrans({'“':'"','”':'"','‘':"'",'’':"'"})

def original_quote_match(text,quote):
    """Restore an exact source span after harmless typography changes only.

    Never remove words, digits, signs or decimal separators, and never match across
    separate sources. The stored evidence is the original text, not the AI rewrite.
    """
    def indexed(value):
        chars=[];positions=[]
        for i,char in enumerate(value):
            normalized=unicodedata.normalize('NFKC',char).translate(_QUOTE_TYPOGRAPHY)
            for c in normalized:
                if not c.isspace() and c not in {'\u200b','\ufeff'}:
                    chars.append(c);positions.append(i)
        return ''.join(chars),positions
    key,_=indexed(quote)
    if not key:return None
    source,positions=indexed(text);start=source.find(key)
    if start<0:return None
    return text[positions[start]:positions[start+len(key)-1]+1]


def reconcile_summary(output,inputs,saved_inputs):
    """Keep valid peers and fall back to source extraction for invalid AI records.

    Strict evidence/role/coverage validation remains mandatory. A rejected summary
    is replaced by the original extraction, never by a fabricated quote supporting
    the rejected value. All results still require manual confirmation.
    """
    by_id={e['id']:e for e in inputs};saved={e['id']:e for e in saved_inputs}
    valid=[];used=set();fallback=set();repairs=0
    for candidate in output.records:
        record=candidate.model_copy(deep=True)
        ids={e.entry_id for e in record.evidence}
        try:
            for evidence in record.evidence:
                source=by_id.get(evidence.entry_id)
                if not source:raise BidCheckError('引用来源编号无效')
                quote=original_quote_match(source['text'],evidence.quote)
                if quote is None or not contains_quote(saved[evidence.entry_id]['text'],quote):
                    raise BidCheckError('引用与原文不符')
                if evidence.quote!=quote:
                    repairs+=1;evidence.quote=quote
            single=SummaryOutput(records=[record],ignored_entry_ids=sorted(by_id.keys()-ids))
            validate_summary(single,inputs)
            validate_summary(single,saved_inputs)
            valid.extend(single.records)
            used.update(e.entry_id for r in single.records for e in r.evidence)
        except BidCheckError:
            fallback.update(ids & by_id.keys())
    ignored=set(output.ignored_entry_ids) & by_id.keys()
    # Unreported inputs must remain represented; do not silently treat them as absent.
    fallback.update(by_id.keys()-used-ignored)
    restored=[]
    for entry_id in sorted(fallback):
        source=saved[entry_id];data=dict(source['data'])
        if reference_only(data):continue
        if data['occurrence']=='third_party':data['occurrence']='unknown'
        warning='汇总条目未能可靠关联来源，已保留原始提取，请人工核对'
        data['needs_review']=True
        data['notes']='；'.join(filter(None,(data.get('notes',''),warning)))
        fields={k:v for k,v in data.items() if k in SummaryRecord.model_fields}
        record=SummaryRecord(**fields,evidence=[SummaryEvidence(entry_id=entry_id,quote=source['source']['quote'])],certificates=[])
        single=SummaryOutput(records=[record],ignored_entry_ids=sorted(by_id.keys()-{entry_id}))
        # A fallback must itself pass the same guards, including immutable full text.
        validate_summary(single,inputs);validate_summary(single,saved_inputs)
        restored.extend(single.records)
    records=valid+restored
    referenced={e.entry_id for r in records for e in r.evidence}
    result=SummaryOutput(records=records,ignored_entry_ids=sorted(by_id.keys()-referenced))
    validate_summary(result,inputs);validate_summary(result,saved_inputs)
    return result,{'quote_repairs':repairs,'original_fallbacks':len(restored)}


def declaration_quote(text):
    """Recognize statement sentences, excluding material titles and blank placeholders."""
    value=text.strip()
    if not value or re.search(r'[_＿]{2,}|(?:声明函|承诺函|情况表|证明材料)\s*[。.]?$',value):return False
    return bool(re.search(r'具备|具有|能够|承担|符合|属于|为.{0,4}(?:中型|小型|微型)|缴纳|无.{0,12}(?:违法|违约)|没有.{0,12}违法|(?:本单位|本公司|本企业|我司|我公司|我单位).{0,50}(?:履行|必备|保证|承诺)',value))


def restore_declaration_evidence(record,inputs):
    by_id={source['id']:source for source in inputs}
    for evidence in record.evidence:
        source=by_id[evidence.entry_id]
        data=source['data']
        if data.get('occurrence')!='actual' or data.get('evidence_kind')!='declaration':continue
        # Restore only an existing extraction quotation, never invented replacement text.
        quote=source.get('original_quote') or source.get('source',{}).get('quote') or data.get('quote','')
        if not declaration_quote(quote) or not contains_quote(source['text'],quote):continue
        topics=('设备','技术','民事责任','税收','社保','社会保障','财务','商业信誉','违法','中小企业','小型','微型','联合体','分包')
        if not any(topic in quote and topic in record.name+record.value for topic in topics):continue
        evidence.quote=quote
        return True
    return False


def validate_summary(output,inputs):
    by_id={e['id']:e for e in inputs};used=set();retained=[];reference_ids=set()
    for record in output.records:
        refs={e.entry_id for e in record.evidence}
        for evidence in record.evidence:
            source=by_id.get(evidence.entry_id)
            if not source or not contains_quote(source['text'],evidence.quote):
                raise BidCheckError('汇总引用与已有来源不符，请重试汇总')
            used.add(evidence.entry_id)
        cited=[by_id[i] for i in refs]
        if all(reference_only(e['data']) for e in cited):
            # An invented actual/declaration label cannot turn a directory into proof.
            # Keep the original clue in the snapshot, but do not block valid peers.
            reference_ids.update(refs)
            continue
        for certificate in record.certificates:
            if not set(certificate.entry_ids)<=refs:raise BidCheckError('证件字段缺少对应来源')
            for value in (certificate.number,certificate.date):
                if value and not any(contains_quote(by_id[i]['text'],value) for i in certificate.entry_ids):
                    raise BidCheckError('证件编号或日期与来源不符')
        if local_absence(record.value,' '.join(e.quote for e in record.evidence)):
            raise BidCheckError('局部未发现材料不能生成企业全局缺失结论')
        for source in cited:
            changes=source.get('human_changes',{})
            if changes.get('value') and changes['value'] not in record.value:
                raise BidCheckError('汇总不能覆盖已人工修改的内容，请保留修改后重试')
        legacy=[e for e in cited if e['data']['occurrence']=='third_party']
        if record.occurrence=='actual' and len(legacy)==len(cited):
            actual_cues=r'统一社会信用代码|注册号|法定代表人|证书编号|许可证编号|发证机关|有效期|登记机关|我司.{0,60}(?:声明|承诺|具备|具有)|本(?:公司|企业).{0,60}(?:声明|承诺|具备|具有)'
            if not any(re.search(actual_cues,e['text']) for e in legacy):
                # Old name-mismatch marks alone cannot establish how a material appeared.
                record.occurrence='unknown';record.needs_review=True
                record.notes='旧记录出现方式缺少明确原文依据，保留待核实'
        # New explicit roles/occurrences cannot be upgraded by a summary model.
        if record.subject_role=='supplier' and any(e['data'].get('subject_role') in {'manufacturer','customer','third_party'} for e in cited):
            raise BidCheckError('汇总不能将明确第三方材料归入供应商')
        if record.kind=='fact' and record.occurrence=='actual' and record.evidence_kind in {'proof','declaration'}:
            if all(e['data']['occurrence'] in {'mention','template'} for e in cited):raise BidCheckError('目录或模板不能生成能力事实')
            if record.evidence_kind=='declaration' and all(e['data']['kind']=='material' for e in cited):
                if not any(declaration_quote(e.quote) for e in record.evidence) and not restore_declaration_evidence(record,cited):
                    # The material remains usable; a title alone is not a supported ability.
                    record.evidence_kind='inference'
                    record.occurrence='unknown'
                    record.needs_review=True
                    warning='仅识别材料，尚未定位到明确能力声明正文；能力待核实'
                    record.notes='；'.join(filter(None,(record.notes,warning)))
        retained.append(record)
    ignored=set(output.ignored_entry_ids)
    if not ignored<=by_id.keys() or used|ignored!=by_id.keys() or used&ignored:
        raise BidCheckError('汇总未完整覆盖输入记录，或忽略项与已引用项重复')
    output.records=retained
    retained_ids={e.entry_id for r in retained for e in r.evidence}
    output.ignored_entry_ids=sorted(ignored | (reference_ids-retained_ids))
    return output
