"""Pick validation (stock-USD basis) and token USDT price conversion. Pure, no I/O."""
from .constants import (MAX_STOP_ATR_MULT, MAX_TARGET_ATR_MULT, MIN_REWARD_RISK, MIN_STOP_ATR_MULT,
                        PRICE_TOLERANCE)

REQUIRED = ('symbol', 'decision', 'current_price', 'rationale', 'invalidation', 'risks', 'citations')
NEWS_MARKERS = ('news', 'grade', 'earning', 'calendar', 'filing', 'insider')
VIEWS = {'hold', 'reduce', 'exit'}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_citations(pick, tool_log, purpose, errs):
    by_id = {e.get('call_id'): e for e in tool_log}
    tools = set()
    for c in pick.get('citations') or []:
        entry = by_id.get(c.get('call_id')) if isinstance(c, dict) else None
        if entry is None:
            errs.append(f"引用的工具调用不存在：{c.get('call_id') if isinstance(c, dict) else c}")
            continue
        tools.add(str(entry.get('tool') or ''))
        if c.get('url') and c['url'] not in (entry.get('urls') or []):
            errs.append(f"引用链接不在对应工具调用返回中：{c['url']}")
    if 'get_factors' not in tools:
        errs.append('至少需要引用一次 get_factors 的结果')
    if purpose == 'pick' and not any(m in t for t in tools for m in NEWS_MARKERS):
        errs.append('至少需要引用一次新闻/事件类工具的结果')


def validate_pick(pick, tool_log, *, symbol, reference_price_usd, atr14, token_quote, purpose):
    errs = []
    missing = [k for k in REQUIRED if not pick.get(k)]
    if missing:
        errs.append('缺少必填字段：' + '、'.join(missing))
    if pick.get('symbol') and pick['symbol'] != symbol:
        errs.append(f"标的不一致：应为 {symbol}")
    if not isinstance(pick.get('citations'), list) or not isinstance(pick.get('risks'), list):
        errs.append('risks 与 citations 必须为列表')
        return errs
    _check_citations(pick, tool_log, purpose, errs)

    cur = pick.get('current_price')
    if not _num(cur) or cur <= 0:
        return errs
    if reference_price_usd and abs(cur / reference_price_usd - 1) > PRICE_TOLERANCE:
        errs.append('现价与主服务行情偏差超过 2%（可能误用代币 USDT 价）')

    tgt, stop = pick.get('target_price'), pick.get('stop_price')
    if purpose == 'position_review':
        if pick.get('view') not in VIEWS:
            errs.append('持仓复核的 view 必须为 hold / reduce / exit')
        if _num(stop) and not stop < cur:
            errs.append('止损价必须低于现价')
    elif purpose == 'pick' and pick.get('decision') == 'selected':
        if not (_num(tgt) and _num(stop) and tgt > cur > stop):
            errs.append('价位必须满足 目标价 > 现价 > 止损价')
        else:
            if (tgt - cur) / (cur - stop) < MIN_REWARD_RISK:
                errs.append(f'盈亏比低于 {MIN_REWARD_RISK}')
            if tgt - cur > MAX_TARGET_ATR_MULT * atr14:
                errs.append(f'目标价距离超过 {MAX_TARGET_ATR_MULT} 倍 ATR，一周内不现实')
            if not MIN_STOP_ATR_MULT * atr14 <= cur - stop <= MAX_STOP_ATR_MULT * atr14:
                errs.append(f'止损距离需在 {MIN_STOP_ATR_MULT}–{MAX_STOP_ATR_MULT} 倍 ATR 之间')
        if token_quote.get('tradable') is not True:
            errs.append('对应代币当前不可交易')
    return errs


def to_token_prices(token_mid_usdt, current_usd, target_usd, stop_usd):
    ratio = token_mid_usdt / current_usd
    f = lambda v: None if v is None else round(v * ratio, 4)
    return {'token_price_usdt': round(token_mid_usdt, 4), 'token_target_usdt': f(target_usd),
            'token_stop_usdt': f(stop_usd)}
