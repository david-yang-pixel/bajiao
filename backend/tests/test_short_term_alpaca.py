from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from backend.app.services.short_term import alpaca_data
from backend.app.services.short_term.alpaca_data import AlpacaData
from backend.app.services.short_term.universe import MarketDataUnavailable

NY = ZoneInfo('America/New_York')


def ny(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=NY)


def bar(day, o=10.0, h=11.0, l=9.0, c=10.5, v=1000):
    return {'t': f'{day}T05:00:00Z', 'o': o, 'h': h, 'l': l, 'c': c, 'v': v}


class FakeGet:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, client, url, params=None, headers=None, **kw):
        self.calls.append({'url': url, 'params': dict(params or {}), 'headers': dict(headers or {})})
        item = self.responses.pop(0)
        if isinstance(item, int):
            return httpx.Response(item, json={'message': 'x'}, request=httpx.Request('GET', url))
        return httpx.Response(200, json=item, request=httpx.Request('GET', url))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(alpaca_data.asyncio, 'sleep', fake_sleep)
    return sleeps


def patch_get(monkeypatch, responses):
    fake = FakeGet(responses)
    monkeypatch.setattr(httpx.AsyncClient, 'get', lambda client, url, **kw: fake.get(client, url, **kw))
    return fake


@pytest.mark.asyncio
async def test_bars_paginate_and_drop_incomplete_today(monkeypatch):
    fake = patch_get(monkeypatch, [
        {'bars': {'NVDA': [bar('2026-11-12'), bar('2026-11-13')]}, 'next_page_token': 'tok'},
        {'bars': {'NVDA': [bar('2026-11-16')]}, 'next_page_token': None},
    ])
    api = AlpacaData('k', 's', now=lambda: ny(2026, 11, 16, 10, 0))
    data = await api.get_daily_bars(['NVDA'])
    assert [b['date'] for b in data['NVDA']] == ['2026-11-12', '2026-11-13']
    assert len(fake.calls) == 2
    assert fake.calls[1]['params']['page_token'] == 'tok'
    p = fake.calls[0]['params']
    assert fake.calls[0]['url'] == 'https://data.alpaca.markets/v2/stocks/bars'
    assert (p['symbols'], p['timeframe'], p['adjustment'], p['feed'], p['limit']) == ('NVDA', '1Day', 'all', 'sip', 10000)
    assert p['end'] == '2026-11-16T14:44:00Z'
    assert fake.calls[0]['headers'] == {'APCA-API-KEY-ID': 'k', 'APCA-API-SECRET-KEY': 's'}
    b = data['NVDA'][0]
    assert b == {'date': '2026-11-12', 'open': 10.0, 'high': 11.0, 'low': 9.0, 'close': 10.5, 'volume': 1000.0}


@pytest.mark.asyncio
async def test_today_included_after_1630(monkeypatch):
    patch_get(monkeypatch, [{'bars': {'NVDA': [bar('2026-11-16')]}}])
    api = AlpacaData('k', 's', now=lambda: ny(2026, 11, 16, 16, 30))
    data = await api.get_daily_bars(['NVDA'])
    assert [b['date'] for b in data['NVDA']] == ['2026-11-16']


@pytest.mark.asyncio
async def test_batches_of_100_symbols(monkeypatch):
    fake = patch_get(monkeypatch, [{'bars': {}}, {'bars': {}}])
    symbols = [f'S{i}' for i in range(101)]
    data = await AlpacaData('k', 's', now=lambda: ny(2026, 11, 16, 10)).get_daily_bars(symbols)
    assert len(fake.calls) == 2
    assert len(fake.calls[0]['params']['symbols'].split(',')) == 100
    assert fake.calls[1]['params']['symbols'] == 'S100'
    assert data == {s: [] for s in symbols}


@pytest.mark.asyncio
async def test_invalid_ohlc_drops_symbol_and_records_error(monkeypatch):
    patch_get(monkeypatch, [{'bars': {
        'BAD': [bar('2026-11-12', h=8.0)],
        'OK': [bar('2026-11-12')],
    }}])
    api = AlpacaData('k', 's', now=lambda: ny(2026, 11, 16, 10))
    data = await api.get_daily_bars(['BAD', 'OK'])
    assert 'BAD' not in data
    assert len(data['OK']) == 1
    assert 'BAD' in api.last_errors


@pytest.mark.asyncio
async def test_unconfigured_raises_readable_error():
    api = AlpacaData()
    assert api.configured is False
    for coro in (api.get_daily_bars(['NVDA']), api.get_latest_price('NVDA'),
                 api.count_news_since('NVDA', ny(2026, 11, 15))):
        with pytest.raises(MarketDataUnavailable, match='Alpaca API Key/Secret'):
            await coro


@pytest.mark.asyncio
async def test_error_message_has_no_secret(monkeypatch):
    patch_get(monkeypatch, [403])
    api = AlpacaData('KEY-ABC', 'SECRET-XYZ', now=lambda: ny(2026, 11, 16, 10))
    with pytest.raises(MarketDataUnavailable) as exc:
        await api.get_daily_bars(['NVDA'])
    assert str(exc.value) == 'Alpaca 行情请求未通过（HTTP 403）'
    assert 'KEY-ABC' not in str(exc.value) and 'SECRET-XYZ' not in str(exc.value)


@pytest.mark.asyncio
async def test_retries_429_and_5xx_twice_then_fails(monkeypatch, no_sleep):
    fake = patch_get(monkeypatch, [429, 503, {'bars': {'NVDA': [bar('2026-11-13')]}}])
    api = AlpacaData('k', 's', now=lambda: ny(2026, 11, 16, 10))
    data = await api.get_daily_bars(['NVDA'])
    assert len(data['NVDA']) == 1 and len(fake.calls) == 3
    assert no_sleep == [1, 2]

    fake = patch_get(monkeypatch, [500, 500, 500])
    with pytest.raises(MarketDataUnavailable, match='HTTP 500'):
        await api.get_latest_price('NVDA')
    assert len(fake.calls) == 3


@pytest.mark.asyncio
async def test_latest_price(monkeypatch):
    fake = patch_get(monkeypatch, [{'symbol': 'NVDA', 'trade': {'p': 187.25}}])
    price = await AlpacaData('k', 's').get_latest_price('NVDA')
    assert price == 187.25
    assert fake.calls[0]['url'] == 'https://data.alpaca.markets/v2/stocks/NVDA/trades/latest'
    assert fake.calls[0]['params']['feed'] == 'iex'


@pytest.mark.asyncio
async def test_latest_price_rejects_bad_payload(monkeypatch):
    patch_get(monkeypatch, [{'trade': {'p': 0}}])
    with pytest.raises(MarketDataUnavailable):
        await AlpacaData('k', 's').get_latest_price('NVDA')


@pytest.mark.asyncio
async def test_count_news_since(monkeypatch):
    fake = patch_get(monkeypatch, [{'news': [{'id': 1}, {'id': 2}, {'id': 3}]}])
    since = datetime(2026, 11, 15, 12, 0, tzinfo=ZoneInfo('UTC'))
    assert await AlpacaData('k', 's').count_news_since('NVDA', since) == 3
    c = fake.calls[0]
    assert c['url'] == 'https://data.alpaca.markets/v1beta1/news'
    assert c['params'] == {'symbols': 'NVDA', 'start': '2026-11-15T12:00:00Z', 'limit': 50}
