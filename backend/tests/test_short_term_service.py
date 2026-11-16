from datetime import datetime, timezone

import pytest
import pytest_asyncio

from backend.app.database import init_db
from backend.app.services.analysis_errors import AnalysisUnavailable
from backend.app.services.analysis_store import AnalysisStore
from backend.app.services.short_term import service as svc_mod
from backend.app.services.short_term.agent_client import AgentResult
from backend.app.services.short_term.factors import compute_factors
from backend.app.services.short_term.service import ShortTermService
from backend.app.services.short_term.universe import TokenPair


def make_bars(n=80):
    bars = []
    for i in range(n):
        close = 100 + 0.5 * i - (1.5 if i % 2 else 0)
        bars.append({'date': f'2026-{1 + i // 28:02d}-{1 + i % 28:02d}', 'open': close - 0.2, 'high': close + 1.0,
                     'low': close - 1.0, 'close': close, 'volume': 1000.0 + i})
    return bars


BARS = make_bars()
FACTORS = compute_factors(BARS, {'SPY': BARS, 'QQQ': BARS}, None)
PRICE, ATR = FACTORS['price'], FACTORS['atr14']
TOOL_LOG = [{'call_id': 'c1', 'tool': 'get_factors', 'urls': []},
            {'call_id': 'c2', 'tool': 'get_news', 'urls': ['http://n/1']}]


def agent_pick(symbol, target_mult=2.0):
    return {'symbol': symbol, 'decision': 'selected', 'current_price': PRICE,
            'target_price': PRICE + target_mult * ATR, 'stop_price': PRICE - ATR,
            'rationale': '放量突破，趋势延续', 'invalidation': '跌破止损位', 'risks': ['大盘回撤'],
            'citations': [{'call_id': 'c1', 'summary': '因子'}, {'call_id': 'c2', 'url': 'http://n/1', 'summary': '新闻'}]}


def selected(symbol, target_mult=2.0):
    return AgentResult(status='ok', decision='selected', pick=agent_pick(symbol, target_mult), tool_log=TOOL_LOG)


class FakeAlpaca:
    configured = True
    last_errors = {}

    def __init__(self):
        self.news = 0

    async def get_daily_bars(self, symbols, days=150):
        return {s: BARS for s in symbols}

    async def get_latest_price(self, symbol):
        return PRICE

    async def count_news_since(self, symbol, since):
        return self.news


class FakeAgent:
    def __init__(self, results):
        self.results, self.calls = results, []

    async def analyze(self, symbol, token_symbol, purpose, context):
        self.calls.append((symbol, token_symbol, purpose, context))
        return self.results[symbol]


class FakeBinance:
    api_key = 'k'


class FakeUniverse:
    async def get(self, force=False):
        return [TokenPair('NVDAUSDT', 'NVDA', 'NVDA', 1.0), TokenPair('TSLAUSDT', 'TSLA', 'TSLA', 1.0)]


@pytest_asyncio.fixture
async def svc_factory(tmp_path, monkeypatch):
    async def quote(client, pair, price):
        return {'symbol': pair.symbol, 'mid_usdt': PRICE, 'tradable': True, 'status': 'TRADING'}
    monkeypatch.setattr(svc_mod, 'get_token_quote', quote)
    path = str(tmp_path / 't.db')
    await init_db(path)

    def factory(results=None, alpaca=None):
        s = ShortTermService(FakeBinance(), alpaca or FakeAlpaca(), FakeAgent(results or {}), None, AnalysisStore(path))
        s.universe = FakeUniverse()
        return s
    return factory


@pytest.mark.asyncio
async def test_run_selects_validated_pick_and_ranks_by_reward_risk(svc_factory):
    svc = svc_factory({'NVDA': selected('NVDA', 2.0), 'TSLA': selected('TSLA', 2.4)})
    r = await svc.run(top_n=1)
    assert r['status'] == 'completed' and r['count'] == 1
    p = r['picks'][0]
    assert p['underlying_symbol'] == 'TSLA' and p['symbol'] == 'TSLAUSDT'
    assert p['strategy'] == 'short_term_1w' and p['analysis_mode'] == 'pi_agent'
    assert p['target_price'] == p['target_price_usd'] and p['reward_risk'] == pytest.approx(2.4)
    assert p['token_target_usdt'] > p['token_price_usdt'] > p['token_stop_usdt']
    assert p['sources'][1] == {'call_id': 'c2', 'tool': 'get_news', 'url': 'http://n/1', 'summary': '新闻'}
    assert p['expires_after'] > p['as_of'] and p['track_status'] == 'active'
    run = await svc.store.get_run(r['run_id'])
    assert run['count'] == 1
    assert {d['symbol']: d['decision'] for d in run['diagnostics']} == {'TSLA': 'selected', 'NVDA': 'rejected'}


