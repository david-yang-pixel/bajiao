"""Synthetic ledger fixtures, never real balances or paid provider requests."""
from unittest.mock import AsyncMock
import copy

import aiosqlite
import httpx
import pytest

from backend.app import main
from backend.tests.test_equity_sync import prepare


def ledger():
    return dict(symbol='EQ_MRVL',quantity=.5,token_quantity=0,
        conversions=[{'issuerRequestId':'redeem-1','underlyingAsset':'MRVL','tokenizedAsset':'MRVLB',
            'convertType':'REDEEM','status':'S','tokenizedAssetAmount':'1.5','underlyingAssetAmount':'.75',
            'createdAt':30,'updatedAt':31}],
        spot=[{'id':1,'symbol':'MRVLBUSDT','time':10,'qty':'2','quoteQty':'160','isBuyer':True,
               'commission':'0','commissionAsset':'BNB'},
              {'id':2,'symbol':'MRVLBUSDT','time':20,'qty':'.5','quoteQty':'60','isBuyer':False,
               'commission':'0','commissionAsset':'BNB'}],
        flows=[],
        trades=[{'executionId':'sale-1','symbol':'MRVL','executionAt':40,'qty':'.25','price':'200','side':'SELL'}])


@pytest.mark.parametrize('mode',['base','conversion_sell','base_fee','flat_before_conversion'])
def test_rebuilds_original_payment_cost_through_documented_conversion_ratio(mode):
    from backend.app.services.equity_cost_basis import reconstruct_equity_cost
    data=ledger()
    if mode=='conversion_sell':
        data['spot'].pop()
        data['flows']=[{'orderId':'f1','createTime':20,'orderStatus':'SUCCESS','fromAsset':'MRVLB','toAsset':'USDC','fromAmount':'.5','toAmount':'60'}]
    if mode=='base_fee':
        data['spot'][0].update(qty='2.1',commission='.1',commissionAsset='MRVLB')
    if mode=='flat_before_conversion':
        data['trades'][:0]=[
            {'executionId':'buy-old','symbol':'MRVL','executionAt':1,'qty':'1','price':'50','side':'BUY'},
            {'executionId':'sell-old','symbol':'MRVL','executionAt':2,'qty':'1','price':'60','side':'SELL'}]
    result=reconstruct_equity_cost(**data)
    assert result['currency']=='USDT'
    assert result['price']==pytest.approx(160)


def test_convert_acquisition_preserves_original_currency():
    from backend.app.services.equity_cost_basis import reconstruct_equity_cost
    data=ledger();data['spot']=[]
    data['flows']=[{'orderId':'f1','createTime':10,'orderStatus':'SUCCESS','fromAsset':'USDC',
                    'toAsset':'MRVLB','fromAmount':'160','toAmount':'2'},
                   {'orderId':'f2','createTime':20,'orderStatus':'SUCCESS','fromAsset':'MRVLB',
                    'toAsset':'USDT','fromAmount':'.5','toAmount':'60'}]
    assert reconstruct_equity_cost(**data)=={'price':160.0,'currency':'USDC'}


@pytest.mark.parametrize('case',['wrong_quantity','missing_buy','duplicate_trade','wrong_spot_symbol',
    'ambiguous_order','mixed_currency','invalid_ratio','nan','unsupported_payment'])
def test_incomplete_or_ambiguous_ledgers_are_rejected(case):
    from backend.app.services.equity_cost_basis import reconstruct_equity_cost, EquityCostUnavailable
    data=ledger()
    if case=='wrong_quantity':data['quantity']=.6
    if case=='missing_buy':data['spot'].pop(0)
    if case=='duplicate_trade':data['spot'].append(copy.deepcopy(data['spot'][0]))
    if case=='wrong_spot_symbol':data['spot'][0]['symbol']='TSLABUSDT'
    if case=='ambiguous_order':data['spot'][1]['time']=10
    if case=='mixed_currency':
        data['trades'].append({'executionId':'new-buy','symbol':'MRVL','executionAt':50,'qty':'.1','price':'150','side':'BUY'})
        data['quantity']=.6
    if case=='invalid_ratio':data['conversions'][0]['underlyingAssetAmount']='0'
    if case=='nan':data['spot'][0]['quoteQty']='NaN'
    if case=='unsupported_payment':
        data['spot']=[];data['flows']=[{'orderId':'f1','createTime':10,'orderStatus':'SUCCESS',
            'fromAsset':'BTC','toAsset':'MRVLB','fromAmount':'.01','toAmount':'1.5'}]
    with pytest.raises(EquityCostUnavailable):reconstruct_equity_cost(**data)


