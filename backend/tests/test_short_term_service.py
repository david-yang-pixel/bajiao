import asyncio
from datetime import datetime, timedelta, timezone

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
TOOL_LOG = [{'call_id': 'c1', 'tool': 'get_factors', 'urls': [], 'summary': '{"price": 139.5}', 'is_error': False},
            {'call_id': 'c2', 'tool': 'get_news', 'urls': ['http://n/1'], 'summary': '财报超预期 http://n/1',
             'is_error': False}]


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
        self.health_state = {'status': 'ok', 'auth': 'valid'}

    async def health(self):
        return dict(self.health_state)

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
    # summary comes from the tool_log entry (what the tool returned), not from the model's citation text
    assert p['sources'][1] == {'call_id': 'c2', 'tool': 'get_news', 'url': 'http://n/1', 'summary': '财报超预期 http://n/1'}
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
    # every candidate skipped by the quota guard is not a normal "no opportunity" round
    assert svc.agent.calls == [] and r['status'] == 'failed' and '额度保护跳过' in r['message']
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


@pytest.mark.asyncio
async def test_reused_pick_rejected_when_price_already_below_stored_stop(svc_factory):
    svc = svc_factory({})
    run_id, _ = await svc.store.begin_run('seed')
    stale = agent_pick('NVDA')
    stale['stop_price'] = PRICE + 0.5 * ATR  # current price is already below the stored stop
    stale['target_price'] = PRICE + 3 * ATR
    stale['current_price'] = PRICE + 1.0 * ATR
    sid = await svc.store.start_session(run_id, 'NVDA', 'pick')
    await svc.store.finish_session(sid, 'ok', 'selected', PRICE + 0.5 * ATR, ATR,
                                   {'decision': 'selected', 'pick': stale, 'tool_log': TOOL_LOG}, '')
    await svc.store.finish_run(run_id, 'no_candidates', [], [], [], 'seed')
    r = await svc.run(top_n=5)
    assert svc.agent.calls == [] or all(c[0] != 'NVDA' for c in svc.agent.calls)
    assert 'NVDA' not in [p['underlying_symbol'] for p in r['picks']]
    run = await svc.store.get_run(r['run_id'])
    assert any(d['symbol'] == 'NVDA' and d['decision'] == 'rejected_by_validation' for d in run['diagnostics'])


