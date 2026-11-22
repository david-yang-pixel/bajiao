from datetime import date, timedelta

from app.services.short_term.macro_calendar import FOMC_DATES, events_between
from app.services.short_term.prescreen import prescreen


def _f(**kw):
    base = {
        'insufficient_history': False, 'price': 100.0, 'returns': {'5d': 1.0},
        'ma': {'5': 100.0, '10': 101.0, '20': 99.0}, 'ma_aligned': False,
        'breakout_20d': False, 'volume_ratio': 0.8, 'relative': {'SPY': {'5d': -1.0, '20d': 0.0}},
        'rsi2': 50.0, 'rsi14': 45.0, 'dist_to_20d_high_pct': -8.0, 'atr_pct': 1.0,
    }
    base.update(kw)
    return base


def test_prescreen_ranks_breakout_first_and_filters_short_history():
    f_breakout = _f(breakout_20d=True, volume_ratio=2.0, ma_aligned=True, rsi14=60.0,
                    dist_to_20d_high_pct=0.5, atr_pct=3.0, relative={'SPY': {'5d': 2.0, '20d': 3.0}})
    cands, diag = prescreen({'NVDA': f_breakout, 'AAPL': _f(), 'NEW': {'insufficient_history': True}}, limit=1)
    assert [c['symbol'] for c in cands] == ['NVDA']
    assert cands[0]['score'] == 100 and len(cands[0]['reasons']) == 7
    assert any(d['symbol'] == 'NEW' and d['decision'] == 'filtered' for d in diag)
    assert any(d['symbol'] == 'AAPL' for d in diag) is False


def test_prescreen_filters_overbought_and_downtrend_and_tolerates_missing_relative():
    cands, diag = prescreen({
        'HOT': _f(rsi2=96.0),
        'DOWN': _f(price=95.0, returns={'5d': -2.0}),
        'OK': _f(relative={'SPY': None}),
    })
    assert [c['symbol'] for c in cands] == ['OK']
    assert {d['symbol'] for d in diag} == {'HOT', 'DOWN'}


def test_events_between_returns_fomc_in_week():
    d = date.fromisoformat(FOMC_DATES[0])
    assert events_between(d - timedelta(days=2), d + timedelta(days=2))[0]['event'] == 'FOMC 利率决议'
    assert events_between(d + timedelta(days=1), d + timedelta(days=5)) == []
