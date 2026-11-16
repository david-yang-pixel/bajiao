"""Alpaca market-data REST adapter (read-only): US stock daily bars, latest trade, news count."""
import asyncio
import math
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import httpx

from .constants import HISTORY_CALENDAR_DAYS
from .universe import MarketDataUnavailable

BASE_URL = 'https://data.alpaca.markets'
NY = ZoneInfo('America/New_York')
BATCH_SIZE = 100
MAX_RETRIES = 2
Bar = dict


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse_bar(row: dict) -> Bar:
    ts = datetime.fromisoformat(str(row['t']).replace('Z', '+00:00'))
    item = {'date': ts.astimezone(NY).date().isoformat()}
    for key, field in (('open', 'o'), ('high', 'h'), ('low', 'l'), ('close', 'c'), ('volume', 'v')):
        value = float(row[field])
        if not math.isfinite(value) or (value <= 0 if key != 'volume' else value < 0):
            raise ValueError('invalid value')
        item[key] = value
    if not item['low'] <= item['close'] <= item['high'] or not item['low'] <= item['open'] <= item['high']:
        raise ValueError('invalid OHLC')
    return item


class AlpacaData:
    def __init__(self, api_key: str = '', secret_key: str = '', now: Callable[[], datetime] | None = None):
        self.api_key = api_key
        self.secret_key = secret_key
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.last_errors: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.secret_key)

    async def _get(self, path: str, params: dict) -> dict:
        if not self.configured:
            raise MarketDataUnavailable('请在配置中心填写 Alpaca API Key/Secret')
        headers = {'APCA-API-KEY-ID': self.api_key, 'APCA-API-SECRET-KEY': self.secret_key}
        async with httpx.AsyncClient(timeout=20) as client:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    resp = await client.get(f'{BASE_URL}{path}', params=params, headers=headers)
                except httpx.HTTPError:
                    raise MarketDataUnavailable('Alpaca 行情请求失败（网络错误）') from None
                code = resp.status_code
                if code == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        raise MarketDataUnavailable('Alpaca 行情响应无法解析') from None
                    if not isinstance(data, dict):
                        raise MarketDataUnavailable('Alpaca 行情响应格式无效')
                    return data
                if (code == 429 or code >= 500) and attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise MarketDataUnavailable(f'Alpaca 行情请求未通过（HTTP {code}）')
        raise MarketDataUnavailable('Alpaca 行情请求失败')  # pragma: no cover

    async def get_daily_bars(self, symbols: list[str], days: int = HISTORY_CALENDAR_DAYS) -> dict[str, list[Bar]]:
        if not self.configured:
            raise MarketDataUnavailable('请在配置中心填写 Alpaca API Key/Secret')
        self.last_errors = {}
        now = self.now()
        ny_now = now.astimezone(NY)
        # Be conservative: exclude today's bar until 16:30 ET, including early-close days.
        completed = ny_now.date() if (ny_now.hour, ny_now.minute) >= (16, 30) else ny_now.date() - timedelta(days=1)
        start = _rfc3339(now - timedelta(days=days))
        end = _rfc3339(now - timedelta(minutes=16))
        raw: dict[str, list[dict]] = {}
        for i in range(0, len(symbols), BATCH_SIZE):
            batch = symbols[i:i + BATCH_SIZE]
            params = {'symbols': ','.join(batch), 'timeframe': '1Day', 'start': start, 'end': end,
                      'adjustment': 'all', 'feed': 'sip', 'limit': 10000}
            while True:
                data = await self._get('/v2/stocks/bars', params)
                for sym, rows in (data.get('bars') or {}).items():
                    raw.setdefault(sym, []).extend(rows or [])
                token = data.get('next_page_token')
                if not token:
                    break
                params = {**params, 'page_token': token}
            for sym in batch:
                raw.setdefault(sym, [])
        result: dict[str, list[Bar]] = {}
        for sym, rows in raw.items():
            try:
                bars = {}
                for row in rows:
                    item = _parse_bar(row)
                    if date.fromisoformat(item['date']) <= completed:
                        bars[item['date']] = item
            except (ValueError, KeyError, TypeError):
                self.last_errors[sym] = 'Alpaca 日线字段无效，已拒绝用于分析'
                continue
            result[sym] = [bars[k] for k in sorted(bars)]
        return result

    async def get_latest_price(self, symbol: str) -> float:
        data = await self._get(f'/v2/stocks/{symbol}/trades/latest', {'feed': 'iex'})
        try:
            price = float(data['trade']['p'])
        except (KeyError, TypeError, ValueError):
            raise MarketDataUnavailable(f'{symbol} Alpaca 最新成交价缺失') from None
        if not math.isfinite(price) or price <= 0:
            raise MarketDataUnavailable(f'{symbol} Alpaca 最新成交价无效')
        return price

    async def count_news_since(self, symbol: str, since: datetime) -> int:
        data = await self._get('/v1beta1/news', {'symbols': symbol, 'start': _rfc3339(since), 'limit': 50})
        return len(data.get('news') or [])