@pytest.mark.asyncio
async def test_cancelled_run_marked_failed_and_next_run_can_start(svc_factory):
    import asyncio
    svc = svc_factory({'NVDA': selected('NVDA'), 'TSLA': selected('TSLA')})
    started = asyncio.Event()

    async def hang(symbol, token_symbol, purpose, context):
        started.set()
        await asyncio.sleep(60)
    svc.agent.analyze = hang
    task = asyncio.create_task(svc.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runs = await svc.store.list_runs()
    assert runs[0]['status'] == 'failed'
    left = await svc.store.rows("SELECT status FROM agent_sessions WHERE status='running'")
    assert left == []
    svc.agent = FakeAgent({'NVDA': selected('NVDA'), 'TSLA': selected('TSLA')})
    assert (await svc.run())['status'] == 'completed'


@pytest.mark.asyncio
async def test_store_failure_for_one_symbol_keeps_other_symbol(svc_factory):
    svc = svc_factory({'NVDA': selected('NVDA'), 'TSLA': selected('TSLA')})
    orig = svc.store.finish_session

    async def flaky(sid, status, decision, *a, **k):
        rows = await svc.store.rows('SELECT symbol FROM agent_sessions WHERE id=?', (sid,))
        if rows[0]['symbol'] == 'NVDA':
            raise RuntimeError('db down')
        return await orig(sid, status, decision, *a, **k)
    svc.store.finish_session = flaky
    r = await svc.run()
    assert r['status'] == 'partial' and [p['underlying_symbol'] for p in r['picks']] == ['TSLA']
    run = await svc.store.get_run(r['run_id'])
    assert any(e['symbol'] == 'NVDA' and e['stage'] == 'agent' for e in run['errors'])


async def _age_run(store, run_id, minutes=10):
    """Pretend the run finished `minutes` ago (past the 5-minute re-claim guard)."""
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    await store.execute('UPDATE analysis_runs SET finished_at=? WHERE id=?', (ts, run_id))


@pytest.mark.asyncio
@pytest.mark.parametrize('health, reason', [({'status': 'unreachable', 'auth': 'unknown'}, 'unreachable'),
                                            ({'status': 'ok', 'auth': 'expired'}, '凭据已过期'),
                                            ({'status': 'ok', 'auth': 'missing'}, '凭据缺失')])
async def test_unhealthy_agent_fails_run_without_starting_sessions(svc_factory, health, reason):
    svc = svc_factory({'NVDA': selected('NVDA'), 'TSLA': selected('TSLA')})
    svc.agent.health_state = health
    r = await svc.run()
    assert r['status'] == 'failed' and r['picks'] == []
    assert r['message'].startswith('AI 分析服务不可用：') and reason in r['message']
    assert svc.agent.calls == [] and await svc.store.rows('SELECT id FROM agent_sessions') == []


@pytest.mark.asyncio
async def test_scheduled_slot_failed_after_sessions_is_not_retried_and_keeps_quota(svc_factory):
    err = AgentResult(status='error', error_code='model_error', error_message='模型调用失败：boom')
    svc = svc_factory({'NVDA': err, 'TSLA': err})
    key = 'schedule:2026-11-16:09:00'
    first = await svc.run(push_notifications=False, run_key=key)
    assert first['status'] == 'failed' and '模型调用失败' in first['message']
    for _ in range(3):  # the 30 s scheduler keeps ticking through the 3 h catch-up window
        await _age_run(svc.store, first['run_id'])
        again = await svc.run(run_key=key)
        assert again['run_id'] == first['run_id'] and again['status'] == 'failed'
    assert len(svc.agent.calls) == 2
    assert await svc.store.sessions_started_since('') == 2  # 22 of 24 left for the next slot
    svc.agent.results = {'NVDA': selected('NVDA'), 'TSLA': selected('TSLA')}
    nxt = await svc.run(run_key='schedule:2026-11-16:21:00')
    assert nxt['status'] == 'completed' and len(svc.agent.calls) == 4


@pytest.mark.asyncio
async def test_scheduled_slot_failed_before_sessions_retries_and_pushes_new_result(svc_factory):
    from backend.tests.test_short_term_tracking import FakeNotifier
    svc = svc_factory({'NVDA': selected('NVDA'), 'TSLA': selected('TSLA')})
    svc.notifier = FakeNotifier()
    svc.agent.health_state = {'status': 'unreachable', 'auth': 'unknown'}
    key = 'schedule:2026-11-16:09:00'
    first = await svc.run(push_notifications=True, run_key=key)
    assert first['status'] == 'failed' and len(svc.notifier.sent) == 1
    assert '本轮分析未完成：AI 分析服务不可用' in svc.notifier.sent[0][1]
    await svc.run(push_notifications=True, run_key=key)  # within 5 minutes: not re-claimed
    assert len(svc.notifier.sent) == 1
    svc.agent.health_state = {'status': 'ok', 'auth': 'valid'}
    await _age_run(svc.store, first['run_id'])
    retry = await svc.run(push_notifications=True, run_key=key)
    assert retry['run_id'] == first['run_id'] and retry['status'] == 'completed' and retry['count'] == 2
    assert len(svc.notifier.sent) == 2 and '本轮新增 2 只' in svc.notifier.sent[1][1]
    assert (await svc.store.get_run(first['run_id']))['push_status'] == 'success'


@pytest.mark.asyncio
async def test_position_review_shares_agent_concurrency_limit_with_runs(svc_factory, monkeypatch):
    monkeypatch.setattr(svc_mod, 'AGENT_CONCURRENCY', 2)
    svc = svc_factory({'NVDA': selected('NVDA'), 'TSLA': selected('TSLA')})
    release, full = asyncio.Event(), asyncio.Event()
    state = {'inflight': 0, 'peak': 0}
    orig = svc.agent.analyze

    async def analyze(symbol, token_symbol, purpose, context):
        state['inflight'] += 1
        state['peak'] = max(state['peak'], state['inflight'])
        if state['inflight'] >= 2:
            full.set()
        try:
            await release.wait()
            return await orig(symbol, token_symbol, purpose, context)
        finally:
            state['inflight'] -= 1
    svc.agent.analyze = analyze
    run_task = asyncio.create_task(svc.run())
    await asyncio.wait_for(full.wait(), 2)
    review_task = asyncio.create_task(svc.analyze_position('NVDAUSDT'))
    await asyncio.sleep(0.05)
    assert state['peak'] == 2  # the review waits for a free agent slot instead of hitting the agent's 429
    release.set()
    await run_task
    await asyncio.gather(review_task, return_exceptions=True)
    assert state['peak'] == 2 and len(svc.agent.calls) == 3


@pytest.mark.asyncio
async def test_unchanged_failed_scheduled_run_is_not_repushed(svc_factory):
    from backend.tests.test_short_term_tracking import FakeNotifier
    svc = svc_factory({'NVDA': selected('NVDA'), 'TSLA': selected('TSLA')})
    svc.notifier = FakeNotifier()
    svc.agent.health_state = {'status': 'ok', 'auth': 'expired'}
    key = 'schedule:2026-11-16:09:00'
    first = await svc.run(push_notifications=True, run_key=key)
    for _ in range(5):
        await _age_run(svc.store, first['run_id'])
        again = await svc.run(push_notifications=True, run_key=key)
        assert again['run_id'] == first['run_id'] and again['status'] == 'failed'
    assert len(svc.notifier.sent) == 1
    assert (await svc.store.get_run(first['run_id']))['push_status'] == 'success'
    svc.agent.health_state = {'status': 'ok', 'auth': 'valid'}
    await _age_run(svc.store, first['run_id'])
    retry = await svc.run(push_notifications=True, run_key=key)
    assert retry['status'] == 'completed' and len(svc.notifier.sent) == 2 and '本轮新增 2 只' in svc.notifier.sent[1][1]


@pytest.mark.asyncio
async def test_changed_failure_reason_on_retry_pushes_again(svc_factory):
    from backend.tests.test_short_term_tracking import FakeNotifier
    svc = svc_factory({'NVDA': selected('NVDA'), 'TSLA': selected('TSLA')})
    svc.notifier = FakeNotifier()
    svc.agent.health_state = {'status': 'unreachable', 'auth': 'unknown'}
    key = 'schedule:2026-11-16:09:00'
    first = await svc.run(push_notifications=True, run_key=key)
    svc.agent.health_state = {'status': 'ok', 'auth': 'expired'}
    await _age_run(svc.store, first['run_id'])
    await svc.run(push_notifications=True, run_key=key)
    assert len(svc.notifier.sent) == 2 and svc.notifier.sent[0][1] != svc.notifier.sent[1][1]
