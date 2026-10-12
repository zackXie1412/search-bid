"""Positive initial-screening support and presentation roles, never negative gates."""
from __future__ import annotations

import re


def substantive_text(text: str) -> str:
    return re.sub(r'^\s*[（(]?[\d一二三四五六七八九十]+[、.．)）]\s*', '', text).strip()


def clause_role(quote: str, context: str = '', body: str = '') -> str:
    text = substantive_text(quote)
    if context and context != quote and re.search(r'^提供(?:许可证|备案证明材料).*?(?:复印|公章)',substantive_text(context)): return 'procedure'
    if context and context != quote and re.search(r'^重大违法记录[，,：:].*(?:是指|包括)', substantive_text(context)): return 'explanation'
    # Submission/enrolment and procurement-agency operations are not enterprise qualifications.
    if re.search(r'全部内容.*投标报价|不得.*缺漏.*报价|已获取.*采购文件|获取(?:本次|采购|招标|文件)|注册并上传|开标当日|开标解密|加密投标|解密失败|加解密|电子营业执照.*(?:小程序|APP|密钥)|以书面形式提出质疑|查询记录.*档案.*保存', text): return 'procedure'
    if re.search(r'^(?:采购代理机构|采购人|资格审查人员).*(?:查询|审查|保存)',text) and not re.search(r'供应商.*(?:不得|须|必须)',text): return 'procedure'
    if re.search(r'提供虚假资料.*(?:可能|将|面临)|如.*提供虚假.*(?:处罚|禁止|取消)',text): return 'procedure'
    if re.search(r'^如非.{0,8}多证合一.*(?:组织机构代码|税务登记)',text): return 'alternative_note'
    if not re.search(r'[\u4e00-\u9fffA-Za-z]', text): return 'structure'
    if re.search(r'^(?:附件\s*\d*|相关附件)[：:].*\.(?:docx?|pdf|xlsx?|zip)\b', text, re.I): return 'attachment'
    if re.search(r'(?:资格要求(?:如下)?|应具备.*第二十二条规定的条件|满足.*第二十二条规定)[，,:：]?\s*(?:如下|提供下列材料)?[：:]?$', text):
        # An umbrella law clause remains substantive if no concrete subrequirements were read.
        if '第二十二条' not in text or len(re.findall(r'民事责任|财务|税收|社会保障|专业技术|重大违法', body)) >= 2:
            return 'structure'
    if re.search(r'^重大违法记录[，,：:].*(?:是指|包括)|较大数额罚款.*(?:认定|标准)|^根据财库.*罚款', text): return 'explanation'
    if re.search(r'^(?:如|若)(?:已实施|投标人的?许可证|国家另有|许可).*?(?:可提供|合并办理|适用其规定)', text): return 'alternative_note'
    if re.search(r'^(?:如|若)国家另有规定', text): return 'alternative_note'
    if re.search(r'^提供(?:许可证|备案证明材料|相关证明资料|材料复印件).*?(?:复印|公章)|^备案证明材料复印件|^投标函相关承诺(?:要求|格式)内容', text): return 'procedure'
    if re.search(r'^以资格审查人员.*(?:查询结果为准|查询为准)', text): return 'procedure'
    return 'qualification'


def usable_fact(fact: dict) -> bool:
    data = fact.get('data', {})
    return bool(fact.get('owned') and data.get('occurrence', 'actual') == 'actual'
                and data.get('subject_role', 'supplier') not in {'manufacturer', 'customer', 'third_party', 'unknown'}
                and data.get('evidence_kind', 'proof') in {'proof', 'declaration'}
                and any(s.get('quote', '').strip() for s in fact.get('sources', [])))


def fact_text(fact: dict) -> str:
    data = fact.get('data', {})
    return ' '.join([data.get('name', ''), data.get('value', ''), *(s.get('quote', '') for s in fact.get('sources', []))])


def corporate_company(index: list[dict]) -> bool:
    return any(usable_fact(f) and re.search(r'企业类型|主体信息|注册信息|营业执照', f.get('data', {}).get('name', ''))
               and re.search(r'有限责任公司|股份有限公司|企业法人', fact_text(f))
               and not re.search(r'分公司|分支机构', f.get('data', {}).get('value', '')) for f in index)


def inapplicable_branch(quote: str, index: list[dict]) -> bool:
    return bool(re.search(r'^(?:[\d）)\s.、]*)(?:分支机构|分公司)(?:投标|参与|参加).*?(?:授权|总公司)', quote)
                and corporate_company(index))


