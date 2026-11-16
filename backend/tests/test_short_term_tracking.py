import pytest
import pytest_asyncio

from backend.app.database import init_db
from backend.app.services.analysis_store import AnalysisStore
from backend.app.services.short_term.messages import build_run_message, split_message
from backend.app.services.short_term.service import ShortTermService
from backend.app.services.short_term.universe import MarketDataUnavailable

AS_OF = '2026-11-02'  # Monday
BASE, TARGET, STOP = 100.0, 106.0, 97.0
DATES = ['2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06', '2026-11-09']


def bar(date, low=99.0, high=101.0, close=100.0):
    return {'date': date, 'open': close, 'high': high, 'low': low, 'close': close, 'volume': 1000.0}


def report(sym='NVDA'):
    return {'symbol': f'{sym}USDT', 'underlying_symbol': sym, 'strategy': 'short_term_1w', 'as_of': AS_OF,
            'expires_after': '2026-11-09', 'current_price_usd': BASE, 'target_price_usd': TARGET,
            'stop_price_usd': STOP, 'target_price': TARGET, 'target_pct': 6.0, 'stop_pct': -3.0, 'reward_risk': 2.0,
            'token_price_usdt': 100.5, 'token_target_usdt': 106.5, 'token_stop_usdt': 97.5,
            'reason': '放量突破', 'invalidation': '跌破止损', 'risks': ['大盘回撤'],
            'sources': [{'call_id': 'c1', 'tool': 'get_news', 'url': 'http://n/1', 'summary': 'a'},
                        {'call_id': 'c2', 'tool': 'get_news', 'url': 'http://n/2', 'summary': 'b'},
                        {'call_id': 'c3', 'tool': 'get_news', 'url': 'http://n/3', 'summary': 'c'}]}


class FakeAlpaca:
    configured = True
    last_errors = {}

    def __init__(self, bars, latest=None):
        self.bars, self.latest, self.calls = bars, latest, []

    async def get_daily_bars(self, symbols, days=150):
        self.calls.append(list(symbols))
        return {s: self.bars.get(s, []) for s in symbols}

    async def get_latest_price(self, symbol):
        if self.latest is None:
            raise MarketDataUnavailable('x')
        return self.latest


class FakeBinance:
    api_key = 'k'

    def __init__(self, status='TRADING'):
        self.status = status

    async def get_spot_symbols(self):
        return {'NVDAUSDT': self.status, 'TSLAUSDT': 'TRADING'}


class FakeNotifier:
    def __init__(self, feishu='http://f', wecom=''):
        self.feishu_webhook, self.wecom_webhook, self.sent = feishu, wecom, []

    async def send_channel_text(self, channel, text):
        self.sent.append((channel, text))
        return True


@pytest_asyncio.fixture
async def factory(tmp_path):
    path = str(tmp_path / 't.db')
    await init_db(path)

    def make(bars=None, latest=None, status='TRADING', notifier=None):
        return ShortTermService(FakeBinance(status), FakeAlpaca(bars or {}, latest), None, notifier or FakeNotifier(),
                                AnalysisStore(path))
    return make


async def add_pick(svc, sym='NVDA'):
    run_id, _ = await svc.store.begin_run(None)
    ids = await svc.store.insert_short_term_picks(run_id, [report(sym)])
    await svc.store.finish_run(run_id, 'completed', [], [], [], 'ok')
    return run_id, ids[0]


async def status_of(svc, pid):
    return (await svc.store.rows('SELECT track_status FROM daily_picks WHERE id=?', (pid,)))[0]['track_status']


@pytest.mark.asyncio
async def test_same_bar_target_and_stop_is_stopped(factory):
    svc = factory({'NVDA': [bar(DATES[0], low=96.0, high=107.0)]})
    _, pid = await add_pick(svc)
    events = await svc.track_active()
    assert [(e['event'], e['price_usd']) for e in events] == [('stopped', STOP)]
    assert await status_of(svc, pid) == 'stopped'
    assert svc.alpaca.calls == [['NVDA']]


