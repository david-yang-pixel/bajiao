import logging
from typing import Any, Dict, List, Optional
import httpx

from backend.app.config import settings

logger = logging.getLogger(__name__)

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

class BinanceClient:
    """
    Binance Client that prioritizes remote MCP endpoint (JSON-RPC 2.0 over HTTP POST)
    and falls back to Binance Public REST API (https://api.binance.com/api/v3/...) on error or absence of MCP endpoint.
    """

    def __init__(
        self,
        mcp_url: Optional[str] = None,
        api_key: Optional[str] = None,
        rest_base_url: str = "https://api.binance.com",
        timeout: float = 10.0
    ):
        self.mcp_url = mcp_url if mcp_url is not None else settings.BINANCE_MCP_URL
        self.api_key = api_key if api_key is not None else settings.BINANCE_API_KEY
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
        """
        Fetch the current ticker price for a given symbol.
        Returns float price.
        """
        # REST endpoint: ticker/price?symbol=...
        data = await self._call_mcp_or_api("ticker/price", {"symbol": symbol.upper()})
        # data should be {"symbol": ..., "price": "142.50"}
        if isinstance(data, dict) and "price" in data:
            return float(data["price"])
        raise ValueError(f"Unexpected price response for {symbol}: {data}")

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
