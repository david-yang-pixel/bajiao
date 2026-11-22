import logging

import pytest

from backend.app.services.short_term.universe import (
    MarketDataUnavailable, TokenPair, UniverseCache, get_token_quote, load_universe,
)


class FakeBinance:
    api_key = 'k'

    def __init__(self):
        self.tokenized = []
        self.spot = {}
        self.book = {}
        self.t24 = {}
        self.fail = False
        self.status = 'TRADING'

    async def get_tokenized_assets(self):
        if self.fail:
            raise RuntimeError('boom')
        return self.tokenized

    async def get_spot_symbols(self):
        if self.fail:
            raise RuntimeError('boom')
        return self.spot

    async def _public_get(self, path, params=None):
        if path.endswith('bookTicker'):
            return self.book
        if path.endswith('exchangeInfo'):
            return {'symbols': [{'symbol': params['symbol'], 'status': self.status}]}
        return self.t24


@pytest.fixture
def fake_binance():
    return FakeBinance()


def asset(code, und, mult='1'):
    return {'assetCode': code, 'underlyingEquitySymbol': und, 'multiplier': mult}


@pytest.mark.asyncio
async def test_universe_requires_trading_spot_pair(fake_binance):
    fake_binance.tokenized = [asset('NVDAB', 'NVDA'), asset('XYZB', 'XYZ'), asset('HALTB', 'HALT')]
    fake_binance.spot = {'NVDABUSDT': 'TRADING', 'HALTBUSDT': 'BREAK'}
    pairs = await load_universe(fake_binance)
    assert [p.symbol for p in pairs] == ['NVDABUSDT'] and pairs[0].underlying == 'NVDA'


@pytest.mark.asyncio
async def test_universe_filters_bad_underlying_and_multiplier_and_sorts(fake_binance):
    fake_binance.tokenized = [asset('TSLAB', 'TSLA'), asset('BADB', 'bad;x'), asset('ZEROB', 'ZERO', '0'),
                              asset('NANB', 'NAN', 'nan'), asset('BRKB', 'BRK.B', '2'), asset('AAPLB', 'AAPL', 'x')]
    fake_binance.spot = {s: 'TRADING' for s in ('TSLABUSDT', 'BADBUSDT', 'ZEROBUSDT', 'NANBUSDT', 'BRKBUSDT', 'AAPLBUSDT')}
    pairs = await load_universe(fake_binance)
    assert [p.symbol for p in pairs] == ['BRKBUSDT', 'TSLABUSDT']
    assert pairs[0].multiplier == 2.0 and pairs[0].underlying == 'BRK.B'


@pytest.mark.asyncio
async def test_cache_serves_stale_on_failure(fake_binance, caplog):
    fake_binance.tokenized = [asset('NVDAB', 'NVDA')]
    fake_binance.spot = {'NVDABUSDT': 'TRADING'}
    cache = UniverseCache(fake_binance)
    first = await cache.get()
    fake_binance.fail = True
    with caplog.at_level(logging.WARNING):
        again = await cache.get(force=True)
    assert again == first and caplog.records


@pytest.mark.asyncio
async def test_cache_without_stale_raises(fake_binance):
    fake_binance.fail = True
    with pytest.raises(MarketDataUnavailable):
        await UniverseCache(fake_binance).get()


@pytest.mark.asyncio
async def test_cache_reuses_within_ttl(fake_binance):
    fake_binance.tokenized = [asset('NVDAB', 'NVDA')]
    fake_binance.spot = {'NVDABUSDT': 'TRADING'}
    cache = UniverseCache(fake_binance)
    await cache.get()
    fake_binance.fail = True
    assert len(await cache.get()) == 1


@pytest.mark.asyncio
async def test_token_quote_flags_wide_spread(fake_binance):
    fake_binance.book = {'bidPrice': '100', 'askPrice': '101'}
    fake_binance.t24 = {'quoteVolume': '50000'}
    q = await get_token_quote(fake_binance, TokenPair('NVDABUSDT', 'NVDAB', 'NVDA', 1.0), 100.0)
    assert q['tradable'] is False and round(q['spread_pct'], 2) == 1.0
    assert round(q['premium_pct'], 2) == 0.5


@pytest.mark.asyncio
async def test_token_quote_tradable_and_missing_underlying(fake_binance):
    fake_binance.book = {'bidPrice': '100', 'askPrice': '100.1'}
    fake_binance.t24 = {'quoteVolume': '50000'}
    q = await get_token_quote(fake_binance, TokenPair('NVDABUSDT', 'NVDAB', 'NVDA', 1.0), None)
    assert q['premium_pct'] is None and q['tradable'] is True
    fake_binance.t24 = {'quoteVolume': '10'}
    assert (await get_token_quote(fake_binance, TokenPair('NVDABUSDT', 'NVDAB', 'NVDA', 1.0), None))['tradable'] is False


@pytest.mark.asyncio
async def test_token_quote_not_tradable_when_halted(fake_binance):
    fake_binance.book = {'bidPrice': '100', 'askPrice': '100.1'}
    fake_binance.t24 = {'quoteVolume': '50000'}
    fake_binance.status = 'BREAK'
    q = await get_token_quote(fake_binance, TokenPair('NVDABUSDT', 'NVDAB', 'NVDA', 1.0), None)
    assert q['status'] == 'BREAK' and q['tradable'] is False


@pytest.mark.asyncio
async def test_tokenized_assets_requires_api_key():
    from backend.app.services.binance_client import BinanceClient
    with pytest.raises(ValueError, match='API Key'):
        await BinanceClient(api_key='', mcp_url='').get_tokenized_assets()