@pytest.mark.asyncio
async def test_target_hit_by_bar_and_earlier_stop_wins(factory):
    svc = factory({'NVDA': [bar(DATES[0], high=107.0), bar(DATES[1], low=90.0)]})
    _, pid = await add_pick(svc)
    events = await svc.track_active()
    assert [(e['event'], e['price_usd']) for e in events] == [('target_hit', TARGET)]
    assert await status_of(svc, pid) == 'target_hit'


@pytest.mark.asyncio
async def test_bars_on_or_before_as_of_ignored(factory):
    svc = factory({'NVDA': [bar(AS_OF, low=90.0, high=120.0)]})
    _, pid = await add_pick(svc)
    assert await svc.track_active() == []
    assert await status_of(svc, pid) == 'active'


@pytest.mark.asyncio
async def test_latest_price_hits(factory):
    svc = factory({'NVDA': [bar(DATES[0])]}, latest=107.5)
    await add_pick(svc)
    events = await svc.track_active()
    assert [(e['event'], e['price_usd']) for e in events] == [('target_hit', 107.5)]
    svc2 = factory({'NVDA': [bar(DATES[0])]}, latest=96.5)
    await add_pick(svc2)
    assert [(e['event'], e['price_usd']) for e in await svc2.track_active()] == [('stopped', 96.5)]


@pytest.mark.asyncio
async def test_expired_after_five_sessions(factory):
    svc = factory({'NVDA': [bar(d, close=101.0 + i * 0.1) for i, d in enumerate(DATES)]}, latest=100.0)
    _, pid = await add_pick(svc)
    events = await svc.track_active()
    assert [e['event'] for e in events] == ['expired'] and events[0]['price_usd'] == pytest.approx(101.4)
    assert await status_of(svc, pid) == 'expired'


@pytest.mark.asyncio
async def test_halted_recorded_once_and_pick_stays_active(factory):
    svc = factory({'NVDA': [bar(DATES[0])]}, status='BREAK')
    _, pid = await add_pick(svc)
    events = await svc.track_active()
    assert [e['event'] for e in events] == ['halted']
    assert await status_of(svc, pid) == 'active'
    assert await svc.track_active() == []


@pytest.mark.asyncio
async def test_halted_does_not_stop_other_checks(factory):
    svc = factory({'NVDA': [bar(DATES[0], low=96.0)]}, status='BREAK')
    await add_pick(svc)
    assert {e['event'] for e in await svc.track_active()} == {'halted', 'stopped'}


@pytest.mark.asyncio
async def test_repeat_tracking_no_duplicate_events(factory):
    svc = factory({'NVDA': [bar(DATES[0], low=96.0)]})
    await add_pick(svc)
    assert len(await svc.track_active()) == 1
    assert await svc.track_active() == []
    assert len(await svc.store.rows('SELECT * FROM pick_events')) == 1


@pytest.mark.asyncio
async def test_data_failure_does_not_raise(factory):
    svc = factory()

    async def boom(symbols, days=150):
        raise MarketDataUnavailable('down')
    svc.alpaca.get_daily_bars = boom
    await add_pick(svc)
    assert await svc.track_active() == []


@pytest.mark.asyncio
async def test_review_writes_outcome_and_returns(factory):
    bars = [bar(d, close=100.0 + i) for i, d in enumerate(DATES)]
    bars[1] = bar(DATES[1], high=107.0, close=101.0)
    spy = [bar(AS_OF, close=400.0)] + [bar(d, close=400.0 + 4 * (i + 1)) for i, d in enumerate(DATES)]
    svc = factory({'NVDA': bars, 'SPY': spy})
    _, pid = await add_pick(svc)
    res = await svc.review()
    assert res['count'] == 1 and res['errors'] == []
    row = (await svc.store.rows('SELECT * FROM analysis_reviews'))[0]
    assert row['pick_id'] == pid and row['horizon_sessions'] == 5 and row['as_of'] == DATES[4]
    assert row['outcome'] == 'target'
    assert row['return_pct'] == pytest.approx(4.0)
    assert row['benchmark_return_pct'] == pytest.approx(5.0)
    assert row['excess_return_pct'] == pytest.approx(-1.0)
    assert (await svc.review())['count'] == 0


