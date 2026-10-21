"""Native EQ_ holdings must never be valued as tokenized BUSDT instruments."""
import json
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest

from backend.app import main
from backend.app.database import init_db
from backend.app.services.binance_client import BinanceClient


async def prepare(tmp_path, monkeypatch, balances=None):
    path = str(tmp_path / 'equities.db')
    monkeypatch.setattr(main.settings, 'DB_PATH', path)
    await init_db(path)
    monkeypatch.setattr(main.binance_client, 'api_key', 'test-key')
    monkeypatch.setattr(main.binance_client, 'api_secret', 'test-secret')
    monkeypatch.setattr(main.binance_client, 'get_account_balances', AsyncMock(return_value=balances or [
        {'asset': 'EQ_MRVL', 'total': 2.0, 'wallet': '资金账户'},
        {'asset': 'EQ_TSLA', 'total': 1.0, 'wallet': '资金账户'},
    ]))
    monkeypatch.setattr(main.binance_client, 'get_ticker_price', AsyncMock(return_value=100.0))
    monkeypatch.setattr(main.binance_client, 'get_average_buy_price', AsyncMock(return_value=None))
    monkeypatch.setattr(main.binance_client, 'get_equity_average_buy_price', AsyncMock(return_value=None), raising=False)
    return path


@pytest.mark.asyncio
async def test_sync_keeps_native_assets_unknown_cost_and_no_default_targets(tmp_path, monkeypatch):
    path = await prepare(tmp_path, monkeypatch)
    result = await main.perform_binance_sync()
    assert result['count'] == 2
    assert {p['symbol'] for p in result['synced']} == {'EQ_MRVL', 'EQ_TSLA'}
    assert all(p['buy_price'] is None and p['quote_currency'] == 'USD' for p in result['synced'])
    assert len(result['warnings']) == 2
    assert [c.args[0] for c in main.binance_client.get_ticker_price.await_args_list] == ['EQ_MRVL', 'EQ_TSLA']
    main.binance_client.get_average_buy_price.assert_not_awaited()
    async with aiosqlite.connect(path) as db:
        rows = await (await db.execute('SELECT buy_price,take_profit_price,stop_loss_price,ladder_alerts FROM positions')).fetchall()
    assert rows == [(0, None, None, '[]'), (0, None, None, '[]')]
    listed = await main.list_positions()
    assert all(p['current_price'] == 100 and p['buy_price'] is None and p['pnl_pct'] is None for p in listed)
    assert all(p['cost_status'] == 'unknown' and p['asset_type'] == 'equity' for p in listed)


@pytest.mark.asyncio
async def test_reimport_equity_preserves_manual_cost_and_all_alert_preferences(tmp_path, monkeypatch):
    path = await prepare(tmp_path, monkeypatch)
    async with aiosqlite.connect(path) as db:
        await db.execute("""INSERT INTO positions(symbol,buy_price,quantity,take_profit_price,stop_loss_price,
            check_interval_hours,ladder_alerts,include_ai_analysis,preferences_customized,is_active)
            VALUES('EQ_TSLA',80,5,120,70,0,'[7.0]',0,1,0)""")
        await db.commit()
    await main.perform_binance_sync()
    await main.perform_binance_sync()
    async with aiosqlite.connect(path) as db:
        rows = await (await db.execute("SELECT buy_price,quantity,take_profit_price,stop_loss_price,check_interval_hours,ladder_alerts,include_ai_analysis FROM positions WHERE symbol='EQ_TSLA' AND is_active=1")).fetchall()
    assert rows == [(80, 1, 120, 70, 0, '[7.0]', 0)]


@pytest.mark.asyncio
async def test_unavailable_equity_quote_is_visible_and_does_not_deactivate(tmp_path, monkeypatch):
    path = await prepare(tmp_path, monkeypatch)
    await main.perform_binance_sync()
    monkeypatch.setattr(main.binance_client, 'get_ticker_price', AsyncMock(side_effect=ValueError('no quote')))
    result = await main.perform_binance_sync()
    assert not result['deactivated']
    assert result['count'] == 2
    assert all(p['current_price'] is None for p in result['synced'])
    listed = await main.list_positions()
    assert len(listed) == 2 and all(p['price_status'] == 'unavailable' for p in listed)


@pytest.mark.asyncio
async def test_dust_and_unmatched_assets_have_explicit_reasons(tmp_path, monkeypatch):
    await prepare(tmp_path, monkeypatch, [
        {'asset': 'METAB', 'total': .0001}, {'asset': 'UNKNOWN', 'total': 3},
    ])
    async def price(symbol):
        if symbol == 'METABUSDT':
            return 100.0
        raise ValueError('unavailable')
    monkeypatch.setattr(main.binance_client, 'get_ticker_price', price)
    result = await main.perform_binance_sync()
    assert result['count'] == 0
    assert {p['asset'] for p in result['skipped']} == {'METAB', 'UNKNOWN'}
    assert all(p['reason'] for p in result['skipped'])