def positive_material_support(requirement: str, index: list[dict]) -> list[dict]:
    """Find confirmed actual materials for positive support; never upgrade missing details."""
    found = []
    for fact in index:
        if not usable_fact(fact): continue
        data = fact.get('data', {})
        text = fact_text(fact)
        name = data.get('name', '')
        if not name: continue  # Unstructured legacy text remains model-cited, not auto-mapped.
        # A confirmed negative fact or contradictory certificate status is not positive support.
        if re.search(r'不具备|不具有|尚未取得|未取得|未持有|已吊销|被吊销|已撤销|被撤销|仍处.*禁止|未缴|欠税|不属于(?:小微|中小)|不是(?:小微|中小)|属于大型企业|没有.{0,8}(?:许可证|设备|能力|资质)|无(?:设备|履约能力)', text): continue
        topic = None
        if re.search(r'食品经营许可证|食品生产许可证', requirement):
            target = '食品经营许可证' if '食品经营许可证' in requirement else '食品生产许可证'
            if target in (name or text) and not re.search(r'未提供|未见|没有|目录|空白', data.get('value', '')):
                # A generic undertaking to renew or submit a certificate is not an actual certificate.
                if not name or target in name or re.search(r'持有|已取得|具备.*'+target, data.get('value', '')): topic = target
        elif re.search(r'营业执照|独立承担民事责任|境内注册的法人|其他组织|^自然人', requirement):
            if re.search(r'营业执照|民事责任(?:的)?能力|法人资格', name or text): topic = '独立民事责任'
        elif re.search(r'设备|专业技术|履约能力', requirement):
            if re.search(r'设备|专业技术|履约能力', name or text): topic = '设备技术能力'
        elif re.search(r'税收|纳税|社会保障|社保', requirement):
            matches_tax = not re.search(r'税收|纳税', requirement) or bool(re.search(r'税收|纳税', text))
            matches_social = not re.search(r'社会保障|社保', requirement) or bool(re.search(r'社会保障|社保', text))
            if matches_tax and matches_social: topic = '税收社保'
        elif re.search(r'财务会计|商业信誉', requirement):
            if re.search(r'财务|审计|会计制度|商业信誉', name or text): topic = '财务信誉'
        elif re.search(r'重大违法', requirement):
            if re.search(r'(?:无|没有|不存在|未发生).{0,12}重大违法|无重大违法', text): topic = '无重大违法'
        elif re.search(r'信用中国|失信被执行人|税收违法失信主体|严重违法失信|禁止参加政府采购', requirement):
            if re.search(r'信用记录|信用查询|政府采购.*查询|失信', name or text) and re.search(r'没有.{0,25}相关记录|无.{0,15}(?:失信|违法|记录)|未被列入|不存在.*失信', text): topic = '信用记录'
        elif re.search(r'中小企业|小微企业|监狱企业|残疾人福利', requirement):
            # A declaration's industry and stated scale must match any explicit restrictions.
            if re.search(r'中小企业|小微企业|小型企业|微型企业|监狱企业|残疾人福利', text):
                size_ok = not re.search(r'专门面向微型|仅限微型', requirement) or bool(re.search(r'属于微型|为微型',text))
                if '小微企业' in requirement: size_ok = size_ok and bool(re.search(r'小型企业|微型企业|小微企业',text))
                industry = re.search(r'行业(?:划分)?为[：:]\s*([^。；，]+)', requirement)
                if size_ok and (not industry or industry[1].strip() in text): topic = '企业规模声明'
        elif re.search(r'不接受联合体|不得.*联合体|联合体投标', requirement):
            if re.search(r'非联合体|不.*联合体',text): topic = '非联合体声明'
        elif re.search(r'单位负责人|控股|管理关系|整体设计|规范编制|项目管理|监理|检测等服务', requirement):
            # General technical response promises do not establish conflict-of-interest compliance.
            if re.search(r'单位负责人|控股|管理关系|关联关系|整体设计|规范编制',name) and re.search(r'不参加|不参与|未参与|不为本项目|不提供|不存在',text): topic = '关联及服务限制声明'
        if topic:
            # Keep source passages separate; metadata supplies the material type, not invented quotations.
            source = fact['sources'][0]
            found.append({'fact_id': fact['fact_id'], 'source_id':source['source_id'], 'quote':source['quote'],
                          'filename':source.get('filename',''), 'location':source.get('location',''),
                          'support_type':topic})
    return found


def concise_requirement(text: str) -> str:
    if re.search(r'小微企业|中小企业|监狱企业|残疾人福利',text): return '企业规模及采购政策要求'
    if '食品经营许可证' in text and '食品生产许可证' in text: return '食品经营许可证或食品生产许可证'
    if re.search(r'失信|信用中国|禁止参加政府采购',text): return '信用及禁止参与情况'
    if '重大违法' in text: return '无重大违法记录要求'
    if re.search(r'独立承担民事责任|境内注册的法人',text): return '独立承担民事责任能力'
    text = substantive_text(text)
    text = re.split(r'[：:]',text,maxsplit=1)[0].strip('，,。；; ')
    if len(text)>100: return text[:97]+'…'
    return text or '公告资格条件'