@pytest.mark.asyncio
async def test_agent_error_on_one_symbol_keeps_others_partial(svc_factory):
    svc = svc_factory({'NVDA': AgentResult(status='error', error_code='auth_expired', error_message='x'),
                       'TSLA': selected('TSLA')})
    r = await svc.run(top_n=5)
    assert r['status'] == 'partial' and [p['underlying_symbol'] for p in r['picks']] == ['TSLA']
    run = await svc.store.get_run(r['run_id'])
    assert any(e['symbol'] == 'NVDA' and e['stage'] == 'agent' and '凭据' in e['message'] for e in run['errors'])


@pytest.mark.asyncio
async def test_reuse_skips_agent_when_no_news_and_small_move(svc_factory):
    svc = svc_factory({})
    run_id, _ = await svc.store.begin_run('seed')
    for sym, decision, pick in (('NVDA', 'selected', agent_pick('NVDA')), ('TSLA', 'rejected', None)):
        sid = await svc.store.start_session(run_id, sym, 'pick')
        await svc.store.finish_session(sid, 'ok', decision, PRICE, ATR,
                                       {'decision': decision, 'pick': pick, 'tool_log': TOOL_LOG}, '')
    await svc.store.finish_run(run_id, 'no_candidates', [], [], [], 'seed')
    r = await svc.run(top_n=5, run_key='k')
    assert svc.agent.calls == []
    assert [p['underlying_symbol'] for p in r['picks']] == ['NVDA']
    run = await svc.store.get_run(r['run_id'])
    assert {d['decision'] for d in run['diagnostics']} >= {'reused'}


@pytest.mark.asyncio
async def test_daily_quota_skips_agent(svc_factory):
    svc = svc_factory({'NVDA': selected('NVDA'), 'TSLA': selected('TSLA')})
    run_id, _ = await svc.store.begin_run('seed')
    for _ in range(24):
        await svc.store.start_session(run_id, 'AAPL', 'pick')
    await svc.store.finish_run(run_id, 'no_candidates', [], [], [], 'seed')
    r = await svc.run(top_n=5)
    assert svc.agent.calls == [] and r['status'] == 'no_candidates'
    run = await svc.store.get_run(r['run_id'])
    assert [d['decision'] for d in run['diagnostics']].count('quota_skip') == 2


@pytest.mark.asyncio
async def test_same_run_key_not_rerun(svc_factory):
    svc = svc_factory({'NVDA': selected('NVDA'), 'TSLA': selected('TSLA')})
    first = await svc.run(run_key='schedule:2026-11-16:09:00')
    n = len(svc.agent.calls)
    second = await svc.run(run_key='schedule:2026-11-16:09:00')
    assert n == 2 and len(svc.agent.calls) == n
    assert second['run_id'] == first['run_id'] and second['count'] == first['count']


@pytest.mark.asyncio
async def test_symbol_with_active_pick_not_reanalyzed(svc_factory):
    svc = svc_factory({'NVDA': selected('NVDA'), 'TSLA': selected('TSLA')})
    await svc.run()
    svc.agent.calls.clear()
    r = await svc.run()
    assert svc.agent.calls == [] and r['status'] == 'no_candidates'
    run = await svc.store.get_run(r['run_id'])
    assert {d['decision'] for d in run['diagnostics']} == {'active_pick'}


@pytest.mark.asyncio
async def test_missing_alpaca_config_raises_unavailable(svc_factory):
    svc = svc_factory({})
    svc.alpaca.configured = False
    with pytest.raises(AnalysisUnavailable):
        await svc.run()
    with pytest.raises(ValueError):
        await svc_factory({}).run(top_n=6)
