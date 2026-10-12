"""Project-local instruction snapshots, independent of Codex global skills."""
import hashlib
from pathlib import Path
from .bid_models import BidCheckError

SKILL_PATH=Path(__file__).resolve().parents[2]/'skills/knowledge_extract/company-materials/SKILL.md'

def skill_snapshot():
    try:text=SKILL_PATH.read_text(encoding='utf-8').strip()
    except OSError:raise BidCheckError('企业资料拆解 Skill 无法读取，请检查项目文件') from None
    if not text:raise BidCheckError('企业资料拆解 Skill 不能为空')
    return {'id':'company-materials','version':hashlib.sha256(text.encode()).hexdigest(),'instructions':text}
