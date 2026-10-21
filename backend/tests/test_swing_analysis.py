import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from backend.app.database import init_db
from backend.app.services.swing_analysis import SwingAnalysisService, AnalysisUnavailable
from backend.app.services.swing_screener import score_swing, STOCK_UNIVERSE


def bars(end='2026-10-19', count=270, bullish=True):
    import pandas as pd
    dates = pd.bdate_range(end=end, periods=count)
    result = []
    for i, day in enumerate(dates):
        close = 70 + i * .2 if bullish else 160 - i * .2
        width = 1.2 if i < count - 10 else .45
        result.append({'date': day.date().isoformat(), 'open': close-.1,
                       'high': close+width, 'low': close-width,
                       'close': close, 'volume': 1000000 if i < count-10 else 600000})
    return result


def research():
    return {'financials': [{'date': f'{year}-{month:02d}-30', 'eps': eps, 'revenue': revenue}
                           for year, month, eps, revenue in [(2026,6,2,200), (2026,3,1.9,190),
                           (2025,12,1.8,180), (2025,9,1.7,170), (2025,6,1,140)]],
            'news': [{'id':'news-1', 'title':'Published company update', 'date':'2026-10-19',
                      'url':'https://example.org/news'}],
            'earnings': [], 'sources': [{'id':'financials', 'title':'FMP quarterly financials',
                      'url':'https://financialmodelingprep.com/stable/income-statement?symbol=NVDA',
                      'as_of':'2026-06-30'}, {'id':'news-1','title':'Published company update',
                      'url':'https://example.org/news','as_of':'2026-10-19'}]}


def test_pool_is_explicit_and_not_entire_us_market():
    assert len(STOCK_UNIVERSE) == 8
    assert STOCK_UNIVERSE['NVDABUSDT'] == 'NVDA'
    assert STOCK_UNIVERSE['GOOGBUSDT'] == 'GOOG'
    assert 'SPYUSDT' not in STOCK_UNIVERSE


def test_score_uses_long_history_relative_strength_and_observed_levels():
    scored = score_swing(bars(), bars(bullish=False), research()['financials'])
    assert scored['eligible'] is True
    assert 60 <= scored['score'] <= 100
    assert scored['metrics']['ma200'] > 0
    assert scored['metrics']['relative_strength_20d'] > 0
    low, high = scored['entry_range']
    assert 0 < scored['stop_loss_price'] < low <= high < scored['target_price']
    assert 'probability' not in scored
    assert score_swing(bars(count=30), bars())['eligible'] is False
    assert score_swing(bars(bullish=False), bars())['eligible'] is False


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), -1, 0])
def test_invalid_prices_cannot_become_candidates(invalid):
    history = bars()
    history[-1]['close'] = invalid
    assert score_swing(history, bars())['eligible'] is False


async def service(tmp_path, monkeypatch):
    from backend.app.config import settings
    path = str(tmp_path / 'swing.db')
    monkeypatch.setattr(settings, 'DB_PATH', path)
    await init_db(path)
    market = AsyncMock()
    market.api_key = 'test-fmp'
    market.get_history.return_value = bars()
    # Falling benchmark makes relative strength independently observable.
    async def history(symbol):
        return bars(bullish=False) if symbol == 'SPY' else bars()
    market.get_history.side_effect = history
    market.get_research.return_value = research()
    agent = AsyncMock()
    agent.api_key = 'test-model'
    agent.analyze_swing.return_value = {'selected':True, 'reason':'Trend and earnings evidence support a 2–4 week watch.',
        'invalidation':'A daily close below observed support invalidates this setup.',
        'risks':['Earnings and market drawdown risk'], 'source_ids':['financials','news-1']}
    notifier = AsyncMock()
    notifier.feishu_webhook = 'https://open.feishu.cn/test'
    notifier.wecom_webhook = ''
    notifier.send_channel_text.return_value = True
    result = SwingAnalysisService(market, agent, notifier)
    return result, market, agent, notifier, path


@pytest.mark.asyncio
async def test_run_limits_pool_keeps_history_and_never_creates_positions(tmp_path, monkeypatch):
    runner, market, agent, notifier, path = await service(tmp_path, monkeypatch)
    first = await runner.run(top_n=3)
    assert first['count'] == 3
    assert first['status'] == 'completed'
    assert not notifier.send_channel_text.called
    pick = first['picks'][0]
    assert pick['horizon'] == '2～4 周'
    assert pick['price_basis'] == 'US_EQUITY_USD'
    assert pick['underlying_symbol'] in STOCK_UNIVERSE.values()
    assert pick['sources'] and pick['as_of']
    assert agent.analyze_swing.call_args.args[0]['skills']
    assert set(c.args[0] for c in market.get_history.call_args_list) <= set(STOCK_UNIVERSE.values()) | {'SPY'}
    second = await runner.run(top_n=5)
    assert second['count'] == 5
    async with aiosqlite.connect(path) as db:
        assert (await (await db.execute('SELECT COUNT(*) FROM daily_picks')).fetchone())[0] == 8
        assert (await (await db.execute('SELECT COUNT(*) FROM positions')).fetchone())[0] == 0
    assert len(await runner.store.list_picks()) == 5
    assert len(await runner.store.list_runs()) == 2