@pytest.mark.asyncio
async def test_review_waits_for_five_sessions_and_open_outcome(factory):
    svc = factory({'NVDA': [bar(d) for d in DATES[:4]], 'SPY': [bar(AS_OF)]})
    await add_pick(svc)
    assert (await svc.review())['count'] == 0
    svc.alpaca.bars = {'NVDA': [bar(d) for d in DATES], 'SPY': [bar(AS_OF)] + [bar(d) for d in DATES]}
    assert (await svc.review())['count'] == 1
    assert (await svc.store.rows('SELECT outcome FROM analysis_reviews'))[0]['outcome'] == 'open'


def test_message_no_change_first_line():
    msgs = build_run_message({'id': 1, 'message': ''}, [], [], 3)
    assert msgs[0].splitlines()[0] == '【短线精选 · 1周】本轮无新增，3 只跟踪中'
    assert msgs[-1].endswith('研究价格以正股 USD 为准，USDT 为按比例换算的参考；不保证上涨，不执行交易。')


def test_message_new_pick_and_events():
    events = [{'event': 'target_hit', 'underlying_symbol': 'AAPL', 'symbol': 'AAPLUSDT', 'price_usd': 110.0},
              {'event': 'stopped', 'underlying_symbol': 'TSLA', 'symbol': 'TSLAUSDT', 'price_usd': 90.0},
              {'event': 'expired', 'underlying_symbol': 'AMD', 'symbol': 'AMDUSDT', 'price_usd': 99.0},
              {'event': 'halted', 'underlying_symbol': 'MU', 'symbol': 'MUUSDT', 'price_usd': None}]
    text = '\n'.join(build_run_message({'id': 1}, [report()], events, 2))
    assert 'NVDA / NVDAUSDT  短线·1周·有效至 11-09' in text
    assert '正股 USD 现价 100.00  目标 106.00 (+6.0%)  止损 97.00 (-3.0%)  盈亏比 2.0' in text
    assert '代币 NVDAUSDT 现价 100.50 → 目标约 106.50 / 止损约 97.50（按比例换算）' in text
    assert 'http://n/1' in text and 'http://n/2' in text and 'http://n/3' not in text
    for line in ('✅ AAPL 已到目标 110.00', '⛔ TSLA 触发止损 90.00', '⌛ AMD 到期未达目标', '⚠️ MUUSDT 现货交易对暂停交易'):
        assert line in text
    assert not text.startswith('【短线精选 · 1周】本轮无新增')


def test_split_message_limits_bytes():
    chunks = split_message('测' * 1000, 1800)
    assert len(chunks) == 2 and all(len(c.encode()) <= 1800 for c in chunks) and ''.join(chunks) == '测' * 1000


@pytest.mark.asyncio
async def test_push_run_marks_events_notified(factory):
    notifier = FakeNotifier()
    svc = factory({'NVDA': [bar(DATES[0], low=96.0)]}, notifier=notifier)
    run_id, _ = await add_pick(svc)
    await svc.track_active()
    res = await svc.push_run(run_id)
    assert res['success'] is True and res['status'] == 'success'
    assert notifier.sent and 'NVDA' in notifier.sent[0][1] and '触发止损' in notifier.sent[0][1]
    assert [r['notified'] for r in await svc.store.rows('SELECT notified FROM pick_events')] == [1]
    await svc.retry_pushes()
    assert len(notifier.sent) == 1


@pytest.mark.asyncio
async def test_push_run_without_channels_keeps_events_unnotified(factory):
    svc = factory({'NVDA': [bar(DATES[0], low=96.0)]}, notifier=FakeNotifier(feishu='', wecom=''))
    run_id, _ = await add_pick(svc)
    await svc.track_active()
    res = await svc.push_run(run_id)
    assert res == {'success': False, 'status': 'failed', 'message': '未配置推送渠道'}
    assert [r['notified'] for r in await svc.store.rows('SELECT notified FROM pick_events')] == [0]
