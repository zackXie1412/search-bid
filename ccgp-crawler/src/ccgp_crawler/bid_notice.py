"""Deterministic, conservative notice clauses; snapshots never truncate the body."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from .bid_simplify import clause_role

QUALIFICATION = re.compile(r'资格|具备|具有|须|必须|不得|不接受|不允许|应当|应提供|需提供|满足|政府采购法|许可证|许可资质|信用|独立承担|民事责任|联合体|授权|关联供应商|仅限|专门面向|面向小微|小微企业|中小企业|依法缴纳|重大违法')
SECTION = re.compile(r'^\s*[一二三四五六七八九十]+[、.．]\s*(.*)')
SCORING = re.compile(r'加分|评分|得分|评审因素|评分标准')
EXCEPTION = re.compile(r'除外|除非|但是|但(?:对|如|已|可|不|应|须)|豁免|无需|免于|不适用')
ATTACHMENT_GAP = re.compile(r'(?:资格|资质|申请人|投标人|供应商).{0,100}(?:详见|见附件|以.*为准|参见)(?:.{0,30})(?:附件|招标文件|采购文件)|(?:资格|资质)(?:要求)?[：:，\s]*(?:详见|见|参见).*(?:附件|文件)|(?:详见|参见|见附件).{0,30}(?:附件|招标文件|采购文件).{0,30}(?:资格|资质)')


def safe_attachment_url(url: str, base: str) -> str | None:
    candidate = urljoin(base, url.strip())
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            return None
        import ipaddress
        host = parsed.hostname.lower()
        if host == 'localhost' or host.endswith(('.local', '.localhost')) or '.' not in host:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return candidate
    except ValueError:
        return None


def attachment_entries(content, base: str) -> list[dict]:
    entries = []
    for a in content.select('a'):
        href, name = a.get('href', ''), a.get_text(' ', strip=True)
        if not (re.search(r'附件|下载|招标文件|采购文件', name) or re.search(r'\.(?:pdf|docx?|xlsx?|zip|rar)(?:\?|$)', href, re.I)):
            continue
        entry = {'name': name or '公告附件', 'url': safe_attachment_url(href, base) if href else None, 'read': False}
        if entry not in entries:
            entries.append(entry)
    return entries


def _split_relation(text: str):
    """Only explicit top-level logical connectives. Preserve all context separately."""
    depth, start, parts, operators = 0, 0, [], []
    pos = 0
    while pos < len(text):
        char = text[pos]
        if char in '（(': depth += 1
        elif char in '）)': depth = max(0, depth-1)
        if depth == 0:
            match = re.match(r'并且|而且|且|或者|或(?!以上|以下|等于)|同时(?:须|应|必须)', text[pos:])
            if match:
                parts.append(text[start:pos].strip()); operators.append('or' if '或' in match[0] else 'and')
                pos += len(match[0]); start = pos; continue
        pos += 1
    if not operators: return None
    parts.append(text[start:].strip())
    if any(not p for p in parts): return None
    return ('unknown' if len(set(operators))>1 else operators[0]), parts


def notice_snapshot(text: str, attachments=()) -> dict:
    paragraphs = [{'id': f'P{i+1}', 'text': part} for i, part in enumerate(text.splitlines()) if part.strip()]
    clauses, groups, roots = [], [], []
    in_section, sequence = False, 0

    def add_expression(body, cid, paragraph_id, context, scoring):
        # Strip a full enclosing bracket only; inside branches remain exact source substrings.
        body = body.strip()
        if body and body[0] in '（(':
            level, closes = 0, -1
            for i, ch in enumerate(body):
                if ch in '（(': level += 1
                elif ch in '）)':
                    level -= 1
                    if level == 0: closes=i; break
            if closes==len(body)-1 or (closes==len(body)-2 and body[-1] in '。；;'):
                return add_expression(body[1:closes], cid, paragraph_id, context, scoring)
        relation = _split_relation(body)
        # “须具有（A 或 B）”: common prefix is context, branches retain literal citations.
        if relation is None:
            match = re.fullmatch(r'([^（(]*)([（(].*[）)])[。；;]?', body)
            if match and _split_relation(match[2][1:-1]):
                return add_expression(match[2], cid, paragraph_id, context, scoring)
        if relation:
            operator, parts = relation
            members = [add_expression(part, f'{cid}.{j+1}', paragraph_id, context, scoring) for j, part in enumerate(parts)]
            gid = 'G'+cid[1:]
            groups.append({'id':gid,'operator':operator,'members':members,'context':context,'exception':bool(EXCEPTION.search(context)), 'mandatory':not scoring})
            return gid
        role=clause_role(body,context,text)
        clauses.append({'id':cid,'paragraph_id':paragraph_id,'quote':body,'context':context,
                        'category_hint':'scoring' if scoring else 'qualification','mandatory':not scoring and role=='qualification',
                        'display_role':role,'reference_only':role!='qualification',
                        'source_gap':bool(ATTACHMENT_GAP.search(context)), 'exception':bool(EXCEPTION.search(context)),
                        'relation_unclear':bool(re.search(r'或|且',body) and re.search(r'（|\(',body))})
        return cid

    for paragraph in paragraphs:
        line = paragraph['text'].strip()
        # Inline next section headings are handled before clause splitting as well.
        segments = re.split(r'(?=[三四五六七八九十]+[、．]\s*(?:获取|提交|公告|联系|其他|开标))|(?<![\d.])(?=[（(]?\d{1,2}[、．)）](?!\d))', line)
        for segment in segments:
            heading = SECTION.match(segment)
            if heading: in_section = bool(re.search(r'资格|申请人',heading[1]))
            parts, start, depth = [], 0, 0
            for pos, char in enumerate(segment):
                if char in '（([': depth += 1
                if char in '）)]': depth=max(0,depth-1)
                if char in '。；;' and depth==0:
                    parts.append(segment[start:pos+1]); start=pos+1
            if start<len(segment): parts.append(segment[start:])
            for part in parts:
                body = re.sub(r'^.*?(?:申请人[的]?资格要求|本项目[的]?特定资格要求|供应商资格要求)[：:]\s*','',part).strip()
                if not body or re.fullmatch(r'[\d一二三、.\s]*.*资格要求[：:]?',body): continue
                if not (in_section or QUALIFICATION.search(body) or SCORING.search(body)): continue
                if not QUALIFICATION.search(body) and not SCORING.search(body) and re.search(r'公告说明|普通采购事项|^附件\d*|^采购文件附件|^获取|^下载',body): continue
                scoring = bool(SCORING.search(body)) and not re.search(r'必须|须|不得|资格条件',body)
                sequence += 1
                roots.append(add_expression(body,f'C{sequence}',paragraph['id'],part,scoring))
    return {'body':text,'read_at':datetime.now(timezone.utc).isoformat(),'sha256':hashlib.sha256(text.encode()).hexdigest(),
            'paragraphs':paragraphs,'clauses':clauses,'groups':groups,'roots':roots,
            'attachments':list(attachments),'scope':'announcement_body_only'}