@pytest.mark.asyncio
async def test_client_loads_complete_30_day_convert_windows_and_exact_record_mapping(monkeypatch):
    from backend.app.services.binance_client import BinanceClient
    data=ledger();calls=[]
    async def read(path,params):
        calls.append((path,params))
        if path.endswith('tokenized/history'):return {'rows':data['conversions'],'hasMore':False}
        if path.endswith('myTrades'):return data['spot']
        if path.endswith('tradeFlow'):return {'list':[],'moreData':False}
        if path.endswith('trade/history'):return {'total':len(data['trades']),'rows':data['trades']}
        raise AssertionError(path)
    client=BinanceClient(api_key='key',api_secret='secret')
    monkeypatch.setattr(client,'_call_signed_read',read,raising=False)
    # Keep synthetic timestamps within the bounded complete lookback.
    monkeypatch.setattr('backend.app.services.binance_client.time.time',lambda:100)
    result=await client.get_equity_conversion_cost('EQ_MRVL',.5,asset_balances={'MRVLB':0})
    assert result=={'price':160.0,'currency':'USDT'}
    windows=[p for route,p in calls if route.endswith('tradeFlow')]
    assert windows and all(p['endTime']-p['startTime']<=30*86400000 for p in windows)
    assert any(p.get('symbol')=='MRVLBUSDT' and p.get('fromId')==0 for _,p in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('truncated',['tokenized','spot','convert','native'])
async def test_client_rejects_truncated_sources(monkeypatch,truncated):
    from backend.app.services.binance_client import BinanceClient
    data=ledger()
    async def read(path,params):
        if path.endswith('tokenized/history'):return {'rows':data['conversions'],'hasMore':truncated=='tokenized'}
        if path.endswith('myTrades'):return data['spot']*500 if truncated=='spot' else data['spot']
        if path.endswith('tradeFlow'):return {'list':[],'moreData':truncated=='convert'}
        if path.endswith('trade/history'):return {'total':200 if truncated=='native' else 1,'rows':data['trades']}
        raise AssertionError(path)
    client=BinanceClient(api_key='key',api_secret='secret')
    monkeypatch.setattr(client,'_call_signed_read',read,raising=False)
    monkeypatch.setattr('backend.app.services.binance_client.time.time',lambda:100)
    assert await client.get_equity_conversion_cost('EQ_MRVL',.5,asset_balances={'MRVLB':0}) is None


@pytest.mark.asyncio
async def test_synced_original_cost_is_saved_but_never_subtracted_from_usd_quote(tmp_path,monkeypatch):
    path=await prepare(tmp_path,monkeypatch,[{'asset':'EQ_MRVL','total':2}])
    monkeypatch.setattr(main.binance_client,'get_equity_conversion_cost',AsyncMock(return_value={'price':80,'currency':'USDT'}),raising=False)
    result=await main.perform_binance_sync()
    assert result['synced'][0]['buy_price']==80 and result['synced'][0]['cost_currency']=='USDT'
    positions=await main.list_positions();pos=positions[0]
    assert pos['cost_status']=='known' and pos['cost_currency']=='USDT'
    assert pos['current_price']==100 and pos['quote_currency']=='USD'
    assert pos['pnl_pct'] is None and pos['pnl_usd'] is None and pos['pnl_status']=='currency_mismatch'
    sender=AsyncMock(return_value=True)
    monkeypatch.setattr(main.notification_service,'send_position_alert',sender)
    raw=(await main.position_monitor.get_active_positions())[0]
    assert await main.position_monitor.check_single_position(raw,force_periodic=True)==[]
    sender.assert_not_awaited()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
        monkeypatch.setattr(main.notification_service,'feishu_webhook','https://example.test')
        assert (await client.post('/api/positions/1/test-alert')).status_code==409
        assert (await client.put('/api/positions/1',json={'buy_price':90,'cost_currency':'USD'})).status_code==200
    await main.perform_binance_sync()
    pos=(await main.list_positions())[0]
    assert pos['cost_currency']=='USD' and pos['buy_price']==90 and pos['pnl_pct']==11.11
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute('SELECT cost_source FROM positions WHERE id=1')).fetchone()==('manual',)


