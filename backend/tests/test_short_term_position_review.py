import pytest

from backend.app.services.analysis_errors import AnalysisUnavailable
from backend.app.services.short_term.agent_client import AgentResult
from backend.app.services.short_term.universe import TokenPair
from tests.test_short_term_service import ATR, PRICE, TOOL_LOG, FakeAgent, selected, svc_factory  # noqa: F401


class Universe:
    async def get(self, force=False):
        return [TokenPair('NVDABUSDT', 'NVDA', 'NVDA', 1.0)]


def review(view='reduce', **over):
    pick = {'symbol': 'NVDA', 'decision': view, 'view': view, 'current_price': PRICE, 'stop_price': PRICE - ATR,
            'target_price': PRICE + ATR, 'rationale': '动能转弱，建议降低仓位', 'invalidation': '重新站上 20 日线',
            'risks': ['财报波动'],
            'citations': [{'call_id': 'c1', 'summary': '因子'}, {'call_id': 'c2', 'url': 'http://n/1', 'summary': '新闻'}],
            **over}
    return AgentResult(status='ok', decision=view, pick=pick, tool_log=TOOL_LOG)


def make(svc_factory, result):
    svc = svc_factory({'NVDA': result})
    svc.universe = Universe()
    return svc


@pytest.mark.asyncio
async def test_review_returns_report_with_token_prices(svc_factory):
    svc = make(svc_factory, review('reduce'))
    r = await svc.analyze_position('NVDABUSDT', buy_price_usdt=90.0, quantity=2.0)
    assert r['view'] == 'reduce' and r['reason'] == '动能转弱，建议降低仓位'
    assert r['price_basis'] == '正股 USD（USDT 为按比例换算参考）'
    assert r['target_price'] == r['token_target_usdt'] and r['stop_loss_price'] == r['token_stop_usdt']
    assert r['token_target_usdt'] > r['token_stop_usdt'] and r['current_price_usd'] == PRICE
    assert r['sources'][1]['url'] == 'http://n/1' and r['sources'][1]['summary'] == '财报超预期 http://n/1'
    sym, tok, purpose, ctx = svc.agent.calls[0]
    assert (sym, tok, purpose) == ('NVDA', 'NVDABUSDT', 'position_review')
    assert ctx['position'] == {'buy_price_usdt': 90.0, 'quantity': 2.0} and ctx['reference_price_usd'] == PRICE


@pytest.mark.asyncio
async def test_symbol_outside_universe(svc_factory):
    svc = make(svc_factory, review())
    with pytest.raises(AnalysisUnavailable, match='不在币安美股代币现货标的池内'):
        await svc.analyze_position('AAPLBUSDT')


@pytest.mark.asyncio
async def test_cache_hit_skips_agent_and_position_none(svc_factory):
    svc = make(svc_factory, review('hold'))
    first = await svc.analyze_position('NVDABUSDT')
    assert svc.agent.calls[0][3]['position'] is None
    assert await svc.analyze_position('NVDABUSDT') == first
    assert len(svc.agent.calls) == 1
    svc._position_cache.clear()
    await svc.analyze_position('NVDABUSDT')
    assert len(svc.agent.calls) == 2


@pytest.mark.asyncio
async def test_quota_exceeded(svc_factory):
    svc = make(svc_factory, review())
    svc.session_limit = 0
    with pytest.raises(AnalysisUnavailable, match='今日 AI 分析额度已用完'):
        await svc.analyze_position('NVDABUSDT')
    assert svc.agent.calls == []


@pytest.mark.asyncio
async def test_agent_error_and_validation_failure(svc_factory):
    svc = make(svc_factory, AgentResult(status='error', error_code='auth_expired', error_message='x'))
    with pytest.raises(AnalysisUnavailable, match='凭据已过期'):
        await svc.analyze_position('NVDABUSDT')
    svc = make(svc_factory, review(view='buy'))
    with pytest.raises(AnalysisUnavailable, match='view'):
        await svc.analyze_position('NVDABUSDT')
    assert svc._position_cache == {}


@pytest.mark.asyncio
async def test_insufficient_history_raises_unavailable_without_session(svc_factory):
    from tests.test_short_term_service import make_bars

    class ShortAlpaca:
        configured, last_errors = True, {}

        async def get_daily_bars(self, symbols, days=150):
            return {s: make_bars(20) for s in symbols}

        async def get_latest_price(self, symbol):
            return PRICE
    svc = svc_factory({'NVDA': review()}, alpaca=ShortAlpaca())
    svc.universe = Universe()
    with pytest.raises(AnalysisUnavailable, match='该标的历史数据不足，暂无法分析'):
        await svc.analyze_position('NVDABUSDT')
    assert svc.agent.calls == []
    assert await svc.store.rows('SELECT id FROM agent_sessions') == []
