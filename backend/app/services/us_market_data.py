"""Read-only FMP adapter. No broker operations, key-bearing URLs, or synthetic data."""
import asyncio
from datetime import date, datetime, timedelta, timezone
import math
import re
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx

from backend.app.config import settings


class MarketDataError(RuntimeError):
    pass


class USMarketData:
    BASE_URL = 'https://financialmodelingprep.com/stable'

    def __init__(self, api_key=None, now=None):
        self.api_key = settings.FMP_API_KEY if api_key is None else api_key
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._limit = asyncio.Semaphore(2)

    async def _get(self, endpoint, **params):
        if not self.api_key:
            raise MarketDataError('未配置 FMP API Key，无法读取底层美股数据')
        symbol = params.get('symbol', params.get('symbols', ''))
        if not re.fullmatch(r'[A-Z.\-]{1,12}', symbol):
            raise MarketDataError('不支持的美股代码')
        async with self._limit:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
                for attempt in range(3):
                    try:
                        response = await client.get(f'{self.BASE_URL}/{endpoint}',
                            params={**params, 'apikey': self.api_key})
                        if response.status_code == 429 or response.status_code >= 500:
                            if attempt < 2:
                                await asyncio.sleep(.5 * (2 ** attempt))
                                continue
                        if response.status_code != 200:
                            raise MarketDataError(f'FMP 数据请求未通过（HTTP {response.status_code}），请检查额度和数据权限')
                        result = response.json()
                        if not isinstance(result, list):
                            raise MarketDataError('FMP 数据响应异常，请检查订阅权限')
                        return result
                    except (httpx.HTTPError, ValueError):
                        if attempt < 2:
                            await asyncio.sleep(.5 * (2 ** attempt))
                            continue
                        raise MarketDataError('FMP 数据网络或响应异常，请稍后重试') from None

    def sources(self, symbol):
        return [{'id': 'history', 'title': f'FMP {symbol} 已完成日线',
                 'url': f'{self.BASE_URL}/historical-price-eod/full?{urlencode({"symbol": symbol})}'}]

    async def get_history(self, symbol):
        now = self.now().astimezone(ZoneInfo('America/New_York'))
        rows = await self._get('historical-price-eod/full', symbol=symbol,
            **{'from': (now.date()-timedelta(days=550)).isoformat(), 'to': now.date().isoformat()})
        # Be conservative: exclude today's bar until 16:30 ET, including early-close days.
        completed = now.date() if (now.hour, now.minute) >= (16, 30) else now.date()-timedelta(days=1)
        history = {}
        for row in rows:
            try:
                day = date.fromisoformat(str(row['date'])[:10])
                if day > completed:
                    continue
                item = {'date': day.isoformat()}
                for key in ('open','high','low','close','volume'):
                    value = float(row[key])
                    if not math.isfinite(value) or (value <= 0 if key != 'volume' else value < 0):
                        raise ValueError('invalid price')
                    item[key] = value
                if not item['low'] <= item['close'] <= item['high'] or item['low'] > item['high']:
                    raise ValueError('invalid OHLC')
                history[item['date']] = item
            except (ValueError, KeyError, TypeError):
                raise MarketDataError('FMP 日线字段无效，已拒绝用于分析') from None
        result = [history[key] for key in sorted(history)]
        if not result or (now.date()-date.fromisoformat(result[-1]['date'])).days > 7:
            raise MarketDataError('底层美股行情缺失或过期，已拒绝用于分析')
        return result

    async def get_research(self, symbol):
        today = self.now().date()
        financials = await self._get('income-statement', symbol=symbol, period='quarter', limit=8)
        news_rows = await self._get('news/stock', symbols=symbol, limit=8,
            **{'from': (today-timedelta(days=14)).isoformat(), 'to': today.isoformat()})
        earnings_rows = await self._get('earnings', symbol=symbol, limit=20)
        if not financials:
            raise MarketDataError('季度财务数据缺失，无法复核成长性')
        sources = [{'id':'financials', 'title': f'FMP {symbol} 季度损益表',
            'url':f'{self.BASE_URL}/income-statement?{urlencode({"symbol": symbol, "period": "quarter"})}',
            'as_of':str(financials[0].get('date',''))[:10]},
            {'id':'earnings', 'title':f'FMP {symbol} 财报日历',
             'url':f'{self.BASE_URL}/earnings?{urlencode({"symbol": symbol})}', 'as_of':today.isoformat()}]
        news = []
        for index, row in enumerate(news_rows):
            url = str(row.get('url',''))
            if urlsplit(url).scheme not in ('http','https') or not urlsplit(url).hostname:
                continue
            published = str(row.get('publishedDate',''))[:10]
            try:
                if not today-timedelta(days=14) <= date.fromisoformat(published) <= today:
                    continue
            except ValueError:
                continue
            item = {'id':f'news-{index}', 'title':str(row.get('title',''))[:300],
                    'date':published, 'url':url, 'summary':str(row.get('text',''))[:1200]}
            news.append(item)
            sources.append({'id':item['id'], 'title':item['title'], 'url':url, 'as_of':published})
        earnings = []
        for row in earnings_rows:
            try:
                day = date.fromisoformat(str(row['date'])[:10])
                if today <= day <= today+timedelta(days=28):
                    earnings.append({'date':day.isoformat(), 'title':'公司财报发布（数据商日历，时间可能调整）',
                        'source_id':'earnings', 'source_url':sources[1]['url']})
            except (KeyError, ValueError, TypeError):
                raise MarketDataError('财报日历字段无效，不能确认事件风险') from None
        return {'financials': financials, 'news': news, 'earnings':earnings, 'sources':sources}
