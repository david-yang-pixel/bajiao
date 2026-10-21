import hashlib
import hmac
import logging
import math
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
import httpx

from backend.app.config import settings

logger = logging.getLogger(__name__)


class BinanceAccountError(RuntimeError):
    """Account failure containing only safe status codes and fixed explanations."""

    def __init__(self, wallet: str, response: httpx.Response):
        self.status_code = response.status_code
        self.code = None
        try:
            data = response.json()
            code = data.get("code") if isinstance(data, dict) else None
            if isinstance(code, int) and not isinstance(code, bool):
                self.code = code
        except ValueError:
            pass
        explanations = {
            -1022: "签名校验失败，请检查配套的 API Key 和 API Secret",
            -1021: "请求时间超出允许范围，请检查服务器时间",
            -2014: "API Key 格式无效",
            -2015: "API Key、读取权限或 IP 白名单校验失败",
            -2008: "API Key 无效",
            -1003: "请求过于频繁，请稍后重试",
        }
        status = f"HTTP {self.status_code}"
        if self.code is not None:
            status += f"，币安错误码 {self.code}"
        reason = explanations.get(self.code, "账户请求被币安拒绝")
        super().__init__(f"币安{wallet}读取失败（{status}）：{reason}；本次同步已取消，现有持仓保持不变")

# Common bStocks symbols on Binance
SUPPORTED_BSTOCKS = [
    "NVDABUSDT",
    "TSLABUSDT",
    "AAPLBUSDT",
    "MSFTBUSDT",
    "AMZNBUSDT",
    "GOOGBUSDT",
    "METABUSDT",
    "COINBUSDT"
]

def is_equity_asset(symbol: str) -> bool:
    return bool(re.fullmatch(r'EQ_[A-Z][A-Z0-9.\-]{0,19}', symbol.upper()))


def position_market(symbol: str) -> Dict[str, str]:
    equity = is_equity_asset(symbol)
    return {'asset_type': 'equity' if equity else 'spot',
            'quote_currency': 'USD' if equity else 'USDT',
            'price_method': 'bid_ask_midpoint' if equity else 'last_trade'}