@pytest.mark.asyncio
async def test_equity_cost_edit_and_no_test_alert_with_unknown_cost(tmp_path, monkeypatch):
    await prepare(tmp_path, monkeypatch)
    await main.perform_binance_sync()
    monkeypatch.setattr(main.notification_service, 'feishu_webhook', 'https://example.test')
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(main.notification_service, 'send_position_alert', sender)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
        result = await client.post('/api/positions/1/test-alert')
        assert result.status_code == 409
        sender.assert_not_awaited()
        assert (await client.put('/api/positions/1', json={'buy_price': 90})).status_code == 200
        rows = (await client.get('/api/positions')).json()
        assert rows[0]['pnl_pct'] == 11.11 and rows[0]['cost_status'] == 'known'


@pytest.mark.asyncio
async def test_equity_quote_uses_header_only_and_underlying_not_spot(monkeypatch):
    client = BinanceClient(api_key='key', api_secret='secret', mcp_url='https://unused.test')
    requests = []
    async def get(self, url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(200, json={'symbol': 'TSLA', 'bidPrice': '99', 'askPrice': '101'}, request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx.AsyncClient, 'get', get)
    assert await client.get_ticker_price('EQ_TSLA') == 100.0
    assert requests == [('https://api.binance.com/sapi/v1/equity/market/quote', {'params': {'symbol': 'TSLA'}, 'headers': {'X-MBX-APIKEY': 'key'}})]


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'symbol': 'AAPL', 'bidPrice': '99', 'askPrice': '101'},
    {'symbol': 'TSLA', 'bidPrice': 'NaN', 'askPrice': '101'},
    {'symbol': 'TSLA', 'bidPrice': '102', 'askPrice': '101'},
    {'symbol': 'TSLA', 'bidPrice': '0', 'askPrice': '101'}, {},
])
async def test_equity_quote_rejects_wrong_symbol_and_invalid_prices(monkeypatch, payload):
    async def get(self, url, **kwargs):
        return httpx.Response(200, json=payload, request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx.AsyncClient, 'get', get)
    with pytest.raises(ValueError):
        await BinanceClient(api_key='key').get_ticker_price('EQ_TSLA')


@pytest.mark.asyncio
async def test_equity_cost_reconstructs_inventory_and_requires_matching_balance(monkeypatch):
    rows = [
        {'executionId': 'a', 'executionAt': 1, 'symbol': 'TSLA', 'qty': '2', 'price': '90', 'side': 'BUY'},
        {'executionId': 'b', 'executionAt': 2, 'symbol': 'TSLA', 'qty': '1', 'price': '120', 'side': 'SELL'},
        {'executionId': 'c', 'executionAt': 3, 'symbol': 'TSLA', 'qty': '1', 'price': '110', 'side': 'BUY'},
    ]
    calls = []
    async def get(self, url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={'total': 3, 'page': 1, 'rows': list(reversed(rows))}, request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx.AsyncClient, 'get', get)
    client = BinanceClient(api_key='key', api_secret='secret')
    assert await client.get_equity_average_buy_price('EQ_TSLA', 2) == 100.0
    assert await client.get_equity_average_buy_price('EQ_TSLA', 3) is None
    url, kwargs = calls[0]
    assert url.endswith('/sapi/v1/equity/trade/history')
    assert kwargs['params']['symbol'] == 'TSLA' and kwargs['params']['startTime'] == 0
    assert 'signature' in kwargs['params']
    assert kwargs['params']['current'] == 1 and kwargs['params']['size'] == 100


