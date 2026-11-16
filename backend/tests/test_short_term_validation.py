from backend.app.services.short_term.validation import to_token_prices, validate_pick

LOG = [{'call_id': 'c1', 'tool': 'get_factors', 'urls': []},
       {'call_id': 'c2', 'tool': 'alpaca_get_news', 'urls': ['https://ex.com/a']}]
GOOD = {'symbol': 'NVDA', 'decision': 'selected', 'current_price': 100.0, 'target_price': 106.0,
        'stop_price': 97.0, 'rationale': '放量突破 20 日高点并有发布会催化', 'invalidation': '收盘跌破 97',
        'risks': ['大盘回调'], 'citations': [{'call_id': 'c1'}, {'call_id': 'c2', 'url': 'https://ex.com/a'}]}
Q = {'tradable': True}


def run(pick, **kw):
    args = dict(symbol='NVDA', reference_price_usd=100.5, atr14=3.0, token_quote=Q, purpose='pick')
    args.update(kw)
    return validate_pick(pick, LOG, **args)


def test_good_pick_passes():
    assert run(GOOD) == []


def test_usdt_price_confusion_rejected():
    errs = run({**GOOD, 'current_price': 103.5, 'target_price': 109.5, 'stop_price': 100.5}, reference_price_usd=100.0)
    assert any('2%' in e for e in errs)


def test_url_from_other_call_rejected():
    bad = {**GOOD, 'citations': [{'call_id': 'c1', 'url': 'https://ex.com/a'}, {'call_id': 'c2'}]}
    assert run(bad)


def test_unknown_call_id_rejected():
    assert run({**GOOD, 'citations': [{'call_id': 'zz'}]})


def test_missing_field_and_symbol_mismatch():
    assert run({k: v for k, v in GOOD.items() if k != 'rationale'})
    assert run({**GOOD, 'symbol': 'AAPL'})
    assert run({**GOOD, 'risks': []})


def test_requires_factors_and_news_citation():
    only_news = {**GOOD, 'citations': [{'call_id': 'c2'}]}
    assert run(only_news)
    only_factors = {**GOOD, 'citations': [{'call_id': 'c1'}]}
    assert run(only_factors)


def test_low_reward_risk_and_oversized_target_rejected():
    low_rr = run({**GOOD, 'target_price': 103.6, 'stop_price': 97.0})  # rr=1.2
    assert any('盈亏比' in e for e in low_rr)
    big = run({**GOOD, 'target_price': 108.0, 'stop_price': 97.0}, atr14=3.0)  # 8 > 7.5
    assert any('ATR' in e for e in big)


def test_stop_distance_bounds():
    assert run({**GOOD, 'stop_price': 99.0, 'target_price': 103.0})  # stop 1 < 1.5
    assert run({**GOOD, 'stop_price': 93.0, 'target_price': 106.0}, atr14=3.0)  # stop 7 > 6


def test_level_order():
    assert run({**GOOD, 'target_price': 99.0})
    assert run({**GOOD, 'stop_price': 101.0})
    assert run({**GOOD, 'target_price': None})


def test_untradable_token_rejected():
    assert any('可交易' in e for e in run(GOOD, token_quote={'tradable': False}))


def test_rejected_decision_skips_levels():
    rej = {k: v for k, v in GOOD.items() if k not in ('target_price', 'stop_price')}
    assert run({**rej, 'decision': 'rejected'}, token_quote={'tradable': False}) == []


def test_position_review():
    pr = {**GOOD, 'view': 'hold'}
    pr.pop('decision')
    pr['decision'] = 'review'
    assert run(pr, purpose='position_review') == []
    assert run({**pr, 'view': 'buy'}, purpose='position_review')
    assert run({**pr, 'stop_price': 101.0}, purpose='position_review')


def test_token_conversion():
    assert to_token_prices(101.0, 100.0, 106.0, 97.0) == {
        'token_price_usdt': 101.0, 'token_target_usdt': 107.06, 'token_stop_usdt': 97.97}
    assert to_token_prices(101.0, 100.0, None, None)['token_target_usdt'] is None


def test_non_numeric_or_non_positive_current_price_rejected():
    for bad in ('100', -5, 0):
        assert any('现价必须为正数' in e for e in run({**GOOD, 'current_price': bad})) or \
            any('缺少必填' in e for e in run({**GOOD, 'current_price': bad}))
    assert any('现价必须为正数' in e for e in run({**GOOD, 'current_price': '100'}))
    assert any('现价必须为正数' in e for e in run({**GOOD, 'current_price': -5}))
