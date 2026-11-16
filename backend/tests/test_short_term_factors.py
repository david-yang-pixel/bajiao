import datetime as dt
import math

from backend.app.services.short_term.factors import compute_factors


def bars(closes, volume=1_000_000, start='2026-01-01'):
    d0 = dt.date.fromisoformat(start)
    return [{'date': (d0 + dt.timedelta(days=i)).isoformat(), 'open': c, 'high': c * 1.01,
             'low': c * 0.99, 'close': c, 'volume': volume} for i, c in enumerate(closes)]


def test_breakout_with_volume_is_flagged():
    closes = [100.0] * 79 + [105.0]
    data = bars(closes); data[-1]['volume'] = 3_000_000
    f = compute_factors(data, {'SPY': bars([400.0] * 80), 'QQQ': bars([300.0] * 80)}, None)
    assert f['breakout_20d'] is True
    assert round(f['volume_ratio'], 1) == 3.0
    assert round(f['returns']['1d'], 2) == 5.0
    assert f['relative']['SPY']['5d'] > 0


def test_insufficient_history_does_not_raise():
    f = compute_factors(bars([10.0] * 30), {'SPY': bars([1.0] * 30), 'QQQ': bars([1.0] * 30)}, None)
    assert f['insufficient_history'] is True and f['price'] == 10.0
    assert set(f) == {'as_of', 'price', 'insufficient_history'}


def test_atr_and_levels_present():
    closes = [100 + 5 * math.sin(i / 3) for i in range(90)]
    f = compute_factors(bars(closes), {'SPY': bars([1.0] * 90), 'QQQ': bars([1.0] * 90)}, None)
    assert f['atr14'] > 0 and f['levels']['resistance'] and f['levels']['support']
    assert all(r > f['price'] for r in f['levels']['resistance'])
    assert all(s < f['price'] for s in f['levels']['support'])
    assert f['levels']['resistance'] == sorted(f['levels']['resistance'])
    assert f['levels']['support'] == sorted(f['levels']['support'], reverse=True)


def test_sector_etf_included_only_when_present_and_missing_overlap_is_none():
    closes = [100.0] * 80
    bench = {'SPY': bars([400.0] * 80), 'QQQ': bars([300.0] * 80), 'XLK': bars([50.0] * 80),
             'ZZZ': bars([5.0] * 80, start='2020-01-01')}
    f = compute_factors(bars(closes), bench, 'XLK')
    assert set(f['relative']) == {'SPY', 'QQQ', 'XLK'}
    f = compute_factors(bars(closes), bench, 'NOPE')
    assert set(f['relative']) == {'SPY', 'QQQ'}
    f = compute_factors(bars(closes), bench, 'ZZZ')
    assert f['relative']['ZZZ'] is None


def test_rsi_streak_and_ma_on_rising_series():
    closes = [100.0 + i for i in range(80)]
    f = compute_factors(bars(closes), {'SPY': bars(closes), 'QQQ': bars(closes)}, None)
    assert f['rsi14'] > 99 and f['rsi2'] > 99
    assert f['streak'] == 79
    assert f['ma_aligned'] is True
    assert f['ma']['5'] > f['ma']['10'] > f['ma']['20']
    assert f['ma20_slope_pct'] > 0
    assert f['price'] == 179.0 and f['as_of'] == bars(closes)[-1]['date']
