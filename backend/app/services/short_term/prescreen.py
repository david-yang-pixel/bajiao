"""Cheap rule-based prescreen: score daily-bar factors, keep the top candidates."""
from .constants import PRESCREEN_LIMIT


def _filter_reason(f):
    if f.get('insufficient_history'):
        return '历史数据不足'
    if f['rsi2'] > 95:
        return 'RSI2 > 95，短线过热'
    if f['price'] < f['ma']['20'] and f['returns']['5d'] < 0:
        return '价格低于 MA20 且 5 日收益为负'
    return None


def _score(f):
    score, reasons = 0, []
    rel = (f.get('relative') or {}).get('SPY')
    vol = f.get('volume_ratio')
    checks = (
        (25, bool(f['breakout_20d']), '突破 20 日高'),
        (20, vol is not None and vol >= 1.5, '量比 ≥ 1.5'),
        (15, bool(f['ma_aligned']), 'MA5>MA10>MA20 多头排列'),
        (15, rel is not None and rel['5d'] > 0, '5 日跑赢 SPY'),
        (10, 50 <= f['rsi14'] <= 75, 'RSI14 处于 50–75'),
        (10, f['dist_to_20d_high_pct'] >= -3, '距 20 日高 ≤ 3%'),
        (5, 1.5 <= f['atr_pct'] <= 6, 'ATR% 处于 1.5–6'),
    )
    for pts, hit, text in checks:
        if hit:
            score += pts
            reasons.append(text)
    return score, reasons


def prescreen(factors_by_symbol, limit=PRESCREEN_LIMIT):
    candidates, diagnostics = [], []
    for symbol, f in factors_by_symbol.items():
        reason = _filter_reason(f)
        if reason:
            diagnostics.append({'symbol': symbol, 'decision': 'filtered', 'reason': reason})
            continue
        score, reasons = _score(f)
        candidates.append({'symbol': symbol, 'score': score, 'reasons': reasons})
    candidates.sort(key=lambda c: -c['score'])
    return candidates[:limit], diagnostics
