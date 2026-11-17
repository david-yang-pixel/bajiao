"""Short-term (about one week) technical factors from daily bars."""
import pandas as pd

from .constants import MIN_HISTORY_BARS

Bar = dict
_COLS = ('open', 'high', 'low', 'close', 'volume')
SWING_SIDE = 3
LEVEL_LOOKBACK = 60
LEVEL_COUNT = 3


def _frame(rows):
    df = pd.DataFrame(rows)
    df = df.drop_duplicates('date').sort_values('date').set_index('date')
    for key in _COLS:
        df[key] = pd.to_numeric(df[key], errors='coerce').astype(float)
    return df


def _wilder(series, n):
    return series.ewm(alpha=1 / n, adjust=False).mean()


def _rsi(close, n):
    delta = close.diff()
    gain, loss = _wilder(delta.clip(lower=0), n).iloc[-1], _wilder((-delta).clip(lower=0), n).iloc[-1]
    if loss == 0:
        return 50.0 if gain == 0 else 100.0
    return float(100 - 100 / (1 + gain / loss))


def _pct_return(close, n):
    return float((close.iloc[-1] / close.iloc[-1 - n] - 1) * 100)


def _streak(close):
    diffs = close.diff().dropna().tolist()[::-1]
    if not diffs or diffs[0] == 0:
        return 0
    sign, count = (1 if diffs[0] > 0 else -1), 0
    for d in diffs:
        if d * sign > 0:
            count += 1
        else:
            break
    return sign * count


def _levels(df, price):
    high, low = df['high'].iloc[-LEVEL_LOOKBACK:], df['low'].iloc[-LEVEL_LOOKBACK:]
    res, sup = set(), set()
    for i in range(SWING_SIDE, len(df.iloc[-LEVEL_LOOKBACK:]) - SWING_SIDE):
        hw, lw = high.iloc[i - SWING_SIDE:i + SWING_SIDE + 1], low.iloc[i - SWING_SIDE:i + SWING_SIDE + 1]
        if high.iloc[i] == hw.max():
            res.add(round(float(high.iloc[i]), 4))
        if low.iloc[i] == lw.min():
            sup.add(round(float(low.iloc[i]), 4))
    return {
        'resistance': sorted(r for r in res if r > price)[:LEVEL_COUNT],
        'support': sorted((s for s in sup if s < price), reverse=True)[:LEVEL_COUNT],
    }


def _relative(close, bench_rows):
    if not bench_rows:
        return None
    try:
        b = _frame(bench_rows)['close']
    except (KeyError, ValueError):
        return None
    common = close.index.intersection(b.index)
    out = {}
    for label, n in (('5d', 5), ('20d', 20)):
        if len(common) <= n:
            return None
        s, m = close.loc[common], b.loc[common]
        out[label] = float(((s.iloc[-1] / s.iloc[-1 - n]) - (m.iloc[-1] / m.iloc[-1 - n])) * 100)
    return out


def compute_factors(bars, benchmarks, sector_etf):
    df = _frame(bars)
    as_of, price = str(df.index[-1]), float(df['close'].iloc[-1])
    if len(df) < MIN_HISTORY_BARS:
        return {'as_of': as_of, 'price': price, 'insufficient_history': True}
    o, h, l, c, v = (df[k] for k in _COLS)
    prev_close = c.shift()
    true_range = pd.concat([h - l, (h - prev_close).abs(), (l - prev_close).abs()], axis=1).max(axis=1)
    atr = float(_wilder(true_range.dropna(), 14).iloc[-1])
    high20 = float(h.iloc[-21:-1].max())
    vol_base = float(v.iloc[-21:-1].mean())
    rng = float(h.iloc[-1] - l.iloc[-1])
    ma = {str(n): float(c.iloc[-n:].mean()) for n in (5, 10, 20)}
    ma20_prev = float(c.iloc[-25:-5].mean())
    bb_width = float(4 * c.iloc[-20:].std(ddof=0) / ma['20'] * 100)
    rel_keys = ['SPY', 'QQQ'] + ([sector_etf] if sector_etf and sector_etf in benchmarks
                                 and sector_etf not in ('SPY', 'QQQ') else [])
    return {
        'as_of': as_of,
        'insufficient_history': False,
        'price': price,
        'returns': {f'{n}d': _pct_return(c, n) for n in (1, 3, 5, 10)},
        'dist_to_20d_high_pct': float((price / high20 - 1) * 100),
        'breakout_20d': bool(price > high20),
        'gap_pct': float((o.iloc[-1] / prev_close.iloc[-1] - 1) * 100),
        'volume_ratio': float(v.iloc[-1] / vol_base) if vol_base > 0 else None,
        'close_location': float((c.iloc[-1] - l.iloc[-1]) / rng) if rng > 0 else 0.5,
        'streak': _streak(c),
        'ma': ma,
        'ma_aligned': bool(ma['5'] > ma['10'] > ma['20']),
        'ma20_slope_pct': float((ma['20'] / ma20_prev - 1) * 100),
        'ma20_deviation_pct': float((price / ma['20'] - 1) * 100),
        'rsi2': _rsi(c, 2),
        'rsi14': _rsi(c, 14),
        'bb_width_pct': bb_width,
        'atr14': atr,
        'atr_pct': float(atr / price * 100),
        'range_5d_pct': float((h.iloc[-5:].max() - l.iloc[-5:].min()) / price * 100),
        'relative': {k: _relative(c, benchmarks.get(k)) for k in rel_keys},
        'levels': _levels(df, price),
    }