class BinanceClient:
    """
    Binance Client that prioritizes remote MCP endpoint (JSON-RPC 2.0 over HTTP POST)
    and falls back to Binance Public REST API (https://api.binance.com/api/v3/...) on error or absence of MCP endpoint.
    """

    def __init__(
        self,
        mcp_url: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        rest_base_url: str = "https://api.binance.com",
        timeout: float = 10.0
    ):
        self.mcp_url = mcp_url if mcp_url is not None else settings.BINANCE_MCP_URL
        self.api_key = api_key if api_key is not None else settings.BINANCE_API_KEY
        self.api_secret = api_secret if api_secret is not None else settings.BINANCE_API_SECRET
        self.rest_base_url = rest_base_url.rstrip("/")
        self.timeout = timeout

    def get_supported_bstocks(self) -> List[str]:
        """
        Return the list of supported bStocks symbol identifiers.
        """
        return list(SUPPORTED_BSTOCKS)

    async def _call_remote_mcp(self, method: str, params: Dict[str, Any]) -> Any:
        """
        Call Binance Remote MCP endpoint using JSON-RPC 2.0 over HTTP POST.
        """
        if not self.mcp_url:
            raise ValueError("BINANCE_MCP_URL is not configured")

        headers = {
            "Content-Type": "application/json"
        }
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.mcp_url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data and data["error"]:
                raise RuntimeError(f"MCP RPC Error: {data['error']}")
            return data.get("result")

    async def _call_rest_api(self, endpoint: str, params: Dict[str, Any]) -> Any:
        """
        Fallback: Call Binance Public REST API directly.
        """
        url = f"{self.rest_base_url}/api/v3/{endpoint.lstrip('/')}"
        headers = {}
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def _call_mcp_or_api(self, endpoint_or_method: str, params: Dict[str, Any]) -> Any:
        """
        Try calling Remote MCP first, fall back to Public REST API on error or if MCP URL is not configured.
        """
        if self.mcp_url:
            try:
                # In MCP context, methods could be e.g. "binance.ticker_price" or "ticker/price"
                return await self._call_remote_mcp(endpoint_or_method, params)
            except Exception as e:
                logger.warning(
                    f"Remote MCP call failed for {endpoint_or_method}: {e}. Falling back to REST API."
                )

        return await self._call_rest_api(endpoint_or_method, params)

    async def get_ticker_price(self, symbol: str) -> float:
        """EQ_ uses native equity USD quotes; tokens retain the spot route."""
        symbol = symbol.upper()
        if is_equity_asset(symbol):
            if not self.api_key:
                raise ValueError('股票行情需要币安 API Key')
            underlying = symbol[3:]
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f'{self.rest_base_url}/sapi/v1/equity/market/quote',
                    params={'symbol': underlying}, headers={'X-MBX-APIKEY': self.api_key})
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, dict) or data.get('symbol') != underlying:
                raise ValueError('股票报价标识不匹配')
            bid, ask = float(data.get('bidPrice', 0)), float(data.get('askPrice', 0))
            if not all(math.isfinite(v) and v > 0 for v in (bid, ask)) or bid > ask:
                raise ValueError('股票买卖报价无效或暂不可用')
            return bid / 2 + ask / 2
        data = await self._call_mcp_or_api('ticker/price', {'symbol': symbol})
        if isinstance(data, dict) and 'price' in data:
            price = float(data['price'])
            if math.isfinite(price) and price > 0:
                return price
        raise ValueError(f'Unexpected price response for {symbol}')

    async def get_equity_average_buy_price(self, symbol: str, quantity: float) -> Optional[float]:
        """Moving-average USD cost excluding fees, only with complete matching inventory.

        Reject incomplete, transferred or ambiguous history instead of substituting
        a token/spot cost. A user-entered cost takes precedence in the sync service.
        """
        if not is_equity_asset(symbol) or not self.api_key or not self.api_secret:
            return None
        try:
            params = {'symbol': symbol[3:], 'startTime': 0, 'endTime': int(time.time()*1000),
                      'current': 1, 'size': 100, 'timestamp': int(time.time()*1000), 'recvWindow': 5000}
            params['signature'] = self._generate_signature(params)
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f'{self.rest_base_url}/sapi/v1/equity/trade/history',
                    params=params, headers={'X-MBX-APIKEY': self.api_key})
                response.raise_for_status()
                data = response.json()
            rows = data['rows']
            # Bounded to one page: larger history is explicitly unknown, not truncated.
            if not isinstance(rows, list) or not rows or int(data['total']) != len(rows):
                return None
            rows = sorted(rows, key=lambda t: int(t['executionAt']))
            sides_by_time = {}
            for trade in rows:
                timestamp = int(trade['executionAt'])
                sides = sides_by_time.setdefault(timestamp, set())
                sides.add(trade['side'])
                if len(sides) > 1:
                    return None  # Execution IDs do not document an ordering tie-break.
            inventory = cost = 0.0
            seen = set()
            for trade in rows:
                execution_id = trade['executionId']
                if execution_id in seen or trade['symbol'] != symbol[3:]:
                    return None
                seen.add(execution_id)
                qty, price = float(trade['qty']), float(trade['price'])
                if not all(math.isfinite(v) and v > 0 for v in (qty, price)):
                    return None
                if trade['side'] == 'BUY':
                    inventory += qty
                    cost += qty * price
                elif trade['side'] == 'SELL' and inventory > 0 and qty <= inventory + 1e-9:
                    cost -= min(qty, inventory) * cost / inventory
                    inventory = max(0, inventory - qty)
                else:
                    return None
            if inventory <= 0 or not math.isclose(inventory, quantity, rel_tol=1e-7, abs_tol=1e-9):
                return None
            average = cost / inventory
            return average if math.isfinite(average) and average > 0 else None
        except Exception:
            logger.warning('股票成交成本暂不可用 (%s)', symbol)
            return None

    async def _call_signed_read(self, path: str, params: Dict[str, Any]) -> Any:
        """Private read-only GET; never expose signed URLs or response bodies."""
        params = dict(params, timestamp=int(time.time()*1000), recvWindow=5000)
        params['signature'] = self._generate_signature(params)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(self.rest_base_url + path, params=params,
                                        headers={'X-MBX-APIKEY': self.api_key})
            response.raise_for_status()
            return response.json()

    async def get_equity_conversion_cost(self, symbol: str, quantity: float,
                                        asset_balances: Optional[Dict[str, float]] = None) -> Optional[Dict[str, Any]]:
        """Trace an explicit redemption back to original paid token acquisitions.

        Bounded to complete source pages and 90 days of Convert history; never
        infer a USD/stablecoin rate or silently use an incomplete ledger.
        """
        from backend.app.services.equity_cost_basis import reconstruct_equity_cost
        if not is_equity_asset(symbol) or not self.api_key or not self.api_secret:
            return None
        try:
            now = int(time.time()*1000)
            day = 86400000
            start = max(0, now - 90*day)
            conversion_data = await self._call_signed_read('/sapi/v1/equity/tokenized/history',
                {'startTime':0, 'endTime':now, 'size':100})
            if conversion_data.get('hasMore') is not False:
                return None
            conversions = [r for r in conversion_data['rows']
                           if r.get('underlyingAsset') == symbol[3:] and r.get('status') == 'S']
            tokens = {r['tokenizedAsset'] for r in conversions}
            if len(tokens) != 1:
                return None
            token = next(iter(tokens))
            if not re.fullmatch(r'[A-Z][A-Z0-9]{0,24}', token):
                return None
            spot = await self._call_signed_read('/api/v3/myTrades',
                {'symbol':token+'USDT','fromId':0,'limit':1000})
            if not isinstance(spot,list) or len(spot) >= 1000:
                return None
            native = await self._call_signed_read('/sapi/v1/equity/trade/history',
                {'symbol':symbol[3:],'startTime':0,'endTime':now,'current':1,'size':100})
            if not isinstance(native.get('rows'),list) or int(native['total']) != len(native['rows']):
                return None
            times = [int(r['time']) for r in spot] + [int(r['updatedAt']) for r in conversions]
            times += [int(r['executionAt']) for r in native['rows']]
            if any(t < start or t > now for t in times):
                return None
            flows = []
            end = now
            while end >= start:
                window_start = max(start, end - 30*day + 1)
                data = await self._call_signed_read('/sapi/v1/convert/tradeFlow',
                    {'startTime':window_start,'endTime':end,'limit':1000})
                if data.get('moreData') is not False or not isinstance(data.get('list'),list):
                    return None
                flows.extend(data['list'])
                end = window_start - 1
            if asset_balances is None:
                fresh = {r['asset']:float(r['total']) for r in await self.get_account_balances()}
                if not math.isclose(fresh.get(symbol,0), quantity, rel_tol=1e-7, abs_tol=1e-9):
                    return None
                asset_balances = fresh
            return reconstruct_equity_cost(symbol=symbol,quantity=quantity,
                token_quantity=asset_balances.get(token,0),conversions=conversions,
                spot=spot,flows=flows,trades=native['rows'])
        except Exception:
            logger.warning('转换成本链路暂不可完整核对 (%s)', symbol)
            return None

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Fetch klines (candlesticks) for a given symbol and interval.
        Returns a list of dicts with keys: timestamp, open, high, low, close, volume.
        """
        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit
        }
        raw_klines = await self._call_mcp_or_api("klines", params)

        klines: List[Dict[str, Any]] = []
        # Binance klines format:
        # [ [ open_time, open, high, low, close, volume, close_time, quote_asset_vol, trades, ... ], ... ]
        for item in raw_klines:
            if isinstance(item, list) and len(item) >= 6:
                klines.append({
                    "timestamp": int(item[0]),
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5])
                })
            elif isinstance(item, dict):
                # In case MCP returns formatted dicts
                klines.append({
                    "timestamp": int(item.get("timestamp") or item.get("open_time") or item.get("time", 0)),
                    "open": float(item.get("open", 0.0)),
                    "high": float(item.get("high", 0.0)),
                    "low": float(item.get("low", 0.0)),
                    "close": float(item.get("close", 0.0)),
                    "volume": float(item.get("volume", 0.0))
                })

        return klines

    def _generate_signature(self, params: Dict[str, Any], *, api_secret: Optional[str] = None) -> str:
        """
        Generate HMAC-SHA256 signature for Binance Private Signed Endpoints.
        """
        secret = self.api_secret if api_secret is None else api_secret
        if not secret:
            raise ValueError("BINANCE_API_SECRET 未配置，无法调用币安私有鉴权接口")
        query_str = urlencode(params)
        return hmac.new(
            secret.encode("utf-8"),
            query_str.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    async def get_account_balances(self) -> List[Dict[str, Any]]:
        """
        Fetch combined account balances from Binance:
        1. Spot account (GET /api/v3/account)
        2. Funding account (POST /sapi/v1/asset/get-funding-asset) - holds bStocks/Pay/Convert assets
        Returns all non-zero asset balances merged by asset.
        """
        api_key, api_secret = self.api_key, self.api_secret
        if not api_key or not api_secret:
            raise ValueError("需要同时配置币安 API Key 和 API Secret 才能读取账户持仓")

        balances_map: Dict[str, Dict[str, Any]] = {}

        # 1. Fetch Spot Account Balances
        try:
            params: Dict[str, Any] = {
                "timestamp": int(time.time() * 1000),
                "recvWindow": 5000
            }
            params["signature"] = self._generate_signature(params, api_secret=api_secret)
            headers = {"X-MBX-APIKEY": api_key}
            url = f"{self.rest_base_url}/api/v3/account"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if not isinstance(data, dict) or not isinstance(data.get("balances"), list):
                        raise RuntimeError("币安现货账户响应格式异常")
                    for b in data["balances"]:
                        asset = b.get("asset", "").upper()
                        free = float(b.get("free", 0))
                        locked = float(b.get("locked", 0))
                        tot = free + locked
                        if tot > 0:
                            balances_map[asset] = {
                                "asset": asset,
                                "free": free,
                                "locked": locked,
                                "total": tot,
                                "wallet": "现货账户"
                            }
                else:
                    raise BinanceAccountError("现货账户", resp)
        except BinanceAccountError as e:
            logger.warning("%s", e)
            raise
        except Exception as e:
            logger.warning("币安现货账户读取失败 (%s)", type(e).__name__)
            raise RuntimeError("币安现货账户读取失败，本次同步已取消，现有持仓保持不变") from e

        # 2. Fetch Funding Account Balances (资金账户 - bStocks 常用账户)
        try:
            params = {
                "needBtcValuation": "false",
                "timestamp": int(time.time() * 1000),
                "recvWindow": 5000
            }
            params["signature"] = self._generate_signature(params, api_secret=api_secret)
            headers = {"X-MBX-APIKEY": api_key}
            url = f"{self.rest_base_url}/sapi/v1/asset/get-funding-asset"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, params=params, headers=headers)
                if resp.status_code == 200:
                    funding_data = resp.json()
                    if isinstance(funding_data, list):
                        for b in funding_data:
                            asset = b.get("asset", "").upper()
                            free = float(b.get("free", 0))
                            locked = float(b.get("locked", 0)) + float(b.get("freeze", 0)) + float(b.get("withdrawing", 0))
                            tot = free + locked
                            if tot > 0:
                                if asset in balances_map:
                                    balances_map[asset]["free"] += free
                                    balances_map[asset]["locked"] += locked
                                    balances_map[asset]["total"] += tot
                                    balances_map[asset]["wallet"] = "现货+资金账户"
                                else:
                                    balances_map[asset] = {
                                        "asset": asset,
                                        "free": free,
                                        "locked": locked,
                                        "total": tot,
                                        "wallet": "资金账户"
                                    }
                    else:
                        raise RuntimeError("币安资金账户响应格式异常")
                else:
                    raise BinanceAccountError("资金账户", resp)
        except BinanceAccountError as e:
            logger.warning("%s", e)
            raise
        except Exception as e:
            logger.warning("币安资金账户读取失败 (%s)", type(e).__name__)
            raise RuntimeError("币安资金账户读取失败，本次同步已取消，现有持仓保持不变") from e

        return list(balances_map.values())

    async def get_average_buy_price(self, symbol: str) -> Optional[float]:
        """
        Calculate weighted average buy price from:
        1. Recent buy trades in Spot market (GET /api/v3/myTrades).
        2. Recent Binance Convert orders (GET /sapi/v1/convert/tradeFlow).
        """
        if not self.api_key or not self.api_secret:
            return None

        clean_symbol = symbol.upper()

        # 1. Check Spot Trades
        try:
            params: Dict[str, Any] = {
                "symbol": clean_symbol,
                "limit": 50,
                "timestamp": int(time.time() * 1000),
                "recvWindow": 5000
            }
            params["signature"] = self._generate_signature(params)
            headers = {"X-MBX-APIKEY": self.api_key}
            url = f"{self.rest_base_url}/api/v3/myTrades"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code == 200:
                    trades = resp.json()
                    if isinstance(trades, list) and len(trades) > 0:
                        trades.sort(key=lambda x: x.get("time", 0))
                        current_inventory = 0.0
                        current_cost = 0.0
                        last_buy_price = None

                        for t in trades:
                            qty = float(t.get("qty", 0))
                            price = float(t.get("price", 0))
                            is_buyer = t.get("isBuyer", True)

                            if is_buyer:
                                current_cost += qty * price
                                current_inventory += qty
                                last_buy_price = price
                            else:
                                if current_inventory > 0:
                                    unit_cost = current_cost / current_inventory
                                    sold_qty = min(qty, current_inventory)
                                    current_inventory -= sold_qty
                                    current_cost -= sold_qty * unit_cost
                                    if current_inventory <= 1e-5:
                                        current_inventory = 0.0
                                        current_cost = 0.0

                        if current_inventory > 1e-5:
                            return round(current_cost / current_inventory, 4)
                        if last_buy_price is not None:
                            return last_buy_price
        except Exception as e:
            logger.warning(f"获取 {clean_symbol} 现货成交记录异常: {e}")

        # 2. Check Convert Trade Flow (闪兑/换币记录)
        try:
            base_asset = clean_symbol.replace("USDT", "").replace("USDC", "")
            now_ms = int(time.time() * 1000)
            convert_params: Dict[str, Any] = {
                "startTime": now_ms - (90 * 24 * 3600 * 1000),
                "endTime": now_ms,
                "timestamp": now_ms,
                "recvWindow": 5000
            }
            convert_params["signature"] = self._generate_signature(convert_params)
            headers = {"X-MBX-APIKEY": self.api_key}
            url = f"{self.rest_base_url}/sapi/v1/convert/tradeFlow"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, params=convert_params, headers=headers)
                if resp.status_code == 200:
                    flow_data = resp.json()
                    trades = flow_data.get("list", [])
                    # Find matching buy trades where toAsset matches our base_asset
                    for item in reversed(trades):
                        to_asset = item.get("toAsset", "").upper()
                        from_asset = item.get("fromAsset", "").upper()
                        if to_asset == base_asset and from_asset in ["USDT", "USDC", "BUSD", "FDUSD", "USD"]:
                            to_amt = float(item.get("toAmount", 0))
                            from_amt = float(item.get("fromAmount", 0))
                            if to_amt > 0:
                                unit_price = round(from_amt / to_amt, 4)
                                logger.info(f"从闪兑订单获取到 {clean_symbol} 买入成本价: {unit_price}")
                                return unit_price
        except Exception as e:
            logger.warning(f"获取 {clean_symbol} 闪兑记录异常: {e}")

        return None