@pytest.mark.asyncio
async def test_currency_updates_require_real_price_and_spot_keeps_usdt(tmp_path,monkeypatch):
    await prepare(tmp_path,monkeypatch,[{'asset':'EQ_MRVL','total':2}])
    monkeypatch.setattr(main.binance_client,'get_equity_conversion_cost',AsyncMock(return_value={'price':80,'currency':'USDT'}))
    await main.perform_binance_sync()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
        for currency in ['EUR','BTC','USD; DROP TABLE positions']:
            assert (await client.put('/api/positions/1',json={'cost_currency':currency,'buy_price':90})).status_code==422
        assert (await client.put('/api/positions/1',json={'cost_currency':'USD'})).status_code==422
        assert (await client.post('/api/positions',json={'symbol':'TSLABUSDT','buy_price':90,'quantity':1,'cost_currency':'USD'})).status_code==422
        created=await client.post('/api/positions',json={'symbol':'TSLABUSDT','buy_price':90,'quantity':1,'cost_currency':'USDT'})
        assert created.status_code==200
        assert (await client.put('/api/positions/'+str(created.json()['id']),json={'cost_currency':'USDC','buy_price':90})).status_code==422
    row=(await main.list_positions())[0]
    assert row['buy_price']==80 and row['cost_currency']=='USDT'


@pytest.mark.asyncio
async def test_older_than_complete_convert_lookback_is_not_guessed(monkeypatch):
    from backend.app.services.binance_client import BinanceClient
    data=ledger()
    async def read(path,params):
        if path.endswith('tokenized/history'):return {'rows':data['conversions'],'hasMore':False}
        if path.endswith('myTrades'):return data['spot']
        if path.endswith('trade/history'):return {'total':1,'rows':data['trades']}
        raise AssertionError('Do not load partial older Convert history')
    client=BinanceClient(api_key='key',api_secret='secret')
    monkeypatch.setattr(client,'_call_signed_read',read)
    monkeypatch.setattr('backend.app.services.binance_client.time.time',lambda:200*86400)
    assert await client.get_equity_conversion_cost('EQ_MRVL',.5,asset_balances={'MRVLB':0}) is None


def test_unexplained_remaining_token_inventory_rejects_hidden_opening_balance():
    from backend.app.services.equity_cost_basis import reconstruct_equity_cost,EquityCostUnavailable
    data=ledger();data['token_quantity']=1
    with pytest.raises(EquityCostUnavailable):reconstruct_equity_cost(**data)


def test_payment_currency_purchase_fee_is_part_of_acquisition_cost():
    from backend.app.services.equity_cost_basis import reconstruct_equity_cost
    data=ledger();data['spot'][0].update(commission='1.6',commissionAsset='USDT')
    assert reconstruct_equity_cost(**data)['price']==pytest.approx(161.6)


@pytest.mark.asyncio
async def test_saving_same_currency_does_not_freeze_automatic_cost(tmp_path,monkeypatch):
    path=await prepare(tmp_path,monkeypatch,[{'asset':'EQ_MRVL','total':2}])
    basis=AsyncMock(return_value={'price':80,'currency':'USDT'})
    monkeypatch.setattr(main.binance_client,'get_equity_conversion_cost',basis)
    await main.perform_binance_sync()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
        assert (await client.put('/api/positions/1',json={'cost_currency':'USDT','check_interval_hours':2})).status_code==200
    basis.return_value={'price':90,'currency':'USDT'}
    await main.perform_binance_sync()
    row=(await main.list_positions())[0]
    assert row['buy_price']==90 and row['cost_source']=='token_conversion_history'
