"""Derived review views; saved decisions and evidence remain immutable."""
from __future__ import annotations

import re

REVIEW_SCOPE_VERSION = 3
INTERNAL_LIMITATION = '不作为明确硬性冲突依据'

# Match identity/ownership doubts, never the mere words 公司 or 主体 (e.g. 失信主体).
IDENTITY = re.compile(
    r'(?:主体|企业|公司|投标人|供应商|证照|材料|文件|落款|名称).{0,24}(?:名称.{0,10}(?:不一致|不同|不符)|主体.{0,10}(?:不一致|不同|不符)|关系.{0,8}(?:未确认|不明|不清|待确认)|归属|对应关系|主体一致性)'
    r'|(?:主体|公司|企业|投标人|供应商)?名称.{0,18}(?:不一致|不同|不符|差异|核对|核实)'
    r'|(?:主体关系|材料归属|主体对应|简称|全称|品牌名称|关联公司|其他公司名称|另一企业名称|其他企业名称)'
    r'|已按主体记录限定解读|不扩大证明范围'
    r'|(?:主体|落款|签署主体).{0,10}(?:不一致|不明|未确认|未明确|无法确认|核实)'
    r'|(?:以|针对).{0,18}(?:投标人|投标主体|本企业|本公司).{0,12}(?:名称|为主体)'
)
DEPENDENT = re.compile(r'^(?:且|因此|因而|故|所以|从而|不能据此|无法据此|不能直接认定|无法直接认定|不能确认|无法确认|尚不能确认|尚无法确认|需核验原件及|不能认定)')
INDEPENDENT = re.compile(r'未提供|未见|缺少|缺乏|缺失|未找到|仅.{0,8}目录|目录.{0,8}提及|文字不清|截图不清|有效期|过期|已到期|信用记录|查询结果|持证状态|当前有效')


def legacy_issues(reason: str) -> list[dict]:
    """Split identity causes and their consequences; retain separate substantive doubts."""
    parts = re.split(r'[，,；;。\n]+|(?=且(?:查询主体|部分文件主体|主体名称|另有关联))', reason)
    issues = []
    identity_cause = False
    for part in parts:
        text = part.strip()
        if not text:
            continue
        identity = bool(IDENTITY.search(text))
        if not identity and identity_cause and DEPENDENT.search(text):
            identity = not bool(INDEPENDENT.search(text))
        kind = 'subject_identity' if identity else 'qualification'
        issues.append({'kind': kind, 'text': text})
        # An independent clause resets the causal context.
        identity_cause = identity
    if any(i['kind']=='subject_identity' for i in issues):
        # Supporting background is not an independent unresolved issue.
        issues = [i for i in issues if i['kind']=='subject_identity' or INDEPENDENT.search(i['text']) or re.search(r'但|不清|不足|需|尚|未|无法|不能',i['text'])]
    return issues


def requirement_issues(requirement: dict) -> list[dict]:
    provided = requirement.get('review_issues')
    if provided:
        # Apply the same sanitization even if the model mislabels an issue.
        return [part for issue in provided for part in (
            [{'kind':'subject_identity','text':issue['text']}] if issue['kind']=='subject_identity'
            else legacy_issues(issue['text']))]
    return legacy_issues(requirement.get('reason', ''))


def visible_reason(requirement: dict) -> str:
    return '，'.join(i['text'] for i in requirement_issues(requirement) if i['kind']=='qualification')


def subject_only(requirement: dict) -> bool:
    issues = requirement_issues(requirement)
    return bool(issues) and all(i['kind']=='subject_identity' for i in issues)


def project_pending_checks(requirements: list[dict]) -> list[str]:
    groups = {}
    for requirement in requirements:
        if not requirement.get('mandatory') or requirement.get('assessment') != 'unknown' or subject_only(requirement):
            continue
        name = requirement['requirement'].strip()
        key = re.sub(r'\s+', '', name)
        reason = visible_reason(requirement)
        if not reason or INTERNAL_LIMITATION in reason:
            reason = '需核对本企业证明材料及其适用范围'
        group = groups.setdefault(key, {'name': name, 'reasons': []})
        if reason != name and reason not in group['reasons']:
            group['reasons'].append(reason)
    return [f"{g['name']}：{'；'.join(g['reasons'])}" if g['reasons'] else g['name'] for g in groups.values()]


def decision_view(decision: dict) -> dict:
    if decision.get('protocol_version') == 2:
        # Protocol 2 is already sanitized and evaluated with condition groups and coverage.
        # Recomputing independent leaves here would turn an OR into AND.
        return decision
    requirements = []
    for original in decision['requirements']:
        r = dict(original)
        only_identity = subject_only(r)
        r['review_issues'] = [i for i in requirement_issues(r) if i['kind']=='qualification']
        r['reason'] = visible_reason(original) or '本项未提供可展示的独立资格判断依据'
        if only_identity and r['assessment']=='conflict':
            r['assessment']='unknown'
        r['review_required'] = bool(r['mandatory'] and r['assessment']=='unknown' and not only_identity)
        requirements.append(r)
    # Use original issues so an empty filtered list cannot resurrect identity-only doubts.
    pending = project_pending_checks(decision['requirements'])
    conflict = any(r['mandatory'] and r['assessment']=='conflict' for r in requirements)
    verdict = 'exclude' if conflict else 'needs_review' if pending else 'retain'
    summary = ('存在明确的强制资格冲突' if conflict else
               '未发现有证据的硬性资格冲突，建议关注；请核实：'+'；'.join(pending) if pending else
               '未发现有证据的硬性资格冲突，建议关注；不代表已证明全部资格符合')
    return {**decision, 'requirements':requirements, 'pending_checks':pending, 'verdict':verdict, 'summary':summary}


def profile_warnings(profile: dict | None, sources: list[dict] | None) -> list[str]:
    if not profile:
        return []
    limitations = [v for v in profile.get('limitations', []) if v.strip() and visible_reason({'reason':v})]
    warnings = []
    historical = any(re.search(r'；时间：(?!未知；|未注明；)[^；]+；行业：', fact.get('statement','')) for fact in profile.get('facts', []))
    provenance = [p for source in sources or [] for p in source.get('provenance', [])]
    if any(p.get('ocr') or p.get('visual') for p in provenance) or any(s.get('kind') == 'image' for s in sources or []):
        warnings.append('部分档案来自 OCR 或图片识别，关键文字请对照原件核实；识别来源本身不表示资格冲突。')
    if historical:
        warnings.append('档案包含历史或有适用日期的声明、证照，请按当前公告要求核对适用时间。历史记录不自动代表当前状态。')
    if any('尚未完整拆解' in v or 'OCR 待核实' in v for v in limitations):
        warnings.append('部分来源文档尚未完整拆解或存在未识别内容，当前档案不能代表原文件全部内容；未处理内容不表示材料缺失。')
    if limitations:
        warnings.append('企业档案中部分材料或事实仍需核对证据或适用范围，请在企业知识库集中核对。项目下方仅列出与该公告强制资格有关的事项。')
    return warnings