@pytest.mark.asyncio
async def test_account_failure_is_atomic(tmp_path, monkeypatch):
    path = await prepare(tmp_path, monkeypatch)
    await main.perform_binance_sync()
    async with aiosqlite.connect(path) as db:
        before = await (await db.execute('SELECT * FROM positions')).fetchall()
    monkeypatch.setattr(main.binance_client, 'get_account_balances', AsyncMock(side_effect=RuntimeError('safe error')))
    with pytest.raises(RuntimeError):
        await main.perform_binance_sync()
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute('SELECT * FROM positions')).fetchall() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['truncated','duplicate','oversold','wrong_symbol','nan','empty'])
async def test_incomplete_or_invalid_equity_cost_is_unknown(monkeypatch, case):
    trade = {'executionId':'a','executionAt':1,'symbol':'TSLA','qty':'2','price':'90','side':'BUY'}
    rows, total = [trade], 1
    if case == 'truncated': total = 200
    if case == 'duplicate': rows, total = [trade,dict(trade)], 2
    if case == 'oversold': trade['side'] = 'SELL'
    if case == 'wrong_symbol': trade['symbol'] = 'AAPL'
    if case == 'nan': trade['price'] = 'NaN'
    if case == 'empty': rows, total = [], 0
    async def get(self, url, **kwargs):
        return httpx.Response(200,json={'total':total,'rows':rows},request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    assert await BinanceClient(api_key='key',api_secret='secret').get_equity_average_buy_price('EQ_TSLA',2) is None


@pytest.mark.asyncio
async def test_stock_sync_uses_verified_cost_and_keeps_research_pool_unchanged(tmp_path, monkeypatch):
    await prepare(tmp_path, monkeypatch)
    from backend.app.services.swing_screener import STOCK_UNIVERSE
    before = dict(STOCK_UNIVERSE)
    monkeypatch.setattr(main.binance_client,'get_equity_average_buy_price',AsyncMock(return_value=80))
    await main.perform_binance_sync()
    rows = await main.list_positions()
    assert all(p['pnl_pct'] == 25 and p['cost_source'] == 'trade_history_ex_fees' for p in rows)
    assert dict(STOCK_UNIVERSE) == before and len(before) == 8


@pytest.mark.asyncio
async def test_unknown_cost_stock_never_sends_monitor_alerts(tmp_path, monkeypatch):
    await prepare(tmp_path, monkeypatch)
    await main.perform_binance_sync()
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(main.notification_service,'send_position_alert',sender)
    rows = await main.position_monitor.get_active_positions()
    for row in rows:
        assert await main.position_monitor.check_single_position(row,force_periodic=True) == []
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_sync_does_not_duplicate_holdings(tmp_path, monkeypatch):
    import asyncio
    path = await prepare(tmp_path, monkeypatch)
    # Reset per-test loop binding; production runs in one event loop.
    monkeypatch.setattr(main,'_account_sync_lock',asyncio.Lock())
    await asyncio.gather(main.perform_binance_sync(),main.perform_binance_sync())
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute('SELECT COUNT(*) FROM positions WHERE is_active=1')).fetchone() == (2,)


@pytest.mark.asyncio
async def test_stock_alert_labels_currency_and_midpoint(monkeypatch):
    from backend.app.services.notification import NotificationService
    service = NotificationService(feishu_webhook='https://example.test',wecom_webhook='')
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(service,'_post',sender)
    assert await service.send_position_alert('EQ_TSLA',80,100,25,'periodic')
    body = json.dumps(sender.await_args.args[1],ensure_ascii=False)
    assert 'USD' in body and '非可成交价' in body and 'USDT' not in body


@pytest.mark.asyncio
async def test_equity_cost_uses_documented_usd_fill_price_not_payment_asset(monkeypatch):
    async def get(self,url,**kwargs):
        return httpx.Response(200,json={'total':1,'rows':[
            {'executionId':'a','executionAt':1,'symbol':'TSLA','quote':'USDC','side':'BUY',
             'qty':'2','price':'90','total':'181'}]},request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    assert await BinanceClient(api_key='key',api_secret='secret').get_equity_average_buy_price('EQ_TSLA',2) == 90


@pytest.mark.asyncio
async def test_manual_cost_saved_during_history_fetch_wins(tmp_path, monkeypatch):
    path = await prepare(tmp_path,monkeypatch,[{'asset':'EQ_TSLA','total':1}])
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT INTO positions(symbol,buy_price,quantity,cost_source) VALUES('EQ_TSLA',0,1,'unknown')")
        await db.commit()
    async def fetch(*args):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
            assert (await client.put('/api/positions/1',json={'buy_price':90})).status_code == 200
        return 80
    monkeypatch.setattr(main.binance_client,'get_equity_average_buy_price',fetch)
    result = await main.perform_binance_sync()
    assert result['synced'][0]['buy_price'] == 90
    assert result['synced'][0]['cost_source'] == 'manual'


@pytest.mark.asyncio
async def test_same_timestamp_mixed_side_cost_is_unknown(monkeypatch):
    rows = [
        {'executionId':'a','executionAt':1,'symbol':'TSLA','qty':'2','price':'90','side':'BUY'},
        {'executionId':'b','executionAt':2,'symbol':'TSLA','qty':'1','price':'120','side':'SELL'},
        {'executionId':'c','executionAt':2,'symbol':'TSLA','qty':'1','price':'110','side':'BUY'},
    ]
    async def get(self,url,**kwargs):
        return httpx.Response(200,json={'total':3,'rows':rows},request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    assert await BinanceClient(api_key='key',api_secret='secret').get_equity_average_buy_price('EQ_TSLA',2) is None


@pytest.mark.asyncio
async def test_reactivation_resets_alert_watermark_but_retains_history(tmp_path, monkeypatch):
    path = await prepare(tmp_path,monkeypatch,[{'asset':'EQ_TSLA','total':1}])
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT INTO positions(symbol,buy_price,quantity,is_active) VALUES('EQ_TSLA',90,1,0)")
        await db.execute("INSERT INTO alert_logs(position_id,symbol,message,alert_type) VALUES(1,'EQ_TSLA','old','take_profit')")
        await db.commit()
    main.position_monitor.mark_milestone_achieved('1:take_profit')
    await main.perform_binance_sync()
    assert not await main.position_monitor.is_milestone_achieved(1,'1:take_profit','take_profit')
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute('SELECT COUNT(*) FROM alert_logs')).fetchone() == (1,)
