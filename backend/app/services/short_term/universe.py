import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.app.services.binance_client import BinanceClient
from backend.app.services.short_term.constants import MAX_SPREAD_PCT, MIN_QUOTE_VOLUME_USDT

logger = logging.getLogger(__name__)
_UNDERLYING_RE = re.compile(r'^[A-Z.\-]{1,12}$')


class MarketDataUnavailable(RuntimeError):
    """Market data could not be fetched; callers must not fabricate a substitute."""


@dataclass(frozen=True)
class TokenPair:
    symbol: str
    asset: str
    underlying: str
    multiplier: float


async def load_universe(client: BinanceClient) -> List[TokenPair]:
    assets = await client.get_tokenized_assets()
    spot = await client.get_spot_symbols()
    pairs: Dict[str, TokenPair] = {}
    for a in assets:
        code = str(a.get('assetCode') or '')
        underlying = str(a.get('underlyingEquitySymbol') or '')
        if not code or not _UNDERLYING_RE.fullmatch(underlying):
            continue
        symbol = f'{code}USDT'
        if spot.get(symbol) != 'TRADING':
            continue
        try:
            mult = float(a.get('multiplier'))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(mult) or mult <= 0:
            continue
        pairs[symbol] = TokenPair(symbol, code, underlying, mult)
    return [pairs[k] for k in sorted(pairs)]


class UniverseCache:
    def __init__(self, client: BinanceClient, ttl_seconds: float = 86400):
        self.client = client
        self.ttl = ttl_seconds
        self._pairs: Optional[List[TokenPair]] = None
        self._at = 0.0

    async def get(self, force: bool = False) -> List[TokenPair]:
        if not force and self._pairs is not None and time.monotonic() - self._at < self.ttl:
            return self._pairs
        try:
            self._pairs = await load_universe(self.client)
            self._at = time.monotonic()
        except Exception as exc:
            if self._pairs is None:
                raise MarketDataUnavailable('币安标的池读取失败，且无可用缓存') from exc
            logger.warning('币安标的池刷新失败，使用旧缓存: %s', type(exc).__name__)
        return self._pairs


async def get_token_quote(client: BinanceClient, pair: TokenPair,
                          underlying_price_usd: Optional[float]) -> Dict[str, Any]:
    try:
        book = await client._public_get('/api/v3/ticker/bookTicker', {'symbol': pair.symbol})
        t24 = await client._public_get('/api/v3/ticker/24hr', {'symbol': pair.symbol})
        info = await client._public_get('/api/v3/exchangeInfo', {'symbol': pair.symbol})
        bid, ask = float(book['bidPrice']), float(book['askPrice'])
        volume = float(t24['quoteVolume'])
        status = str(info['symbols'][0]['status'])
    except Exception as exc:
        raise MarketDataUnavailable(f'{pair.symbol} 币安现货报价读取失败') from exc
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid * 100 if mid > 0 else float('inf')
    premium = None
    if underlying_price_usd and underlying_price_usd > 0 and mid > 0:
        premium = (mid / (underlying_price_usd * pair.multiplier) - 1) * 100
    return {
        'symbol': pair.symbol, 'status': status, 'bid_usdt': bid, 'ask_usdt': ask, 'mid_usdt': mid,
        'spread_pct': spread_pct, 'quote_volume_24h_usdt': volume, 'premium_pct': premium,
        'tradable': status == 'TRADING' and spread_pct <= MAX_SPREAD_PCT * 100
                    and volume >= MIN_QUOTE_VOLUME_USDT,
    }