@pytest.mark.asyncio
async def test_model_can_reject_all_no_fabricated_fallback(tmp_path, monkeypatch):
    runner, market, agent, notifier, path = await service(tmp_path, monkeypatch)
    agent.analyze_swing.return_value = {'selected':False, 'reason':'Insufficient upside evidence',
         'risks':['Market risk'], 'invalidation':'No entry', 'source_ids':['financials']}
    result = await runner.run(push_notifications=True)
    assert result['count'] == 0
    assert result['status'] == 'no_candidates'
    assert all('目标' not in c.args[1] for c in notifier.send_channel_text.call_args_list)


@pytest.mark.asyncio
async def test_missing_or_failed_model_does_not_replace_previous_picks(tmp_path, monkeypatch):
    runner, market, agent, notifier, path = await service(tmp_path, monkeypatch)
    first = await runner.run()
    agent.analyze_swing.side_effect = AnalysisUnavailable('模型请求未通过')
    failed = await runner.run()
    assert failed['status'] == 'failed'
    assert len(await runner.store.list_picks()) == first['count']
    agent.api_key = ''
    with pytest.raises(AnalysisUnavailable):
        await runner.run()


@pytest.mark.asyncio
async def test_future_earnings_and_unverified_citations_are_not_recommended(tmp_path, monkeypatch):
    runner, market, agent, notifier, path = await service(tmp_path, monkeypatch)
    evidence = research()
    evidence['earnings'] = [{'date': (datetime.now(timezone.utc).date()+timedelta(days=1)).isoformat(), 'title':'Earnings'}]
    market.get_research.return_value = evidence
    result = await runner.run(earnings_blackout_days=3)
    assert result['count'] == 0
    assert not agent.analyze_swing.called
    market.get_research.return_value = research()
    agent.analyze_swing.return_value['source_ids'] = ['made-up-source']
    result = await runner.run()
    assert result['count'] == 0
    assert result['status'] == 'failed'


@pytest.mark.asyncio
async def test_push_retries_only_failed_delivery_and_survives_new_service(tmp_path, monkeypatch):
    runner, market, agent, notifier, path = await service(tmp_path, monkeypatch)
    notifier.wecom_webhook = 'https://qyapi.weixin.qq.com/test'
    async def deliver(channel, text):
        return channel == 'feishu'
    notifier.send_channel_text.side_effect = deliver
    result = await runner.run(push_notifications=True)
    run_id = result['run_id']
    status = (await runner.store.list_runs())[0]
    assert status['push_status'] == 'partial'
    sent_success = sum(c.args[0]=='feishu' for c in notifier.send_channel_text.call_args_list)
    runner2 = SwingAnalysisService(market, agent, notifier)
    notifier.send_channel_text.reset_mock()
    notifier.send_channel_text.side_effect = None
    notifier.send_channel_text.return_value = True
    assert (await runner2.push_run(run_id))['success'] is True
    assert sent_success >= 1
    assert all(c.args[0] == 'wecom' for c in notifier.send_channel_text.call_args_list)
    notifier.send_channel_text.reset_mock()
    await runner2.push_run(run_id)
    notifier.send_channel_text.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_key_is_persistent_and_concurrent_run_is_rejected(tmp_path, monkeypatch):
    runner, market, agent, notifier, path = await service(tmp_path, monkeypatch)
    result = await runner.run(run_key='schedule:2026-10-20')
    again = await SwingAnalysisService(market, agent, notifier).run(run_key='schedule:2026-10-20')
    assert again['run_id'] == result['run_id']
    assert len(await runner.store.list_runs()) == 1


@pytest.mark.asyncio
async def test_reviews_are_based_on_actual_subsequent_sessions_and_not_early(tmp_path, monkeypatch):
    runner, market, agent, notifier, path = await service(tmp_path, monkeypatch)
    await runner.run()
    assert (await runner.review())['count'] == 0
    market.get_history.side_effect = lambda symbol: bars(end='2026-11-16', count=290, bullish=symbol!='SPY')
    result = await runner.review()
    assert result['count'] == 6
    reviews = await runner.store.list_reviews()
    assert {r['horizon_sessions'] for r in reviews} == {10,20}
    assert all(r['return_pct'] is not None and r['excess_return_pct'] is not None for r in reviews)
    assert (await runner.review())['count'] == 0
