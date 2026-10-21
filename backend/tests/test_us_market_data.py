from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.app.services.us_market_data import USMarketData, MarketDataError


@pytest.mark.asyncio
async def test_missing_key_fails_without_network():
    with pytest.raises(MarketDataError, match='FMP'):
        await USMarketData(api_key='').get_history('NVDA')


@pytest.mark.asyncio
async def test_history_sorts_deduplicates_and_excludes_unfinished_us_session(monkeypatch):
    rows = [
        {'date':'2026-10-21','open':101,'high':104,'low':100,'close':103,'volume':100},
        {'date':'2026-10-20','open':100,'high':103,'low':99,'close':102,'volume':100},
        {'date':'2026-10-20','open':100,'high':103,'low':99,'close':102,'volume':100},
    ]
    data = USMarketData(api_key='secret', now=lambda:datetime(2026,10,21,16,tzinfo=timezone.utc))
    monkeypatch.setattr(data, '_get', AsyncMock(return_value=rows))
    history = await data.get_history('NVDA')
    assert [r['date'] for r in history] == ['2026-10-20']
    assert 'apikey' not in str(data.sources('NVDA'))


@pytest.mark.asyncio
async def test_provider_errors_do_not_leak_api_key(monkeypatch):
    data = USMarketData(api_key='secret-never-log')
    monkeypatch.setattr(httpx.AsyncClient, 'get', AsyncMock(return_value=httpx.Response(403,
        request=httpx.Request('GET','https://financialmodelingprep.com/?apikey=secret-never-log'),
        json={'Error Message':'secret-never-log'})))
    with pytest.raises(MarketDataError) as error:
        await data.get_history('NVDA')
    assert 'secret-never-log' not in str(error.value)
    assert '403' in str(error.value)


@pytest.mark.asyncio
async def test_malformed_or_stale_history_is_not_silently_used(monkeypatch):
    data = USMarketData(api_key='test', now=lambda:datetime(2026,10,21,22,tzinfo=timezone.utc))
    monkeypatch.setattr(data, '_get', AsyncMock(return_value=[{
        'date':'2020-01-01','open':100,'high':101,'low':99,'close':100,'volume':100}]))
    with pytest.raises(MarketDataError, match='过期'):
        await data.get_history('NVDA')
    data._get.return_value = [{'date':'2026-10-20','close':float('nan')}]
    with pytest.raises(MarketDataError):
        await data.get_history('NVDA')


@pytest.mark.asyncio
async def test_research_distinguishes_unavailable_from_no_events(monkeypatch):
    data = USMarketData(api_key='test', now=lambda:datetime(2026,10,21,22,tzinfo=timezone.utc))
    async def result(path, **params):
        if path == 'income-statement':
            return [{'date':'2026-06-30','eps':2,'revenue':100}]
        return []
    monkeypatch.setattr(data, '_get', result)
    snapshot = await data.get_research('NVDA')
    assert snapshot['news'] == [] and snapshot['earnings'] == []
    async def denied(path, **params):
        raise MarketDataError('数据权限不足')
    monkeypatch.setattr(data, '_get', denied)
    with pytest.raises(MarketDataError):
        await data.get_research('NVDA')
