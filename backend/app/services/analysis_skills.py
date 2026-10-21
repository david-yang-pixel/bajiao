"""Project-local, reviewed skill instructions. Never download or execute skills at runtime."""
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'analysis_skills'
SKILLS = [
    {'name':'VCP 波段筛选','file':'vcp-swing.md','revision':'cb0f3aadffed6d1089db4b224162c964368baa46',
     'source_url':'https://github.com/tradermonty/claude-trading-skills/blob/cb0f3aadffed6d1089db4b224162c964368baa46/skills/vcp-screener/SKILL.md',
     'description':'采用趋势、振幅收缩、缩量与突破位置因子；不是完整 VCP 认证'},
    {'name':'CANSLIM 成长性复核','file':'canslim-growth.md','revision':'cb0f3aadffed6d1089db4b224162c964368baa46',
     'source_url':'https://github.com/tradermonty/claude-trading-skills/blob/cb0f3aadffed6d1089db4b224162c964368baa46/skills/canslim-screener/SKILL.md',
     'description':'采用季度盈利/营收增长和相对强弱；缺失机构持仓等因素不假装已验证'},
    {'name':'研究与催化剂复核','file':'research-catalysts.md','revision':'574ed3624aebd0418c7e96cd101262f30210ab26',
     'source_url':'https://github.com/anthropics/financial-services/tree/574ed3624aebd0418c7e96cd101262f30210ab26/plugins/vertical-plugins/equity-research/skills',
     'description':'参考 idea-generation 和 catalyst-calendar，证据、事件风险和可证伪逻辑'}
]


def skill_metadata():
    return [{k:v for k,v in skill.items() if k != 'file'} for skill in SKILLS]


@lru_cache(maxsize=1)
def skill_prompt():
    return '\n\n'.join((ROOT / skill['file']).read_text(encoding='utf-8') for skill in SKILLS)
